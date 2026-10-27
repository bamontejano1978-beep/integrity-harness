"""Longitudinal technical-evidence monitoring for AI Act readiness.

This module does not determine legal compliance. It turns versioned agent
evaluations and declared evidence into reproducible operational gap signals.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from autopilot.contracts.models import AutonomyLevel
from autopilot.trajectory import parse_timestamp

MONITOR_NAME = "ai_act_longitudinal_monitor"
MONITOR_VERSION = "0.1.0"

_AUTONOMY_ORDER = {
    AutonomyLevel.COACH: 0,
    AutonomyLevel.SUPERVISE: 1,
    AutonomyLevel.RESTRICT: 2,
    AutonomyLevel.QUARANTINE: 3,
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportedModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PreliminaryRole(str, Enum):
    provider = "provider"
    deployer = "deployer"
    importer = "importer"
    distributor = "distributor"
    product_manufacturer = "product_manufacturer"
    mixed = "mixed"
    uncertain = "uncertain"


class PreliminaryCategory(str, Enum):
    prohibited_practice_risk = "prohibited_practice_risk"
    high_risk_candidate = "high_risk_candidate"
    transparency_obligation = "transparency_obligation"
    gpai_dependency = "gpai_dependency"
    low_minimal_risk = "low_minimal_risk"
    unknown = "unknown"


class EvidenceStatus(str, Enum):
    evidence_present = "evidence-present"
    evidence_missing = "evidence-missing"
    likely_operational_gap = "likely-operational-gap"
    requires_human_review = "requires-human-review"


class Trend(str, Enum):
    improving = "improving"
    stable = "stable"
    degrading = "degrading"
    baseline = "baseline"


class AgentVersion(StrictModel):
    version: str = Field(min_length=1, max_length=80)
    released_at: str
    changes: list[str] = Field(default_factory=list)
    reevaluated: bool = False
    sandbox_evidence: bool = False
    human_review: bool = False

    _timestamp = field_validator("released_at")(lambda value: _validated_timestamp(value))


class EvidenceItem(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=120)
    ref: str = Field(min_length=1, max_length=500)
    observed_at: str
    version: str | None = None
    obligation_ids: list[str] = Field(default_factory=list)

    _timestamp = field_validator("observed_at")(lambda value: _validated_timestamp(value))


class SystemProfile(StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    system_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=160)
    intended_purpose: str = Field(min_length=1, max_length=1000)
    sector: str = Field(min_length=1, max_length=120)
    owner_human: str = Field(min_length=1, max_length=160)
    preliminary_role: PreliminaryRole
    preliminary_category: PreliminaryCategory
    tools: list[str] = Field(default_factory=list)
    data_categories: list[str] = Field(default_factory=list)
    versions: list[AgentVersion] = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_version_and_evidence_ids(self) -> SystemProfile:
        versions = [item.version for item in self.versions]
        if len(versions) != len(set(versions)):
            raise ValueError("version identifiers must be unique")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence identifiers must be unique")
        return self


class ObligationApplicability(StrictModel):
    roles: list[PreliminaryRole] = Field(default_factory=list)
    categories: list[PreliminaryCategory] = Field(default_factory=list)


class ReadinessObligation(StrictModel):
    obligation_id: str = Field(min_length=1, max_length=160)
    area: str = Field(min_length=1, max_length=120)
    article_hint: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=1000)
    evidence_required: list[str] = Field(min_length=1)
    source_clause_ids: list[str] = Field(default_factory=list)
    applicability: ObligationApplicability = Field(default_factory=ObligationApplicability)
    manual_review_required: bool = False
    owner: str = Field(min_length=1, max_length=160)
    # v0.2 methodology traceability: sandbox guide slugs and automation hint.
    # Optional and additive: v0.1 maps keep parsing with defaults.
    source_guides: list[str] = Field(default_factory=list)
    automatable: Literal["high", "medium", "manual"] | None = None
    # v0.3 completeness columns: what proves the obligation, what that proof
    # cannot carry, and what is still owed. Optional so v0.1/v0.2 maps keep
    # parsing unchanged.
    test: str | None = Field(default=None, min_length=1, max_length=2000)
    limitations: str | None = Field(default=None, min_length=1, max_length=2000)
    pending_action: str | None = Field(default=None, min_length=1, max_length=2000)


class ObligationMap(StrictModel):
    # 0.3 adds the three completeness columns per obligation (test,
    # limitations, pending_action); earlier maps keep parsing unchanged.
    schema_version: Literal["0.1", "0.2", "0.3"] = "0.1"
    legal_reference: str
    disclaimer: str
    obligations: list[ReadinessObligation] = Field(min_length=1)
    # v0.2: optional methodological provenance of the map itself.
    methodology_reference: str | None = None

    @model_validator(mode="after")
    def unique_obligation_ids(self) -> ObligationMap:
        identifiers = [item.obligation_id for item in self.obligations]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("obligation identifiers must be unique")
        return self


class ImportedClauseResult(ImportedModel):
    clause_id: str
    status: Literal["pass", "fail", "indeterminate", "not_applicable"]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    recommended_autonomy: AutonomyLevel


class ImportedEvaluation(ImportedModel):
    source_path: str
    contract_id: str
    contract_version: str
    trajectory_id: str
    candidate_version: str
    status: Literal["pass", "fail", "indeterminate", "not_applicable"]
    evaluated_at: str
    recommended_autonomy: AutonomyLevel
    block_release: bool
    clause_results: list[ImportedClauseResult]


class ObligationAssessment(StrictModel):
    obligation_id: str
    area: str
    article_hint: str
    question: str
    status: EvidenceStatus
    evidence_present: list[str]
    evidence_missing: list[str]
    failed_clauses: list[str]
    latest_run_at: str | None
    owner: str
    manual_review_required: bool
    source_guides: list[str] = Field(default_factory=list)
    automatable: Literal["high", "medium", "manual"] | None = None
    test: str | None = None
    limitations: str | None = None
    pending_action: str | None = None


class MonitorAlert(StrictModel):
    alert_id: str
    severity: Literal["medium", "high", "critical"]
    kind: str
    version: str
    message: str
    requires_human_review: bool = True
    requires_legal_review: bool = False


class VersionSnapshot(StrictModel):
    version: str
    released_at: str
    run_count: int
    pass_count: int
    fail_count: int
    coverage_percent: int = Field(ge=0, le=100)
    operational_drift_index: int = Field(ge=0, le=100)
    trend: Trend
    recommended_autonomy: AutonomyLevel
    obligation_assessments: list[ObligationAssessment]
    alerts: list[MonitorAlert]


class LongitudinalReport(StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    report_id: str
    generated_at: str
    monitor_name: str = MONITOR_NAME
    monitor_version: str = MONITOR_VERSION
    legal_status: Literal["technical_readiness_evidence_only"] = (
        "technical_readiness_evidence_only"
    )
    disclaimer: str
    system_id: str
    system_name: str
    provider: str
    preliminary_role: PreliminaryRole
    preliminary_category: PreliminaryCategory
    current_version: str
    current_autonomy: AutonomyLevel
    current_drift_index: int
    current_coverage_percent: int
    timeline: list[VersionSnapshot]
    current_alerts: list[MonitorAlert]
    source_evaluations: list[str]


def _validated_timestamp(value: str) -> str:
    parse_timestamp(value)
    return value


def _load_yaml(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    with resolved.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{resolved} must contain a YAML mapping")
    return payload


def load_system_profile(path: str | Path) -> SystemProfile:
    return SystemProfile.model_validate(_load_yaml(path))


def load_obligation_map(path: str | Path) -> ObligationMap:
    return ObligationMap.model_validate(_load_yaml(path))


def _parse_evaluation(path: Path) -> ImportedEvaluation | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    required = {
        "contract_id",
        "contract_version",
        "trajectory_id",
        "candidate_version",
        "status",
        "evaluated_at",
        "recommended_autonomy",
        "block_release",
        "clause_results",
    }
    if not isinstance(raw, dict) or not required.issubset(raw):
        return None
    try:
        return ImportedEvaluation.model_validate({"source_path": str(path), **raw})
    except ValidationError:
        return None


def load_evaluations(paths: list[str | Path]) -> list[ImportedEvaluation]:
    candidates: set[Path] = set()
    for path_like in paths:
        path = Path(path_like)
        if path.is_dir():
            candidates.update(path.rglob("*.json"))
        else:
            parsed = _parse_evaluation(path)
            if parsed is None:
                raise ValueError(f"not a supported evaluation JSON: {path}")
            candidates.add(path)
    evaluations = [parsed for path in sorted(candidates) if (parsed := _parse_evaluation(path))]
    evaluations.sort(key=lambda item: (parse_timestamp(item.evaluated_at), item.source_path))
    return evaluations


def _applies(obligation: ReadinessObligation, profile: SystemProfile) -> bool:
    roles = obligation.applicability.roles
    categories = obligation.applicability.categories
    return (not roles or profile.preliminary_role in roles) and (
        not categories or profile.preliminary_category in categories
    )


def _alert_id(system_id: str, version: str, kind: str, message: str) -> str:
    digest = hashlib.sha256(f"{system_id}|{version}|{kind}|{message}".encode()).hexdigest()
    return f"ALT-{digest[:16].upper()}"


def _least_permissive(levels: list[AutonomyLevel]) -> AutonomyLevel:
    return max(levels, key=_AUTONOMY_ORDER.__getitem__, default=AutonomyLevel.COACH)


def _assess_obligation(
    obligation: ReadinessObligation,
    profile: SystemProfile,
    version: AgentVersion,
    evaluations: list[ImportedEvaluation],
) -> ObligationAssessment:
    relevant_items = [
        item
        for item in profile.evidence
        if (item.version in {None, version.version})
        and (not item.obligation_ids or obligation.obligation_id in item.obligation_ids)
    ]
    available_kinds = {item.kind for item in relevant_items}
    evidence_refs = [item.ref for item in relevant_items]
    mapped_results = [
        result
        for evaluation in evaluations
        for result in evaluation.clause_results
        if result.clause_id in obligation.source_clause_ids
    ]
    if mapped_results:
        available_kinds.add("scenario_result")
        evidence_refs.extend(
            f"{evaluation.trajectory_id}:{result.clause_id}"
            for evaluation in evaluations
            for result in evaluation.clause_results
            if result.clause_id in obligation.source_clause_ids
        )
    if any(result.evidence for result in mapped_results):
        available_kinds.add("audit_trace")
    failed_clauses = sorted(
        {result.clause_id for result in mapped_results if result.status == "fail"}
    )
    if failed_clauses:
        available_kinds.add("incident_record")
    if version.sandbox_evidence:
        available_kinds.add("sandbox_report")
    if version.human_review:
        available_kinds.add("human_review")
    missing = sorted(set(obligation.evidence_required) - available_kinds)
    latest_run = max((item.evaluated_at for item in evaluations), default=None)
    has_human_review = version.human_review or bool(
        {"human_review", "human_approval"} & available_kinds
    )

    if failed_clauses:
        status = EvidenceStatus.likely_operational_gap
    elif missing:
        status = EvidenceStatus.evidence_missing
    elif obligation.manual_review_required and not has_human_review:
        status = EvidenceStatus.requires_human_review
    else:
        status = EvidenceStatus.evidence_present

    return ObligationAssessment(
        obligation_id=obligation.obligation_id,
        area=obligation.area,
        article_hint=obligation.article_hint,
        question=obligation.question,
        status=status,
        evidence_present=sorted(set(evidence_refs)),
        evidence_missing=missing,
        failed_clauses=failed_clauses,
        latest_run_at=latest_run,
        owner=obligation.owner,
        manual_review_required=obligation.manual_review_required,
        source_guides=list(obligation.source_guides),
        automatable=obligation.automatable,
        test=obligation.test,
        limitations=obligation.limitations,
        pending_action=obligation.pending_action,
    )


def _drift_index(
    assessments: list[ObligationAssessment],
    *,
    reevaluated: bool,
    repeated_failure_count: int,
) -> int:
    weights = {
        EvidenceStatus.evidence_present: 0,
        EvidenceStatus.requires_human_review: 25,
        EvidenceStatus.evidence_missing: 55,
        EvidenceStatus.likely_operational_gap: 100,
    }
    base = sum(weights[item.status] for item in assessments) / max(len(assessments), 1)
    repeat_penalty = min(repeated_failure_count * 5, 15)
    reevaluation_penalty = 0 if reevaluated else 20
    return min(100, round(base + repeat_penalty + reevaluation_penalty))


def build_longitudinal_report(
    profile: SystemProfile,
    obligation_map: ObligationMap,
    evaluations: list[ImportedEvaluation],
    *,
    generated_at: str | None = None,
) -> LongitudinalReport:
    relevant_obligations = [
        obligation for obligation in obligation_map.obligations if _applies(obligation, profile)
    ]
    versions = sorted(profile.versions, key=lambda item: parse_timestamp(item.released_at))
    timeline: list[VersionSnapshot] = []
    previous_drift: int | None = None
    previous_autonomy = AutonomyLevel.COACH

    for version in versions:
        version_evaluations = [
            evaluation
            for evaluation in evaluations
            if evaluation.candidate_version == version.version
        ]
        assessments = [
            _assess_obligation(obligation, profile, version, version_evaluations)
            for obligation in relevant_obligations
        ]
        alerts: list[MonitorAlert] = []
        evaluated = version.reevaluated and bool(version_evaluations)
        if not evaluated:
            message = "Version change has no completed behavioral reevaluation."
            alerts.append(
                MonitorAlert(
                    alert_id=_alert_id(profile.system_id, version.version, "unreviewed_version", message),
                    severity="critical",
                    kind="unreviewed_version",
                    version=version.version,
                    message=message,
                )
            )

        failed_ids = [
            result.clause_id
            for evaluation in version_evaluations
            for result in evaluation.clause_results
            if result.status == "fail"
        ]
        for clause_id in sorted(set(failed_ids)):
            count = failed_ids.count(clause_id)
            if count > 1:
                message = f"Clause {clause_id} failed in {count} runs for this version."
                alerts.append(
                    MonitorAlert(
                        alert_id=_alert_id(
                            profile.system_id, version.version, "repeated_incident", message
                        ),
                        severity="high",
                        kind="repeated_incident",
                        version=version.version,
                        message=message,
                    )
                )

        autonomy = _least_permissive(
            [evaluation.recommended_autonomy for evaluation in version_evaluations]
        )
        if not evaluated:
            autonomy = _least_permissive([autonomy, AutonomyLevel.QUARANTINE])
        elif any(item.status is EvidenceStatus.evidence_missing for item in assessments):
            autonomy = _least_permissive([autonomy, AutonomyLevel.SUPERVISE])

        if (
            timeline
            and _AUTONOMY_ORDER[autonomy] < _AUTONOMY_ORDER[previous_autonomy]
            and not version.sandbox_evidence
        ):
            message = "Autonomy would increase without declared sandbox recovery evidence."
            alerts.append(
                MonitorAlert(
                    alert_id=_alert_id(
                        profile.system_id, version.version, "recovery_without_sandbox", message
                    ),
                    severity="critical",
                    kind="recovery_without_sandbox",
                    version=version.version,
                    message=message,
                )
            )
            autonomy = previous_autonomy

        if profile.preliminary_role is PreliminaryRole.uncertain or (
            profile.preliminary_category is PreliminaryCategory.unknown
        ):
            message = "Preliminary role or risk category is unresolved."
            alerts.append(
                MonitorAlert(
                    alert_id=_alert_id(
                        profile.system_id, version.version, "classification_review", message
                    ),
                    severity="high",
                    kind="classification_review",
                    version=version.version,
                    message=message,
                    requires_legal_review=True,
                )
            )

        present = sum(
            assessment.status is EvidenceStatus.evidence_present for assessment in assessments
        )
        coverage = round(100 * present / max(len(assessments), 1))
        drift = _drift_index(
            assessments,
            reevaluated=evaluated,
            repeated_failure_count=len(failed_ids) - len(set(failed_ids)),
        )
        if previous_drift is None:
            trend = Trend.baseline
        elif drift < previous_drift:
            trend = Trend.improving
        elif drift > previous_drift:
            trend = Trend.degrading
        else:
            trend = Trend.stable

        timeline.append(
            VersionSnapshot(
                version=version.version,
                released_at=version.released_at,
                run_count=len(version_evaluations),
                pass_count=sum(item.status == "pass" for item in version_evaluations),
                fail_count=sum(item.status == "fail" for item in version_evaluations),
                coverage_percent=coverage,
                operational_drift_index=drift,
                trend=trend,
                recommended_autonomy=autonomy,
                obligation_assessments=assessments,
                alerts=alerts,
            )
        )
        previous_drift = drift
        previous_autonomy = autonomy

    generated = generated_at or datetime.now(timezone.utc).isoformat()
    report_digest = hashlib.sha256(
        f"{profile.system_id}|{versions[-1].version}|{generated}".encode()
    ).hexdigest()
    current = timeline[-1]
    return LongitudinalReport(
        report_id=f"AIM-{report_digest[:20].upper()}",
        generated_at=generated,
        disclaimer=obligation_map.disclaimer,
        system_id=profile.system_id,
        system_name=profile.name,
        provider=profile.provider,
        preliminary_role=profile.preliminary_role,
        preliminary_category=profile.preliminary_category,
        current_version=current.version,
        current_autonomy=current.recommended_autonomy,
        current_drift_index=current.operational_drift_index,
        current_coverage_percent=current.coverage_percent,
        timeline=timeline,
        current_alerts=current.alerts,
        source_evaluations=[item.source_path for item in evaluations],
    )


def render_markdown(report: LongitudinalReport) -> str:
    current = report.timeline[-1]
    lines = [
        f"# AI Act technical readiness - {report.system_name}",
        "",
        f"- Report: `{report.report_id}`",
        f"- Current version: `{report.current_version}`",
        f"- Preliminary role: `{report.preliminary_role.value}`",
        f"- Preliminary category: `{report.preliminary_category.value}`",
        f"- Recommended autonomy: `{report.current_autonomy.value}`",
        f"- Operational drift index: `{report.current_drift_index}/100`",
        f"- Evidence coverage: `{report.current_coverage_percent}%`",
        "",
        "> This is technical readiness evidence, not legal advice, conformity assessment, CE marking, or certification.",
        "",
        "## Current readiness matrix",
        "",
        "| Area | Evidence status | Missing | Failed clauses | Owner |",
        "|---|---|---|---|---|",
    ]
    for item in current.obligation_assessments:
        missing = ", ".join(item.evidence_missing) or "-"
        failed = ", ".join(item.failed_clauses) or "-"
        lines.append(
            f"| {item.area} | {item.status.value} | {missing} | {failed} | {item.owner} |"
        )
    fichas = [
        item
        for item in current.obligation_assessments
        if item.test or item.limitations or item.pending_action
    ]
    if fichas:
        lines.extend(["", "## Fichas por obligación", ""])
        for item in fichas:
            lines.append(f"### `{item.obligation_id}`")
            lines.append("")
            lines.append(f"- Control: {item.question}")
            if item.test:
                lines.append(f"- Prueba: {item.test}")
            if item.limitations:
                lines.append(f"- Limitaciones: {item.limitations}")
            if item.pending_action:
                lines.append(f"- Acción pendiente: {item.pending_action}")
            lines.append("")
    lines.extend(["", "## Timeline", "", "| Version | Runs | Drift | Trend | Autonomy |", "|---|---:|---:|---|---|"])
    for snapshot in report.timeline:
        lines.append(
            f"| {snapshot.version} | {snapshot.run_count} | "
            f"{snapshot.operational_drift_index} | {snapshot.trend.value} | "
            f"{snapshot.recommended_autonomy.value} |"
        )
    lines.extend(["", "## Alerts", ""])
    if report.current_alerts:
        for alert in report.current_alerts:
            suffix = " Legal review required." if alert.requires_legal_review else ""
            lines.append(f"- **{alert.severity.upper()}** `{alert.kind}`: {alert.message}{suffix}")
    else:
        lines.append("- No current automated alerts.")
    lines.extend(["", "## Limits", "", report.disclaimer, ""])
    return "\n".join(lines)


def _write_csv(report: LongitudinalReport, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "version",
                "released_at",
                "area",
                "obligation_id",
                "status",
                "missing_evidence",
                "failed_clauses",
                "owner",
                "autonomy",
                "drift_index",
                "source_guides",
                "automatable",
                "test",
                "limitations",
                "pending_action",
            ]
        )
        for snapshot in report.timeline:
            for item in snapshot.obligation_assessments:
                writer.writerow(
                    [
                        snapshot.version,
                        snapshot.released_at,
                        item.area,
                        item.obligation_id,
                        item.status.value,
                        ";".join(item.evidence_missing),
                        ";".join(item.failed_clauses),
                        item.owner,
                        snapshot.recommended_autonomy.value,
                        snapshot.operational_drift_index,
                        ";".join(item.source_guides),
                        item.automatable or "",
                        item.test or "",
                        item.limitations or "",
                        item.pending_action or "",
                    ]
                )


def render_dashboard(report: LongitudinalReport) -> str:
    payload = report.model_dump_json().replace("</", "<\\/")
    title = html.escape(report.system_name)
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AI Evidence Monitor - {title}</title>
  <style>
    :root{{--ink:#172126;--muted:#647279;--paper:#f6f7f4;--panel:#fff;--line:#d9dfdc;--green:#16745a;--amber:#a45d12;--red:#b43b35;--blue:#275d86}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif;letter-spacing:0}}
    header{{background:#132126;color:#fff;border-bottom:4px solid #42a77c}} .bar{{max-width:1240px;margin:auto;padding:22px 28px;display:flex;justify-content:space-between;gap:20px;align-items:end}}
    h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:0}} p{{margin:0}} .eyebrow{{color:#9fcab7;font-weight:700;text-transform:uppercase;font-size:11px}}
    main{{max-width:1240px;margin:auto;padding:24px 28px 48px}} .metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px}}
    .metric,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:6px}} .metric{{padding:16px;min-height:96px}} .metric span{{display:block;color:var(--muted);font-size:12px;margin-bottom:8px}} .metric strong{{font-size:24px}}
    .layout{{display:grid;grid-template-columns:1.5fr .8fr;gap:16px}} .panel{{padding:18px;margin-bottom:16px;overflow:hidden}} .panel-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}}
    select{{border:1px solid var(--line);background:#fff;padding:7px 32px 7px 9px;border-radius:4px;color:var(--ink)}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;border-bottom:1px solid var(--line);padding:10px 8px;vertical-align:top}} th{{font-size:11px;color:var(--muted);text-transform:uppercase}}
    .status{{display:inline-flex;align-items:center;white-space:nowrap;font-size:11px;font-weight:700;padding:3px 7px;border-radius:4px;background:#edf1ef}} .present{{color:var(--green);background:#e5f4ed}} .gap{{color:var(--red);background:#fae8e7}} .missing,.review{{color:var(--amber);background:#fff0dc}}
    .autonomy{{color:#fff;background:var(--red);padding:5px 9px;border-radius:4px;font-size:12px}} .timeline{{display:flex;gap:8px;align-items:flex-end;height:160px;border-bottom:1px solid var(--line);padding:8px 4px 0}}
    .point{{flex:1;text-align:center;min-width:48px}} .column{{background:var(--blue);min-height:3px;border-radius:3px 3px 0 0}} .point b,.point small{{display:block;margin-top:5px}} .point small{{color:var(--muted)}}
    .alert{{border-left:3px solid var(--amber);padding:9px 10px;background:#fff8ed;margin:8px 0}} .alert.critical{{border-color:var(--red);background:#fdf0ef}} .empty{{color:var(--muted);padding:18px 0}}
    .fine{{color:var(--muted);font-size:12px;margin-top:20px;max-width:850px}} .changes{{color:var(--muted);font-size:12px;max-width:260px}}
    .doc{{color:var(--muted);font-size:12px;margin-top:6px}} .doc summary{{cursor:pointer;color:var(--ink)}} .doc p{{margin:4px 0}}
    @media(max-width:850px){{.metrics{{grid-template-columns:1fr 1fr}}.layout{{grid-template-columns:1fr}}.bar{{align-items:start;flex-direction:column}}}}
    @media(max-width:520px){{main,.bar{{padding-left:16px;padding-right:16px}}.metrics{{grid-template-columns:1fr}}.table-wrap{{overflow-x:auto}}}}
  </style>
</head>
<body>
<header><div class="bar"><div><div class="eyebrow">Behavioural Autopilot</div><h1>AI Evidence Monitor</h1><p>{title} · vigilancia longitudinal</p></div><span id="generated"></span></div></header>
<main>
  <section class="metrics">
    <div class="metric"><span>Versión observada</span><strong id="version"></strong></div>
    <div class="metric"><span>Cobertura de evidencia</span><strong id="coverage"></strong></div>
    <div class="metric"><span>Índice de deriva operativa</span><strong id="drift"></strong></div>
    <div class="metric"><span>Autonomía recomendada</span><strong class="autonomy" id="autonomy"></strong></div>
  </section>
  <div class="layout"><div>
    <section class="panel"><div class="panel-head"><h2>Matriz de evidencia</h2><select id="versionSelect" aria-label="Seleccionar versión"></select></div><div class="table-wrap"><table><thead><tr><th>Área</th><th>Estado</th><th>Hallazgo</th><th>Owner</th></tr></thead><tbody id="matrix"></tbody></table></div></section>
    <section class="panel"><div class="panel-head"><h2>Histórico de deriva</h2><span class="changes">0 = sin gaps detectados · 100 = degradación máxima</span></div><div class="timeline" id="timeline"></div></section>
  </div><aside>
    <section class="panel"><div class="panel-head"><h2>Alertas</h2><span id="alertCount"></span></div><div id="alerts"></div></section>
    <section class="panel"><h2>Contexto preliminar</h2><table><tr><th>Proveedor</th><td>{html.escape(report.provider)}</td></tr><tr><th>Rol</th><td>{report.preliminary_role.value}</td></tr><tr><th>Categoría</th><td>{report.preliminary_category.value}</td></tr></table></section>
  </aside></div>
  <p class="fine">Evidencia técnica de preparación regulatoria. No constituye asesoramiento jurídico, evaluación de conformidad, marcado CE ni certificación.</p>
</main>
<script id="report-data" type="application/json">{payload}</script>
<script>
const report=JSON.parse(document.getElementById('report-data').textContent); const $=id=>document.getElementById(id);
const esc=value=>String(value).replace(/[&<>"']/g,char=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
const cls=s=>s==='evidence-present'?'present':s==='likely-operational-gap'?'gap':s==='requires-human-review'?'review':'missing';
function render(snapshot){{
 $('version').textContent=snapshot.version; $('coverage').textContent=snapshot.coverage_percent+'%'; $('drift').textContent=snapshot.operational_drift_index+'/100'; $('autonomy').textContent=snapshot.recommended_autonomy;
 $('matrix').innerHTML=snapshot.obligation_assessments.map(o=>{{const finding=o.failed_clauses.length?'Falla: '+o.failed_clauses.join(', '):o.evidence_missing.length?'Falta: '+o.evidence_missing.join(', '):'Evidencia disponible';const ficha=(o.test||o.limitations||o.pending_action)?`<details class="doc"><summary>Ficha</summary>${{o.test?`<p><b>Prueba:</b> ${{esc(o.test)}}</p>`:''}}${{o.limitations?`<p><b>Limitaciones:</b> ${{esc(o.limitations)}}</p>`:''}}${{o.pending_action?`<p><b>Pendiente:</b> ${{esc(o.pending_action)}}</p>`:''}}</details>`:'';return `<tr><td><b>${{esc(o.area.replaceAll('_',' '))}}</b><br><small>${{esc(o.article_hint)}}</small></td><td><span class="status ${{cls(o.status)}}">${{esc(o.status)}}</span></td><td>${{esc(finding)}}${{ficha}}</td><td>${{esc(o.owner)}}</td></tr>`}}).join('');
 $('alertCount').textContent=snapshot.alerts.length; $('alerts').innerHTML=snapshot.alerts.length?snapshot.alerts.map(a=>`<div class="alert ${{a.severity}}"><b>${{esc(a.kind.replaceAll('_',' '))}}</b><p>${{esc(a.message)}}</p></div>`).join(''):'<div class="empty">Sin alertas automáticas para esta versión.</div>';
}}
$('generated').textContent='Informe '+report.report_id; $('versionSelect').innerHTML=report.timeline.map((s,i)=>`<option value="${{i}}">${{esc(s.version)}}</option>`).join(''); $('versionSelect').value=report.timeline.length-1; $('versionSelect').addEventListener('change',e=>render(report.timeline[Number(e.target.value)]));
$('timeline').innerHTML=report.timeline.map(s=>`<div class="point"><div class="column" style="height:${{Math.max(s.operational_drift_index,3)}}%" title="Deriva ${{s.operational_drift_index}}"></div><b>${{esc(s.version)}}</b><small>${{esc(s.trend)}}</small></div>`).join(''); render(report.timeline.at(-1));
</script>
</body></html>"""


def write_report_bundle(report: LongitudinalReport, output_dir: str | Path) -> dict[str, str]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": target / "ai_act_readiness_report.json",
        "markdown": target / "ai_act_readiness_report.md",
        "csv": target / "ai_act_readiness_matrix.csv",
        "dashboard": target / "index.html",
    }
    paths["json"].write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    _write_csv(report, paths["csv"])
    paths["dashboard"].write_text(render_dashboard(report), encoding="utf-8")
    return {name: str(path) for name, path in paths.items()}
