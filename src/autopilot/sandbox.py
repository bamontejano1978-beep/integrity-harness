"""Deterministic sandbox verification for human-approved change candidates."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import pydantic_issues
from autopilot.contracts.models import ContractDocument, RecoveryClause, SNAKE_NAME_PATTERN
from autopilot.diagnostics import ChangeCard, CorrectionCandidate
from autopilot.evaluator import ClauseStatus, ContractEvaluation, evaluate_contract
from autopilot.trajectory import Trajectory, parse_timestamp

SANDBOX_NAME = "deterministic_candidate_sandbox"
SANDBOX_VERSION = "0.1.0"


class SandboxStatus(str, Enum):
    passed = "pass"
    failed = "fail"
    indeterminate = "indeterminate"


class SandboxApproval(BaseModel):
    """Human authorization to run tests, explicitly not authorization to release."""

    model_config = ConfigDict(extra="forbid")

    approved: Literal[True] = True
    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    approved_at: str
    scope: Literal["sandbox_evaluation"] = "sandbox_evaluation"
    does_not_authorize_release: Literal[True] = True

    @field_validator("approved_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class ClauseCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clause_id: str
    role: Literal["target", "no_regression"]
    status: ClauseStatus
    accepted_for_run: bool
    reason: str


class SandboxRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_number: int = Field(ge=1)
    trajectory_id: str
    candidate_version: str
    evaluated_at: str
    status: SandboxStatus
    cis: int | None = Field(default=None, ge=0, le=100)
    rollback_available: bool | None
    clause_checks: list[ClauseCheck]
    evaluation: ContractEvaluation


class AcceptanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str
    status: SandboxStatus
    reason: str
    evidence_refs: list[str]


class SandboxReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    report_id: str
    generated_at: str
    contract_id: str
    contract_version: str
    card_id: str
    incident_id: str
    candidate_version: str
    selected_candidate: CorrectionCandidate
    sandbox_approval: SandboxApproval
    required_consecutive_passing_runs: int = Field(ge=1)
    acceptance_window_trajectory_ids: list[str]
    runs: list[SandboxRun]
    acceptance_checks: list[AcceptanceCheck]
    decision: SandboxStatus
    eligible_for_release_review: bool
    release_allowed: Literal[False] = False
    automatic_application: Literal[False] = False
    automatic_release: Literal[False] = False
    human_release_approval_required: Literal[True] = True
    sandbox_name: str = SANDBOX_NAME
    sandbox_version: str = SANDBOX_VERSION
    disclaimer: str = (
        "A passing sandbox report only permits human release review. "
        "It does not apply a change, restore autonomy, or authorize release."
    )


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_change_card(path: str | Path) -> ChangeCard:
    """Load a generated JSON change card with strict validation."""

    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"not found: {resolved}")]
        ) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("document", f"invalid JSON: {exc.msg}")]
        ) from exc
    try:
        return ChangeCard.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def _validate_inputs(
    document: ContractDocument,
    card: ChangeCard,
    candidate_id: str,
    approval: SandboxApproval,
    trajectories: list[Trajectory],
) -> CorrectionCandidate:
    issues: list[ValidationIssue] = []
    if card.incident.contract_id != document.contract.id:
        issues.append(ValidationIssue("card.incident.contract_id", "does not match the contract id"))
    if card.incident.contract_version != document.contract.version:
        issues.append(
            ValidationIssue(
                "card.incident.contract_version",
                "does not match the contract version",
            )
        )

    candidates = [item for item in card.candidates if item.candidate_id == candidate_id]
    if not candidates:
        issues.append(ValidationIssue("candidate_id", "does not exist in the change card"))

    clause_ids = {clause.id for clause in document.clauses}
    required_ids = set(card.acceptance.must_pass_clause_ids)
    required_ids.update(card.acceptance.no_regression_clause_ids)
    unknown_ids = sorted(required_ids - clause_ids)
    if unknown_ids:
        issues.append(
            ValidationIssue(
                "card.acceptance",
                "references unknown clause(s): " + ", ".join(unknown_ids),
            )
        )

    if not trajectories:
        issues.append(ValidationIssue("trajectories", "must contain at least one run"))
    trajectory_ids = [item.trajectory_id for item in trajectories]
    if len(trajectory_ids) != len(set(trajectory_ids)):
        issues.append(ValidationIssue("trajectories", "trajectory ids must be unique"))
    candidate_versions = {item.candidate_version for item in trajectories}
    if len(candidate_versions) > 1:
        issues.append(
            ValidationIssue("trajectories.candidate_version", "must be identical for every run")
        )
    if candidate_versions and card.incident.candidate_version in candidate_versions:
        issues.append(
            ValidationIssue(
                "trajectories.candidate_version",
                "must differ from the incident candidate version",
            )
        )
    if parse_timestamp(approval.approved_at) < parse_timestamp(card.generated_at):
        issues.append(
            ValidationIssue(
                "sandbox_approval.approved_at",
                "cannot precede change-card generation",
            )
        )
    if issues:
        raise DSLValidationError(issues)
    return candidates[0]


def _run_status(checks: list[ClauseCheck]) -> SandboxStatus:
    if any(not item.accepted_for_run and item.status is ClauseStatus.failed for item in checks):
        return SandboxStatus.failed
    if any(not item.accepted_for_run for item in checks):
        return SandboxStatus.indeterminate
    return SandboxStatus.passed


def _evaluate_run(
    document: ContractDocument,
    card: ChangeCard,
    trajectory: Trajectory,
    run_number: int,
) -> SandboxRun:
    evaluation = evaluate_contract(document, trajectory)
    results = {item.clause_id: item for item in evaluation.clause_results}
    checks: list[ClauseCheck] = []

    for clause_id in card.acceptance.must_pass_clause_ids:
        result = results[clause_id]
        checks.append(
            ClauseCheck(
                clause_id=clause_id,
                role="target",
                status=result.status,
                accepted_for_run=result.status is ClauseStatus.passed,
                reason=result.reason,
            )
        )
    for clause_id in card.acceptance.no_regression_clause_ids:
        result = results[clause_id]
        checks.append(
            ClauseCheck(
                clause_id=clause_id,
                role="no_regression",
                status=result.status,
                accepted_for_run=result.status
                in {ClauseStatus.passed, ClauseStatus.not_applicable},
                reason=result.reason,
            )
        )
    return SandboxRun(
        run_number=run_number,
        trajectory_id=trajectory.trajectory_id,
        candidate_version=trajectory.candidate_version,
        evaluated_at=trajectory.evaluated_at,
        status=_run_status(checks),
        cis=trajectory.recovery.cis,
        rollback_available=trajectory.recovery.rollback_available,
        clause_checks=checks,
        evaluation=evaluation,
    )


def _check(
    name: str,
    status: SandboxStatus,
    reason: str,
    runs: list[SandboxRun],
) -> AcceptanceCheck:
    return AcceptanceCheck(
        check=name,
        status=status,
        reason=reason,
        evidence_refs=[f"sandbox-run/{item.trajectory_id}" for item in runs],
    )


def _acceptance_checks(
    document: ContractDocument,
    card: ChangeCard,
    approval: SandboxApproval,
    all_runs: list[SandboxRun],
) -> tuple[list[AcceptanceCheck], list[SandboxRun]]:
    required = card.acceptance.required_consecutive_passing_runs
    if len(all_runs) < required:
        window = all_runs
        run_count_status = SandboxStatus.indeterminate
        run_count_reason = f"Received {len(all_runs)} of {required} required unique run(s)."
    else:
        window = all_runs[-required:]
        if all(item.status is SandboxStatus.passed for item in window):
            run_count_status = SandboxStatus.passed
            run_count_reason = f"The latest {required} run(s) passed consecutively."
        elif any(item.status is SandboxStatus.failed for item in window):
            run_count_status = SandboxStatus.failed
            run_count_reason = "At least one run in the acceptance window failed."
        else:
            run_count_status = SandboxStatus.indeterminate
            run_count_reason = "At least one run in the acceptance window is indeterminate."

    target_checks = [
        check
        for run in window
        for check in run.clause_checks
        if check.role == "target"
    ]
    if target_checks and all(check.status is ClauseStatus.passed for check in target_checks):
        target_status = SandboxStatus.passed
        target_reason = "Every target clause passed in the acceptance window."
    elif any(check.status is ClauseStatus.failed for check in target_checks):
        target_status = SandboxStatus.failed
        target_reason = "A target clause failed in the acceptance window."
    else:
        target_status = SandboxStatus.indeterminate
        target_reason = "Target-clause evidence is incomplete in the acceptance window."

    no_regression_checks = [
        check
        for run in window
        for check in run.clause_checks
        if check.role == "no_regression"
    ]
    non_recovery_ids = {
        clause.id
        for clause in document.clauses
        if not isinstance(clause, RecoveryClause)
        and clause.id in card.acceptance.no_regression_clause_ids
    }
    covered_ids = {
        check.clause_id
        for check in no_regression_checks
        if check.status is ClauseStatus.passed
    }
    if any(check.status is ClauseStatus.failed for check in no_regression_checks):
        regression_status = SandboxStatus.failed
        regression_reason = "A no-regression clause failed in the acceptance window."
    elif any(check.status is ClauseStatus.indeterminate for check in no_regression_checks):
        regression_status = SandboxStatus.indeterminate
        regression_reason = "A no-regression clause is indeterminate."
    elif not non_recovery_ids <= covered_ids:
        missing = sorted(non_recovery_ids - covered_ids)
        regression_status = SandboxStatus.indeterminate
        regression_reason = "No passing coverage for clause(s): " + ", ".join(missing) + "."
    else:
        regression_status = SandboxStatus.passed
        regression_reason = "No regression was found and all non-recovery clauses have passing coverage."

    minimum_cis = card.acceptance.minimum_cis
    if minimum_cis is None:
        cis_status = SandboxStatus.passed
        cis_reason = "The change card declares no minimum CIS."
    elif any(run.cis is None for run in window) or len(window) < required:
        cis_status = SandboxStatus.indeterminate
        cis_reason = f"CIS evidence is required for all {required} acceptance run(s)."
    elif any(run.cis < minimum_cis for run in window if run.cis is not None):
        cis_status = SandboxStatus.failed
        cis_reason = f"At least one acceptance run is below minimum CIS {minimum_cis}."
    else:
        cis_status = SandboxStatus.passed
        cis_reason = f"Every acceptance run meets minimum CIS {minimum_cis}."

    if any(run.rollback_available is False for run in window):
        rollback_status = SandboxStatus.failed
        rollback_reason = "Rollback was explicitly unavailable in an acceptance run."
    elif len(window) < required or any(run.rollback_available is None for run in window):
        rollback_status = SandboxStatus.indeterminate
        rollback_reason = "Rollback evidence is required for every acceptance run."
    else:
        rollback_status = SandboxStatus.passed
        rollback_reason = "Rollback availability is evidenced for every acceptance run."

    checks = [
        _check(
            "sandbox_execution_approval",
            SandboxStatus.passed,
            f"{approval.actor} approved sandbox evaluation only at {approval.approved_at}.",
            window,
        ),
        _check("consecutive_passing_runs", run_count_status, run_count_reason, window),
        _check("target_clauses", target_status, target_reason, window),
        _check("no_regression", regression_status, regression_reason, window),
        _check("minimum_cis", cis_status, cis_reason, window),
        _check("rollback_available", rollback_status, rollback_reason, window),
    ]
    return checks, window


def run_candidate_sandbox(
    document: ContractDocument,
    card: ChangeCard,
    candidate_id: str,
    approval: SandboxApproval,
    trajectories: list[Trajectory],
) -> SandboxReport:
    """Evaluate an approved candidate build without applying or releasing it."""

    candidate = _validate_inputs(document, card, candidate_id, approval, trajectories)
    ordered = sorted(
        trajectories,
        key=lambda item: (parse_timestamp(item.evaluated_at), item.trajectory_id),
    )
    runs = [
        _evaluate_run(document, card, trajectory, index)
        for index, trajectory in enumerate(ordered, start=1)
    ]
    checks, window = _acceptance_checks(document, card, approval, runs)
    if any(item.status is SandboxStatus.failed for item in checks):
        decision = SandboxStatus.failed
    elif any(item.status is SandboxStatus.indeterminate for item in checks):
        decision = SandboxStatus.indeterminate
    else:
        decision = SandboxStatus.passed

    generated_at = ordered[-1].evaluated_at
    report_digest = _canonical_hash(
        {
            "card_id": card.card_id,
            "candidate_id": candidate.candidate_id,
            "candidate_version": ordered[0].candidate_version,
            "trajectory_ids": [item.trajectory_id for item in ordered],
            "approval": approval.model_dump(mode="json"),
            "sandbox_version": SANDBOX_VERSION,
        }
    )
    return SandboxReport(
        report_id=f"SBOX-{report_digest[:24].upper()}",
        generated_at=generated_at,
        contract_id=document.contract.id,
        contract_version=document.contract.version,
        card_id=card.card_id,
        incident_id=card.incident.incident_id,
        candidate_version=ordered[0].candidate_version,
        selected_candidate=candidate,
        sandbox_approval=approval,
        required_consecutive_passing_runs=card.acceptance.required_consecutive_passing_runs,
        acceptance_window_trajectory_ids=[item.trajectory_id for item in window],
        runs=runs,
        acceptance_checks=checks,
        decision=decision,
        eligible_for_release_review=decision is SandboxStatus.passed,
    )
