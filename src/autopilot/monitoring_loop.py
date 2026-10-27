"""Continuous monitoring loop (System 1 v0.1 — OP-5/OP-6 of the design packet).

Prototype for the half that is still missing: nothing in the repo observes an
agent between evaluations. This module closes the smallest honest version of
that gap — a scheduled producer that, every ``interval_seconds``, chains the
full evidence path over real artifacts:

    sign trajectory (Ed25519, ledger scheme) → verify against trust key →
    deterministic contract evaluation → signed snapshot with TTL → the file
    the guard server already re-reads before every screening, fail-closed.

The loop NEVER relaxes autonomy: a snapshot only tightens (guardrail
``recovery_without_sandbox``), so the sensor side cannot gain autonomy by
design (invariant I-1 of the System 1 packet). If any step fails, the previous
snapshot keeps aging until its TTL expires and the guard falls back to
quarantine — an absent or lying sensor degrades availability, never assurance
(invariant I-2). Each cycle appends an ``ai_act_monitor_cycle`` event (payload
``kind="monitoring_loop"``) to the append-only ledger.

What this does NOT do (yet — the System 1 packet names the rest): it does not
observe production traffic (the trajectory source is a file), it does not
detect behavioural drift against a baseline (OP-3), and producer/evaluator
roles share one key pair until the operator provisions separate ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - import cycle guard (audit imports this module)
    from autopilot.audit import AuditLedger

from pydantic import BaseModel, ConfigDict, Field

from autopilot.contracts import DSLValidationError, load_contract
from autopilot.enforcement import (
    build_evaluation_snapshot,
    sign_evaluation_snapshot,
    sign_trajectory_payload,
    verify_evaluation_snapshot,
    verify_trajectory_envelope,
)
from autopilot.evaluator import evaluate_contract
from autopilot.trajectory import load_trajectory

LOOP_SCHEMA_VERSION = "0.1"
DEFAULT_INTERVAL_SECONDS = 300.0
EVALUATION_SNAPSHOT_TTL_SECONDS = 900.0
CYCLE_EVENT_KIND = "monitoring_loop"
ENVELOPE_FILENAME = "trajectory-envelope.json"
EVALUATION_FILENAME = "evaluation.json"
SNAPSHOT_FILENAME = "guard-snapshot.json"
CYCLE_HISTORY_FILENAME = "cycle-history.jsonl"


class CycleChange(str, Enum):
    baseline = "baseline"
    unchanged = "unchanged"
    state_changed = "state_changed"


class MonitoringCycle(BaseModel):
    """One emission of the monitoring loop; written as ``cycle-<id>.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    kind: Literal["monitoring_loop"] = "monitoring_loop"
    cycle_id: str
    produced_by: str
    issued_at: str
    interval_seconds: float
    trajectory_source: str
    contract_source: str
    trajectory_id: str
    contract_id: str
    contract_version: str
    evaluation_status: str
    recommended_autonomy: str
    block_release: bool
    snapshot_id: str
    snapshot_expires_at: str
    envelope_sha256: str
    evaluation_sha256: str
    snapshot_sha256: str
    fingerprint: str
    previous_fingerprint: str | None
    change: CycleChange


MONITOR_REFUSAL_SCHEMA_VERSION = "0.1"

MonitorRefusalReason = Literal[
    "previous_cycle_invalid",
    "parameters_invalid",
    "trajectory_not_accepted",
    "observation_not_trusted",
    "observation_stale",
    "envelope_not_trusted",
    "contract_not_accepted",
    "snapshot_not_verifiable",
    "cycle_not_accepted",
]


class MonitorRefusal(BaseModel):
    """One declared fail-closed refusal of a monitoring cycle (observation record).

    The loop emits a signed snapshot and, when a ledger is supplied, one
    ``monitoring_loop_cycle`` observation. When it refuses — a trajectory it
    cannot sign, an envelope it cannot verify, a contract it cannot evaluate, a
    snapshot it cannot hand the guard — the boundary that reports it declares
    the refusal here instead of leaving it only in a terminal. No authority:
    there is no autonomy field, by construction (I-1).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = MONITOR_REFUSAL_SCHEMA_VERSION
    kind: Literal["monitoring_refusal"] = "monitoring_refusal"
    refusal_id: str
    produced_by: str
    issued_at: str
    reason_code: MonitorRefusalReason
    reason: str = Field(min_length=1, max_length=4000)


class MonitorRefusalError(ValueError):
    """A designed refusal: the loop declined to emit this cycle.

    Carries the ``reason_code`` the boundary persists. The loop itself never
    writes to a ledger, so exactly one command owns each declaration.
    """

    def __init__(self, reason_code: MonitorRefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason_code: MonitorRefusalReason = reason_code


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_document(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, document: str) -> str:
    """Write ``document`` to ``path`` atomically (temp file + ``os.replace``)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return _sha256_document(document)


def _evaluation_fingerprint(evaluation: Any) -> str:
    failing = sorted(
        result.clause_id
        for result in evaluation.clause_results
        if result.status.value == "fail"
    )
    body = {
        "block_release": evaluation.block_release,
        "contract_id": evaluation.contract_id,
        "contract_version": evaluation.contract_version,
        "failing_clauses": failing,
        "recommended_autonomy": evaluation.recommended_autonomy.value,
        "status": evaluation.status.value,
        "trajectory_id": evaluation.trajectory_id,
    }
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _classify_change(previous: MonitoringCycle | None, fingerprint: str) -> CycleChange:
    if previous is None:
        return CycleChange.baseline
    if previous.fingerprint == fingerprint:
        return CycleChange.unchanged
    return CycleChange.state_changed


def _monitor_refusal_id(
    *, reason_code: str, reason: str, produced_by: str, issued_at: str
) -> str:
    return "REFUSED-" + hashlib.sha256(
        _canonical_bytes(
            {
                "reason_code": reason_code,
                "reason": reason,
                "produced_by": produced_by,
                "issued_at": issued_at,
            }
        )
    ).hexdigest()[:24].upper()


def record_monitor_refusal(
    ledger: "AuditLedger | None",
    *,
    reason_code: MonitorRefusalReason,
    reason: str,
    produced_by: str,
    issued_at: str | None = None,
) -> MonitorRefusal | None:
    """Declare one fail-closed monitoring refusal in the ledger (observation, no authority).

    Returns the declared record, or ``None`` when no ledger was supplied: the
    caller still refuses, there is simply no durable evidence to append to.
    """

    if ledger is None:
        return None
    moment = (
        datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        if issued_at
        else datetime.now(timezone.utc)
    ).isoformat()
    refusal = MonitorRefusal(
        refusal_id=_monitor_refusal_id(
            reason_code=reason_code, reason=reason, produced_by=produced_by, issued_at=moment
        ),
        produced_by=produced_by,
        issued_at=moment,
        reason_code=reason_code,
        reason=reason,
    )
    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    ledger.append(
        AuditEventType.monitoring_refusal,
        refusal.refusal_id,
        moment,
        refusal.model_dump(mode="json"),
    )
    return refusal


def run_monitoring_cycle(
    *,
    trajectory_path: str | Path,
    contract_path: str | Path,
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    trust_key_path: str | Path,
    output_dir: str | Path,
    sensor_trust_key_path: str | Path | None = None,
    produced_by: str = "integrity-monitoring-loop",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    snapshot_ttl_seconds: float = EVALUATION_SNAPSHOT_TTL_SECONDS,
    issued_at: str | None = None,
    previous: "MonitoringCycle | None" = None,
    ledger: "AuditLedger | None" = None,
) -> MonitoringCycle:
    """Run one full emission of the continuous-monitoring loop.

    Steps, all over real artifacts: sign the canonical trajectory with the
    producer Ed25519 key, verify the envelope against ``trust_key_path``
    (fail-closed), evaluate the contract deterministically, and emit a signed
    evaluation snapshot with a TTL the guard consumes on its next screening.
    Artifacts are written atomically into ``output_dir``; the snapshot — the
    guard's input — is written last. Returns the cycle record.
    """

    if interval_seconds <= 0:
        raise MonitorRefusalError("parameters_invalid", "interval_seconds must be positive")
    if snapshot_ttl_seconds <= 0:
        raise MonitorRefusalError(
            "parameters_invalid", "snapshot_ttl_seconds must be positive"
        )
    moment = (
        datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        if issued_at
        else datetime.now(timezone.utc)
    )
    if moment.tzinfo is None:
        raise MonitorRefusalError("parameters_invalid", "issued_at must include a timezone")
    issued_iso = moment.isoformat()

    sensor_envelope_verified = False
    if sensor_trust_key_path is not None:
        from autopilot.enforcement import load_signed_trajectory
        from autopilot.trajectory import Trajectory

        trajectory_source_path = resolve_latest_a2_envelope(
            trajectory_path, sensor_trust_key_path=sensor_trust_key_path
        )
        try:
            sensor_envelope = load_signed_trajectory(trajectory_source_path, sensor_trust_key_path)
            trajectory = Trajectory.model_validate(sensor_envelope.payload)
            sensor_envelope_verified = True
        except (OSError, ValueError, DSLValidationError) as exc:
            raise MonitorRefusalError(
                "envelope_not_trusted", f"sensor trajectory envelope is not trusted: {exc}"
            ) from exc
    else:
        trajectory_source_path = Path(trajectory_path)
        if trajectory_source_path.is_dir():
            raise MonitorRefusalError(
                "trajectory_not_accepted",
                "directory trajectory source requires --sensor-trust-key to select the latest signed A2 envelope",
            )
        try:
            trajectory = load_trajectory(trajectory_source_path)
        except DSLValidationError as exc:
            raise MonitorRefusalError(
                "trajectory_not_accepted", f"trajectory is not valid canonical input: {exc}"
            ) from exc

    # A2 harness-derived trajectories carry a signed source marker. Do not let
    # a scheduled loop turn an old static event window into a fresh snapshot.
    # Other legacy trajectory sources retain their existing behavior.
    source = (trajectory.model_extra or {}).get("x_system1_source")
    if isinstance(source, dict) and source.get("source_kind") == "harness":
        if not sensor_envelope_verified:
            raise MonitorRefusalError(
                "observation_not_trusted",
                "harness-derived trajectory requires its signed A2 envelope and sensor trust key",
            )
        heartbeat_at = source.get("heartbeat_at")
        if not isinstance(heartbeat_at, str):
            raise MonitorRefusalError(
                "observation_stale",
                "harness observation window has no valid coverage heartbeat",
            )
        try:
            from autopilot.trajectory import parse_timestamp

            heartbeat = parse_timestamp(heartbeat_at)
        except ValueError as exc:
            raise MonitorRefusalError(
                "observation_stale", "harness observation coverage heartbeat is invalid"
            ) from exc
        age_seconds = (moment - heartbeat).total_seconds()
        if age_seconds < 0 or age_seconds >= snapshot_ttl_seconds:
            detail = "timestamp is in the future" if age_seconds < 0 else f"is {age_seconds:.3f}s old"
            raise MonitorRefusalError(
                "observation_stale",
                f"harness observation {source.get('source_id')}/{source.get('window_id')} {detail}; "
                f"maximum age is {snapshot_ttl_seconds:.3f}s",
            )
        # Bound snapshot validity by the observation's remaining freshness
        # budget. A late cycle must not mint a new TTL starting from `moment`.
        snapshot_ttl_seconds = min(snapshot_ttl_seconds, snapshot_ttl_seconds - age_seconds)

    envelope = sign_trajectory_payload(
        trajectory.model_dump(mode="json"),
        producer_private_key,
        producer_public_key,
        producer=produced_by,
    )
    envelope_verification = verify_trajectory_envelope(envelope, trust_key_path)
    if not envelope_verification.valid:
        raise MonitorRefusalError(
            "envelope_not_trusted",
            "the loop refuses to continue on a self-inconsistent envelope: "
            + "; ".join(envelope_verification.problems),
        )

    try:
        contract = load_contract(contract_path)
    except DSLValidationError as exc:
        raise MonitorRefusalError(
            "contract_not_accepted", f"contract is not valid: {exc}"
        ) from exc
    evaluation = evaluate_contract(contract, trajectory)
    fingerprint = _evaluation_fingerprint(evaluation)
    change = _classify_change(previous, fingerprint)

    seed = _canonical_bytes(
        {
            "fingerprint": fingerprint,
            "issued_at": issued_iso,
            "produced_by": produced_by,
            "trajectory_id": trajectory.trajectory_id,
        }
    )
    cycle_id = f"MONITOR-{hashlib.sha256(seed).hexdigest()[:24].upper()}"

    stage = Path(output_dir)
    envelope_sha = _atomic_write(
        stage / ENVELOPE_FILENAME, json.dumps(envelope.model_dump(mode="json"), indent=2) + "\n"
    )
    evaluation_document = json.dumps(evaluation.model_dump(mode="json"), indent=2) + "\n"
    evaluation_sha = _atomic_write(stage / EVALUATION_FILENAME, evaluation_document)

    snapshot = build_evaluation_snapshot(
        evaluation_source=str(trajectory_source_path),
        recommended_autonomy=evaluation.recommended_autonomy,
        block_release=evaluation.block_release,
        sandbox_evidence_declared=False,
        ttl_seconds=snapshot_ttl_seconds,
        issued_at=issued_iso,
    )
    receipt = sign_evaluation_snapshot(snapshot, producer_private_key, producer_public_key)
    snapshot_verification = verify_evaluation_snapshot(receipt, trust_key_path)
    if not snapshot_verification.valid:
        raise MonitorRefusalError(
            "snapshot_not_verifiable",
            "the loop refuses to hand the guard an unverifiable snapshot: "
            + "; ".join(snapshot_verification.problems),
        )
    snapshot_sha = _atomic_write(
        stage / SNAPSHOT_FILENAME,
        json.dumps(receipt.model_dump(mode="json"), indent=2) + "\n",
    )

    cycle = MonitoringCycle(
        cycle_id=cycle_id,
        produced_by=produced_by,
        issued_at=issued_iso,
        interval_seconds=interval_seconds,
        trajectory_source=str(trajectory_source_path),
        contract_source=str(Path(contract_path)),
        trajectory_id=trajectory.trajectory_id,
        contract_id=evaluation.contract_id,
        contract_version=evaluation.contract_version,
        evaluation_status=evaluation.status.value,
        recommended_autonomy=evaluation.recommended_autonomy.value,
        block_release=evaluation.block_release,
        snapshot_id=snapshot.snapshot_id,
        snapshot_expires_at=snapshot.expires_at,
        envelope_sha256=envelope_sha,
        evaluation_sha256=evaluation_sha,
        snapshot_sha256=snapshot_sha,
        fingerprint=fingerprint,
        previous_fingerprint=previous.fingerprint if previous else None,
        change=change,
    )
    _atomic_write(stage / f"cycle-{cycle_id}.json", cycle.model_dump_json() + "\n")
    _append_cycle_history(stage, cycle)

    if ledger is not None:
        from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

        ledger.append(
            AuditEventType.monitoring_loop_cycle,
            cycle_id,
            issued_iso,
            cycle.model_dump(mode="json"),
        )
    return cycle


def _append_cycle_history(output_dir: Path, cycle: MonitoringCycle) -> Path:
    """Append ``cycle`` to ``cycle-history.jsonl`` inside ``output_dir``."""

    output_dir.mkdir(parents=True, exist_ok=True)
    history_path = output_dir / CYCLE_HISTORY_FILENAME
    with history_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(cycle.model_dump_json() + "\n")
    return history_path


def load_cycle_history(output_dir: str | Path) -> list[MonitoringCycle]:
    """Load the chronological ``MonitoringCycle`` history from ``output_dir``."""

    resolved = Path(output_dir)
    history_path = resolved / CYCLE_HISTORY_FILENAME
    if history_path.is_file():
        cycles: list[MonitoringCycle] = []
        for line_number, line in enumerate(
            history_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                cycles.append(MonitoringCycle.model_validate_json(stripped))
            except Exception as exc:
                raise ValueError(
                    f"{history_path}:{line_number} is not a valid monitoring cycle record: {exc}"
                ) from exc
        if cycles:
            return cycles

    files = sorted(resolved.glob("cycle-*.json"))
    loaded = [load_previous_cycle(path) for path in files]
    loaded.sort(
        key=lambda item: (
            datetime.fromisoformat(item.issued_at.replace("Z", "+00:00")),
            item.cycle_id,
        )
    )
    return loaded


def find_latest_cycle_in_dir(output_dir: str | Path) -> MonitoringCycle | None:
    """Return the most recent ``MonitoringCycle`` in ``output_dir``, or ``None``."""

    resolved = Path(output_dir)
    if not resolved.exists():
        return None
    files = sorted(resolved.glob("cycle-*.json"))
    if not files:
        history_path = resolved / CYCLE_HISTORY_FILENAME
        if not history_path.exists():
            return None
    cycles = load_cycle_history(resolved)
    return cycles[-1] if cycles else None


def resolve_latest_a2_envelope(
    source_path: str | Path,
    *,
    sensor_trust_key_path: str | Path,
) -> Path:
    """Resolve ``source_path`` to a file or the newest verified A2 envelope in a directory.

    When ``source_path`` is a file, returns it directly so ``run_monitoring_cycle``
    validates it as usual. When ``source_path`` is a directory, scans ``*.json``
    files (excluding monitoring loop cycle/evaluation/snapshot outputs), verifies
    each candidate against ``sensor_trust_key_path``, requires an A2
    ``x_system1_source`` with a valid ``heartbeat_at``, and returns the envelope
    with the freshest heartbeat (tie-breaking by ``evaluated_at`` and filename).
    Fails closed with ``MonitorRefusalError("envelope_not_trusted", ...)`` when
    no trusted A2 envelope is found.
    """

    from autopilot.enforcement import load_signed_trajectory
    from autopilot.trajectory import Trajectory, parse_timestamp

    resolved = Path(source_path)
    if not resolved.exists():
        raise MonitorRefusalError(
            "envelope_not_trusted", f"sensor trajectory source does not exist: {resolved}"
        )
    if not resolved.is_dir():
        return resolved

    excluded = {ENVELOPE_FILENAME, EVALUATION_FILENAME, SNAPSHOT_FILENAME}
    candidates: list[tuple[datetime, datetime, str, Path]] = []
    last_error: str | None = None
    for candidate in sorted(resolved.glob("*.json")):
        if candidate.name in excluded or candidate.name.startswith("cycle-"):
            continue
        try:
            envelope = load_signed_trajectory(candidate, sensor_trust_key_path)
            trajectory = Trajectory.model_validate(envelope.payload)
        except Exception as exc:
            last_error = f"{candidate.name}: {exc}"
            continue
        source = (trajectory.model_extra or {}).get("x_system1_source")
        if not isinstance(source, dict) or source.get("source_kind") != "harness":
            last_error = f"{candidate.name}: missing harness x_system1_source"
            continue
        heartbeat_raw = source.get("heartbeat_at")
        if not isinstance(heartbeat_raw, str):
            last_error = f"{candidate.name}: missing coverage heartbeat_at"
            continue
        try:
            heartbeat_dt = parse_timestamp(heartbeat_raw)
            evaluated_dt = parse_timestamp(trajectory.evaluated_at)
        except ValueError as exc:
            last_error = f"{candidate.name}: invalid timestamp ({exc})"
            continue
        candidates.append((heartbeat_dt, evaluated_dt, candidate.name, candidate))

    if not candidates:
        detail = f" (last error: {last_error})" if last_error else ""
        raise MonitorRefusalError(
            "envelope_not_trusted",
            f"no trusted A2 trajectory envelope found in {resolved}{detail}",
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[-1][3]


def run_scheduled_monitoring_loop(
    *,
    trajectory_source: str | Path,
    contract_path: str | Path,
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    trust_key_path: str | Path,
    output_dir: str | Path,
    sensor_trust_key_path: str | Path | None = None,
    produced_by: str = "integrity-monitoring-loop",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    snapshot_ttl_seconds: float = EVALUATION_SNAPSHOT_TTL_SECONDS,
    max_cycles: int = 1,
    previous: MonitoringCycle | None = None,
    ledger: "AuditLedger | None" = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] | None = None,
    after_cycle: Callable[[MonitoringCycle], None] | None = None,
) -> list[MonitoringCycle]:
    """Run ``max_cycles`` scheduled emissions every ``interval_seconds`` (default: 300s / 5 min).

    On each tick:
    1. Resolves the latest A2 envelope when ``trajectory_source`` is a directory.
    2. Chains ``previous`` automatically from ``output_dir`` if not explicitly provided.
    3. Emits the cycle artifacts, appends to ``cycle-history.jsonl`` and records in ``ledger``.
    4. Runs ``after_cycle`` (when supplied) against the cycle just emitted, which is how
       the drift watch is chained without this module importing the drift stack.
    5. Sleeps ``interval_seconds`` before the next tick (except after the final cycle).
    """

    if max_cycles < 1:
        raise MonitorRefusalError("parameters_invalid", "max_cycles must be at least 1")
    if interval_seconds <= 0:
        raise MonitorRefusalError("parameters_invalid", "interval_seconds must be positive")

    stage = Path(output_dir)
    current_previous = previous
    if current_previous is None:
        try:
            current_previous = find_latest_cycle_in_dir(stage)
        except (OSError, ValueError) as exc:
            raise MonitorRefusalError("previous_cycle_invalid", str(exc)) from exc

    emitted: list[MonitoringCycle] = []
    for index in range(max_cycles):
        issued_at = now_fn().isoformat() if now_fn is not None else None
        cycle = run_monitoring_cycle(
            trajectory_path=trajectory_source,
            contract_path=contract_path,
            producer_private_key=producer_private_key,
            producer_public_key=producer_public_key,
            trust_key_path=trust_key_path,
            output_dir=stage,
            sensor_trust_key_path=sensor_trust_key_path,
            produced_by=produced_by,
            interval_seconds=interval_seconds,
            snapshot_ttl_seconds=snapshot_ttl_seconds,
            issued_at=issued_at,
            previous=current_previous,
            ledger=ledger,
        )
        emitted.append(cycle)
        if after_cycle is not None:
            after_cycle(cycle)
        current_previous = cycle
        if index + 1 < max_cycles:
            sleep_fn(interval_seconds)
    return emitted


def load_previous_cycle(path: str | Path) -> MonitoringCycle:
    """Load the previous cycle record (for fingerprint continuity)."""

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        return MonitoringCycle.model_validate(document)
    except Exception as exc:
        raise ValueError(f"{path} is not a monitoring cycle record: {exc}") from exc
