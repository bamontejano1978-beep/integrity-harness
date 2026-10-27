"""Live execution and visual trace inspector for clause canaries.

Provides a read-only local evaluation harness that executes any canary (and its
contrast control) from ``docs/product/clause_canaries_v0.1.json`` in real time
and shapes the full step-by-step trace for the browser inspector on
``/canarios/`` and ``/en/canaries/``:

- For ``sandbox`` canaries: raw conversation messages, tool calls and results,
  control/environment events, adapted trajectory observations, and the
  clause-by-clause contract evaluation.
- For ``trajectory`` canaries: typed trajectory observations and the
  clause-by-clause contract evaluation.
- For ``swarm`` canaries: mediated hash-chained channel entries, isolated vs
  post-mediation member verdicts, and the system-level swarm contract verdict.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field

from autopilot.adapter import load_adapter_config
from autopilot.agent_sandbox import (
    load_declarative_sandbox_config,
    run_declarative_sandbox_evaluation,
)
from autopilot.canary_status import corpus_metadata
from autopilot.clause_canaries import (
    DEFAULT_REGISTRY,
    CanaryCase,
    CanaryError,
    CanaryRegistry,
    _resolve,
    load_registry,
)
from autopilot.contracts import load_contract
from autopilot.evaluator import ClauseStatus, ContractEvaluation, evaluate_contract
from autopilot.insurance_agent import load_agent_scenario
from autopilot.swarm import load_swarm_pair, run_swarm_pair
from autopilot.swarm_contract import load_swarm_contract
from autopilot.swarm_evaluator import evaluate_swarm_contract
from autopilot.trajectory import Trajectory, load_trajectory, parse_timestamp


class LiveTimelineStep(BaseModel):
    """A single chronological step in the visual execution trace."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    stage: Literal["message", "tool_call", "event", "observation", "channel"]
    actor: str
    title: str
    observed_at: str = ""
    ref: str = ""
    status: Literal["ok", "warn", "fail", "info"] = "info"
    body: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    cited_by_target: bool = False


class LiveClauseVerdict(BaseModel):
    """Clause verdict within a live canary or control run."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    is_target: bool
    status: str
    severity: str = ""
    description: str = ""
    reason: str
    recommended_autonomy: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class LiveRunTrace(BaseModel):
    """Complete execution trace for one role (canary or control)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["canary", "control"]
    kind: Literal["trajectory", "sandbox", "swarm"]
    fixture: str
    contract: str
    contract_id: str
    contract_version: str
    sandbox: str | None = None
    adapter: str | None = None
    target_clause_id: str
    target_status: str
    overall_status: str
    recommended_autonomy: str
    block_release: bool
    duration_ms: float
    steps: list[LiveTimelineStep]
    observations: list[LiveTimelineStep]
    clauses: list[LiveClauseVerdict]


class LiveClauseComparison(BaseModel):
    """Full live execution response for a clause (canary + optional control)."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    kind: Literal["trajectory", "sandbox", "swarm"]
    clause_kind: str = ""
    severity: str | None = None
    domains: list[str] = Field(default_factory=list)
    description: str = ""
    discriminating: bool
    gate_holds: bool
    canary: LiveRunTrace
    control: LiveRunTrace | None = None


def _clause_meta_from_contract(document: Any) -> dict[str, tuple[str, str]]:
    """Map clause_id -> (severity, description) from a loaded contract document."""

    out: dict[str, tuple[str, str]] = {}
    for clause in getattr(document, "clauses", ()):
        cid = getattr(clause, "id", None) or getattr(clause, "clause_id", None)
        if not cid:
            continue
        sev_obj = getattr(clause, "severity", None)
        sev = getattr(sev_obj, "value", str(sev_obj)) if sev_obj is not None else ""
        desc = getattr(clause, "description", "") or getattr(clause, "tipo", "")
        out[str(cid)] = (str(sev), str(desc))
    return out


def _build_observation_steps(
    trajectory: Trajectory,
    target_evidence_refs: set[str],
    start_seq: int = 1,
) -> list[LiveTimelineStep]:
    steps: list[LiveTimelineStep] = []
    for idx, obs in enumerate(trajectory.observations, start=start_seq):
        cited = obs.ref in target_evidence_refs or any(
            obs.ref.startswith(f"{ref}/") or ref.startswith(f"{obs.ref}/")
            for ref in target_evidence_refs
        )
        steps.append(
            LiveTimelineStep(
                seq=idx,
                stage="observation",
                actor=obs.kind.value,
                title=obs.name,
                observed_at=obs.observed_at,
                ref=obs.ref,
                status="fail" if cited else "info",
                body=f"{obs.kind.value}: {obs.name}",
                attributes=dict(obs.attributes),
                cited_by_target=cited,
            )
        )
    return steps


def _build_clause_verdicts(
    evaluation: ContractEvaluation,
    contract_doc: Any,
    target_clause_id: str,
) -> tuple[list[LiveClauseVerdict], str, set[str]]:
    meta = _clause_meta_from_contract(contract_doc)
    verdicts: list[LiveClauseVerdict] = []
    target_status = ClauseStatus.not_applicable.value
    target_refs: set[str] = set()

    for result in evaluation.clause_results:
        sev, desc = meta.get(result.clause_id, ("", ""))
        refs = [item.ref for item in result.evidence]
        is_target = result.clause_id == target_clause_id
        if is_target:
            target_status = result.status.value
            target_refs = set(refs)
        verdicts.append(
            LiveClauseVerdict(
                clause_id=result.clause_id,
                is_target=is_target,
                status=result.status.value,
                severity=sev,
                description=desc,
                reason=result.reason,
                recommended_autonomy=result.recommended_autonomy.value,
                evidence_refs=refs,
            )
        )
    return verdicts, target_status, target_refs


def _trace_trajectory(
    case: CanaryCase,
    root: Path,
    fixture: str,
    role: Literal["canary", "control"],
) -> LiveRunTrace:
    t0 = time.perf_counter()
    document = load_contract(_resolve(root, case.contract))
    trajectory = load_trajectory(_resolve(root, fixture))
    evaluation = evaluate_contract(document, trajectory)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    clauses, target_status, target_refs = _build_clause_verdicts(
        evaluation, document, case.clause_id
    )
    obs_steps = _build_observation_steps(trajectory, target_refs, start_seq=1)

    return LiveRunTrace(
        role=role,
        kind="trajectory",
        fixture=fixture,
        contract=case.contract,
        contract_id=document.contract.id,
        contract_version=document.contract.version,
        target_clause_id=case.clause_id,
        target_status=target_status,
        overall_status=evaluation.status.value,
        recommended_autonomy=evaluation.recommended_autonomy.value,
        block_release=evaluation.block_release,
        duration_ms=elapsed_ms,
        steps=obs_steps,
        observations=obs_steps,
        clauses=clauses,
    )


def _raw_ref_matches(raw_ref: str, target_refs: set[str]) -> bool:
    if raw_ref in target_refs:
        return True
    prefix = f"{raw_ref}/"
    return any(ref.startswith(prefix) for ref in target_refs)


def _trace_sandbox(
    case: CanaryCase,
    root: Path,
    fixture: str,
    role: Literal["canary", "control"],
) -> LiveRunTrace:
    if case.adapter is None or case.sandbox is None:
        raise CanaryError(f"sandbox canary {case.clause_id} needs an adapter and a sandbox")
    t0 = time.perf_counter()
    document = load_contract(_resolve(root, case.contract))
    adapter = load_adapter_config(_resolve(root, case.adapter))
    sandbox = load_declarative_sandbox_config(_resolve(root, case.sandbox))
    scenario = load_agent_scenario(_resolve(root, fixture))
    bundle = run_declarative_sandbox_evaluation(document, adapter, sandbox, scenario)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    evaluation = bundle.agent_evaluation.evaluation
    trajectory = bundle.agent_evaluation.trajectory
    raw_session = bundle.agent_evaluation.agent_run.raw_session

    clauses, target_status, target_refs = _build_clause_verdicts(
        evaluation, document, case.clause_id
    )

    raw_items: list[tuple[Any, int, str, Any]] = []
    for idx, msg in enumerate(raw_session.messages):
        raw_items.append((parse_timestamp(msg.observed_at), idx, "message", msg))
    for idx, call in enumerate(raw_session.tool_calls, start=len(raw_items)):
        raw_items.append((parse_timestamp(call.observed_at), idx, "tool_call", call))
    for idx, ev in enumerate(raw_session.events, start=len(raw_items)):
        raw_items.append((parse_timestamp(ev.observed_at), idx, "event", ev))
    raw_items.sort(key=lambda item: (item[0], item[1]))

    steps: list[LiveTimelineStep] = []
    for seq, (_, _, kind, item) in enumerate(raw_items, start=1):
        cited = _raw_ref_matches(item.ref, target_refs)
        if kind == "message":
            role_name = item.role.value
            steps.append(
                LiveTimelineStep(
                    seq=seq,
                    stage="message",
                    actor=role_name,
                    title=f"{role_name.upper()} · {item.ref}",
                    observed_at=item.observed_at,
                    ref=item.ref,
                    status="fail" if (cited and role == "canary") else ("ok" if cited else "info"),
                    body=item.content,
                    attributes=dict(item.metadata),
                    cited_by_target=cited,
                )
            )
        elif kind == "tool_call":
            steps.append(
                LiveTimelineStep(
                    seq=seq,
                    stage="tool_call",
                    actor="tool",
                    title=f"tool_call · {item.name}",
                    observed_at=item.observed_at,
                    ref=item.ref,
                    status="fail" if (cited and role == "canary") else ("ok" if item.success else "warn"),
                    body=json.dumps(item.arguments, ensure_ascii=False),
                    attributes={"success": item.success, "result": dict(item.result)},
                    cited_by_target=cited,
                )
            )
        else:
            steps.append(
                LiveTimelineStep(
                    seq=seq,
                    stage="event",
                    actor="environment",
                    title=f"event · {item.name}",
                    observed_at=item.observed_at,
                    ref=item.ref,
                    status="fail" if (cited and role == "canary") else "info",
                    body=item.name,
                    attributes=dict(item.payload),
                    cited_by_target=cited,
                )
            )

    obs_steps = _build_observation_steps(trajectory, target_refs, start_seq=1)

    return LiveRunTrace(
        role=role,
        kind="sandbox",
        fixture=fixture,
        contract=case.contract,
        contract_id=document.contract.id,
        contract_version=document.contract.version,
        sandbox=case.sandbox,
        adapter=case.adapter,
        target_clause_id=case.clause_id,
        target_status=target_status,
        overall_status=evaluation.status.value,
        recommended_autonomy=evaluation.recommended_autonomy.value,
        block_release=evaluation.block_release,
        duration_ms=elapsed_ms,
        steps=steps,
        observations=obs_steps,
        clauses=clauses,
    )


def _trace_swarm(
    case: CanaryCase,
    root: Path,
    fixture: str,
    role: Literal["canary", "control"],
) -> LiveRunTrace:
    if case.base_contract is None or case.adapter is None:
        raise CanaryError(f"swarm canary {case.clause_id} needs a base contract and an adapter")
    t0 = time.perf_counter()
    document = load_swarm_contract(_resolve(root, case.contract))
    base_contract = load_contract(_resolve(root, case.base_contract))
    adapter = load_adapter_config(_resolve(root, case.adapter))
    pair, base_dir = load_swarm_pair(_resolve(root, fixture))
    run = run_swarm_pair(base_contract, adapter, pair, base_dir)
    verdict = evaluate_swarm_contract(document, run.run, run.report.members)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    all_findings = [*verdict.violations, *verdict.topology_incidents]
    by_rule: dict[str, list[Any]] = {}
    for item in all_findings:
        by_rule.setdefault(item.rule_id, []).append(item)

    target_seqs = {
        item.entry_seq
        for item in by_rule.get(case.clause_id, ())
        if item.entry_seq is not None
    }

    steps: list[LiveTimelineStep] = []
    for entry in run.run.channel:
        cited = entry.seq in target_seqs
        body = (
            entry.payload.get("reply_content")
            or entry.payload.get("claimant_content")
            or json.dumps(entry.payload, ensure_ascii=False)
        )
        steps.append(
            LiveTimelineStep(
                seq=entry.seq,
                stage="channel",
                actor=f"{entry.source_agent} → {entry.target_agent}",
                title=f"seq #{entry.seq} · {entry.kind} (ronda {entry.round})",
                observed_at="",
                ref=f"channel-seq-{entry.seq}",
                status="fail" if cited else "info",
                body=str(body),
                attributes={
                    "kind": entry.kind,
                    "round": entry.round,
                    "data_labels": list(entry.payload.get("data_labels", ())),
                    "entry_sha256": entry.entry_sha256[:16] + "…",
                },
                cited_by_target=cited,
            )
        )

    clauses: list[LiveClauseVerdict] = []
    for clause in document.clauses:
        findings = by_rule.get(clause.clause_id, [])
        status = ClauseStatus.failed.value if findings else ClauseStatus.passed.value
        reason = (
            "; ".join(f.detail for f in findings)
            if findings
            else "No system-level violation observed on the mediated channel."
        )
        refs = [f"channel-seq-{f.entry_seq}" for f in findings if f.entry_seq is not None]
        clauses.append(
            LiveClauseVerdict(
                clause_id=clause.clause_id,
                is_target=clause.clause_id == case.clause_id,
                status=status,
                severity="high",
                description=f"Swarm rule ({clause.tipo})",
                reason=reason,
                recommended_autonomy="QUARANTINE" if findings else "COACH",
                evidence_refs=refs,
            )
        )

    target_status = (
        ClauseStatus.failed.value
        if case.clause_id in by_rule
        else ClauseStatus.passed.value
    )
    return LiveRunTrace(
        role=role,
        kind="swarm",
        fixture=fixture,
        contract=case.contract,
        contract_id=document.swarm_contract_id,
        contract_version=document.schema_version,
        adapter=case.adapter,
        target_clause_id=case.clause_id,
        target_status=target_status,
        overall_status=verdict.status,
        recommended_autonomy="QUARANTINE" if verdict.status == "fail" else "COACH",
        block_release=verdict.status == "fail",
        duration_ms=elapsed_ms,
        steps=steps,
        observations=steps,
        clauses=clauses,
    )


def run_live_trace(
    case: CanaryCase,
    root: str | Path,
    *,
    role: Literal["canary", "control"] = "canary",
) -> LiveRunTrace:
    """Execute one canary or control fixture and return its structured visual trace."""

    base = Path(root)
    fixture = case.fixture if role == "canary" else case.control
    if not fixture:
        raise CanaryError(f"{case.clause_id} has no {role} fixture")
    if case.kind == "trajectory":
        return _trace_trajectory(case, base, fixture, role)
    if case.kind == "sandbox":
        return _trace_sandbox(case, base, fixture, role)
    return _trace_swarm(case, base, fixture, role)


def run_live_clause_comparison(
    clause_id: str,
    root: str | Path,
    *,
    registry: CanaryRegistry | None = None,
) -> LiveClauseComparison:
    """Run the negative canary and (if present) the control for ``clause_id``."""

    base = Path(root)
    if registry is None:
        registry = load_registry(base / DEFAULT_REGISTRY)
    case = registry.by_clause().get(clause_id)
    if case is None:
        raise CanaryError(f"clause {clause_id!r} has no executable canary in the registry")

    meta = corpus_metadata(base, registry.corpus).get(clause_id)
    canary_trace = run_live_trace(case, base, role="canary")
    control_trace = (
        run_live_trace(case, base, role="control") if case.control is not None else None
    )

    target_clause_desc = next(
        (c.description for c in canary_trace.clauses if c.is_target and c.description),
        "",
    )
    gate_holds = canary_trace.target_status == ClauseStatus.failed.value and (
        control_trace is None or control_trace.target_status != ClauseStatus.failed.value
    )
    return LiveClauseComparison(
        clause_id=clause_id,
        kind=case.kind,
        clause_kind=meta.kind if meta else "",
        severity=meta.severity if meta else None,
        domains=list(meta.domains) if meta else [],
        description=target_clause_desc,
        discriminating=control_trace is not None,
        gate_holds=gate_holds,
        canary=canary_trace,
        control=control_trace,
    )


def build_all_live_traces(
    root: str | Path,
    *,
    registry: CanaryRegistry | None = None,
) -> dict[str, Any]:
    """Build the full dictionary of live traces for all demonstrated canaries."""

    base = Path(root)
    if registry is None:
        registry = load_registry(base / DEFAULT_REGISTRY)
    clauses: dict[str, Any] = {}
    for case in registry.canaries:
        comparison = run_live_clause_comparison(case.clause_id, base, registry=registry)
        clauses[case.clause_id] = comparison.model_dump(mode="json")
    return {
        "schema_version": "0.1",
        "clause_count": len(clauses),
        "clauses": clauses,
    }


def make_canary_live_handler(root: Path, web_root: Path | None = None) -> type[BaseHTTPRequestHandler]:
    """Create a request handler serving ``web/`` plus ``/api/canary/*`` live endpoints."""

    resolved_root = root.resolve()
    resolved_web = (web_root or (resolved_root / "web")).resolve()

    class CanaryLiveRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, payload: Any) -> None:
            body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, url_path: str) -> None:
            clean = url_path.lstrip("/")
            candidate = (resolved_web / clean).resolve()
            if not str(candidate).startswith(str(resolved_web)):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if candidate.is_dir():
                candidate = candidate / "index.html"
            if not candidate.is_file():
                not_found = resolved_web / "404.html"
                if not_found.is_file():
                    data = not_found.read_bytes()
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            ctype, _ = mimetypes.guess_type(str(candidate))
            if not ctype:
                ctype = "application/octet-stream"
            if ctype.startswith(("text/", "application/json", "application/javascript", "image/svg")):
                ctype = f"{ctype}; charset=utf-8"
            data = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/hackathon/run":
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return

            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urlparse(origin)
                host_header = self.headers.get("Host", "")
                if parsed_origin.netloc != host_header or parsed_origin.scheme not in {"http", "https"}:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "cross-origin request denied"})
                    return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if content_length < 1 or content_length > 1024:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid request body size"})
                return
            try:
                request_data = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON request"})
                return
            if not isinstance(request_data, dict) or request_data.get("project") not in {"meta", "airwallex"}:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "project must be 'meta' or 'airwallex'"})
                return

            project = request_data["project"]
            project_dir = resolved_root / "hackatons" / project
            runner = project_dir / ("run_meta_harness.py" if project == "meta" else "run_airwallex_harness.py")
            if not runner.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "hackathon runner not found"})
                return
            try:
                with tempfile.TemporaryDirectory(prefix="integrity-hackathon-") as temp_dir:
                    env = os.environ.copy()
                    for secret_name in (
                        "AWX_CLIENT_ID",
                        "AWX_API_KEY",
                        "META_MODEL_API_KEY",
                        "LLAMA_API_KEY",
                    ):
                        env.pop(secret_name, None)
                    command = [sys.executable, "-X", "utf8", str(runner), "--output-dir", temp_dir]
                    completed = subprocess.run(
                        command,
                        cwd=resolved_root,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=120,
                        check=False,
                    )
                    if completed.returncode != 0:
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "project": project, "error": completed.stderr[-3000:] or "hackathon run failed"},
                        )
                        return
                    report_name = "meta_safety_scan_report.json" if project == "meta" else "airwallex_boundary_report.json"
                    report_path = Path(temp_dir) / report_name
                    report = json.loads(report_path.read_text(encoding="utf-8"))
            except subprocess.TimeoutExpired:
                self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "project": project, "error": "hackathon run timed out"})
                return
            except (OSError, ValueError, KeyError) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "project": project, "error": str(exc)})
                return

            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "mode": "local_simulation", "project": project, "report": report},
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/api/canary/health":
                registry = load_registry(resolved_root / DEFAULT_REGISTRY)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "canaries": len(registry.canaries),
                        "unproven": len(registry.unproven),
                    },
                )
                return

            if path == "/api/canary/run":
                clause_id = (query.get("clause") or [""])[0].strip()
                if not clause_id:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "missing required query parameter: clause"},
                    )
                    return
                try:
                    comparison = run_live_clause_comparison(clause_id, resolved_root)
                except CanaryError as exc:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "clause_id": clause_id, "error": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "comparison": comparison.model_dump(mode="json"),
                    },
                )
                return

            if path == "/api/canary/run-all":
                t0 = time.perf_counter()
                registry = load_registry(resolved_root / DEFAULT_REGISTRY)
                family = (query.get("family") or [""])[0].strip().lower()
                results: list[dict[str, Any]] = []
                for case in registry.canaries:
                    if family and family not in (
                        case.clause_id.lower()
                        + " "
                        + case.contract.lower()
                        + " "
                        + case.fixture.lower()
                    ):
                        continue
                    comp = run_live_clause_comparison(
                        case.clause_id, resolved_root, registry=registry
                    )
                    results.append(
                        {
                            "clause_id": comp.clause_id,
                            "kind": comp.kind,
                            "discriminating": comp.discriminating,
                            "gate_holds": comp.gate_holds,
                            "canary_status": comp.canary.target_status,
                            "control_status": (
                                comp.control.target_status if comp.control else None
                            ),
                            "duration_ms": round(
                                comp.canary.duration_ms
                                + (comp.control.duration_ms if comp.control else 0.0),
                                2,
                            ),
                        }
                    )
                total_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "count": len(results),
                        "all_hold": all(item["gate_holds"] for item in results),
                        "duration_ms": total_ms,
                        "results": results,
                    },
                )
                return

            self._serve_static(path)

    return CanaryLiveRequestHandler


def create_canary_live_server(
    host: str = "127.0.0.1",
    port: int = 8899,
    *,
    root: str | Path | None = None,
) -> ThreadingHTTPServer:
    """Create a local ThreadingHTTPServer serving the site and live canary API."""

    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    handler = make_canary_live_handler(base)
    return ThreadingHTTPServer((host, port), handler)
