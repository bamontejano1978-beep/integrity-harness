"""Deterministic evaluator for Behavioural Contracts DSL v0.1."""

from __future__ import annotations

from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.models import (
    AutonomyLevel,
    EvaluationMethod,
    ClauseBase,
    ConditionalClause,
    ContractDocument,
    DebeClause,
    IndeterminatePolicy,
    NoDebeClause,
    RecoveryClause,
    TemporalClause,
    value_matches_attribute_type,
)
from autopilot.trajectory import (
    ObservationKind,
    RecoveryEvidence,
    Trajectory,
    TrajectoryObservation,
    parse_timestamp,
)

EVALUATOR_NAME = "deterministic_trajectory_evaluator"
EVALUATOR_VERSION = "0.2.0"

_AUTONOMY_ORDER = {
    AutonomyLevel.COACH: 0,
    AutonomyLevel.SUPERVISE: 1,
    AutonomyLevel.RESTRICT: 2,
    AutonomyLevel.QUARANTINE: 3,
}


class ClauseStatus(str, Enum):
    passed = "pass"
    failed = "fail"
    indeterminate = "indeterminate"
    not_applicable = "not_applicable"


class EvidenceKind(str, Enum):
    trajectory_turn = "trajectory_turn"
    tool_call = "tool_call"
    event = "event"
    metric = "metric"
    human_attestation = "human_attestation"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    ref: str
    observed_at: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    value_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_serializer(mode="wrap")
    def omit_absent_digests(self, handler):
        payload = handler(self)
        if self.sha256 is None:
            payload.pop("sha256", None)
        if self.value_sha256 is None:
            payload.pop("value_sha256", None)
        return payload


class EvaluatorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = EVALUATOR_NAME
    version: str = EVALUATOR_VERSION


class ClauseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str
    contract_version: str
    clause_id: str
    status: ClauseStatus
    observed_at: str
    # The requested method is policy; evaluation_method records what this
    # runtime actually executed. The current engine supports rules and manual
    # review fallback, never a live LLM judge.
    requested_evaluation_method: EvaluationMethod
    evaluation_method: Literal["rule_based", "manual_review"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    evidence: list[EvidenceRef]
    evaluator: EvaluatorInfo = Field(default_factory=EvaluatorInfo)
    recommended_autonomy: AutonomyLevel
    block_release: bool


class ContractEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str
    contract_version: str
    trajectory_id: str
    candidate_version: str
    status: ClauseStatus
    evaluated_at: str
    recommended_autonomy: AutonomyLevel
    block_release: bool
    clause_results: list[ClauseResult]
    evaluator: EvaluatorInfo = Field(default_factory=EvaluatorInfo)


def _value_digest(value: Any) -> str:
    """Digest the canonical JSON representation of a recovery evidence value."""

    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _least_permissive(levels: Iterable[AutonomyLevel]) -> AutonomyLevel:
    return max(levels, key=_AUTONOMY_ORDER.__getitem__, default=AutonomyLevel.COACH)


def _attribute_state(actual: dict[str, Any], expected: dict[str, Any]) -> str:
    """Return ``match``, ``mismatch``, or ``missing`` for expected attributes."""

    if any(key in actual and actual[key] != value for key, value in expected.items()):
        return "mismatch"
    if any(key not in actual for key in expected):
        return "missing"
    return "match"


def _evidence_for(observation: TrajectoryObservation) -> EvidenceRef:
    return EvidenceRef(
        kind=(
            EvidenceKind.event
            if observation.kind is ObservationKind.event
            else EvidenceKind.trajectory_turn
        ),
        ref=observation.ref,
        observed_at=observation.observed_at,
    )


def _observations(
    trajectory: Trajectory,
    *,
    kind: ObservationKind,
    name: str,
) -> list[TrajectoryObservation]:
    return sorted(
        (
            observation
            for observation in trajectory.observations
            if observation.kind is kind and observation.name == name
        ),
        key=lambda observation: (parse_timestamp(observation.observed_at), observation.ref),
    )


def _workflow_policy(
    document: ContractDocument,
    clause: ClauseBase,
    status: ClauseStatus,
) -> tuple[AutonomyLevel, bool]:
    if status in {ClauseStatus.passed, ClauseStatus.not_applicable}:
        return AutonomyLevel.COACH, False

    failure = clause.on_failure or document.contract.defaults.on_failure
    if status is ClauseStatus.failed:
        return failure.autonomy_level, failure.block_release

    evaluation = clause.evaluation or document.contract.defaults.evaluation
    if evaluation.on_indeterminate is IndeterminatePolicy.fail_closed:
        return failure.autonomy_level, failure.block_release
    if evaluation.on_indeterminate is IndeterminatePolicy.manual_review:
        return AutonomyLevel.SUPERVISE, True
    return AutonomyLevel.COACH, False


def _result(
    document: ContractDocument,
    trajectory: Trajectory,
    clause: ClauseBase,
    *,
    status: ClauseStatus,
    reason: str,
    evidence: list[EvidenceRef],
) -> ClauseResult:
    evaluation = clause.evaluation or document.contract.defaults.evaluation
    confidence = 0.0 if status is ClauseStatus.indeterminate else 1.0
    requested_method = evaluation.method
    actual_method: Literal["rule_based", "manual_review"] = "rule_based"
    if requested_method in {EvaluationMethod.llm_judge, EvaluationMethod.manual}:
        reason = (
            f"Requested evaluation method '{requested_method.value}' is not executable "
            f"by this runtime. {reason}"
        )
        if status is not ClauseStatus.failed:
            confidence = 0.0
            status = ClauseStatus.indeterminate
            actual_method = "manual_review"
        else:
            reason = f"A deterministic violation is confirmed; the requested method was not run. {reason}"
    elif requested_method is EvaluationMethod.hybrid and status is ClauseStatus.indeterminate:
        confidence = 0.0
        actual_method = "manual_review"
        reason = (
            "The deterministic hybrid pre-check is indeterminate; no LLM judge is "
            f"configured, so manual review is required. {reason}"
        )
    if requested_method not in {EvaluationMethod.llm_judge, EvaluationMethod.manual} and confidence < evaluation.confidence_floor:
        status = ClauseStatus.indeterminate
        reason = (
            f"Deterministic confidence {confidence:.1f} is below the configured "
            f"floor {evaluation.confidence_floor:.1f}. {reason}"
        )
    autonomy, block_release = _workflow_policy(document, clause, status)
    if isinstance(clause, RecoveryClause):
        if status is ClauseStatus.passed:
            autonomy = clause.autonomy.restore_to
        elif status in {ClauseStatus.failed, ClauseStatus.indeterminate}:
            autonomy = _least_permissive([autonomy, clause.autonomy.on_activation])

    return ClauseResult(
        contract_id=document.contract.id,
        contract_version=document.contract.version,
        clause_id=clause.id,
        status=status,
        observed_at=trajectory.evaluated_at,
        requested_evaluation_method=evaluation.method,
        evaluation_method=actual_method,
        confidence=confidence,
        reason=reason,
        evidence=evidence,
        recommended_autonomy=autonomy,
        block_release=block_release,
    )


def _evaluate_monotonic_obligation(
    document: ContractDocument,
    trajectory: Trajectory,
    clause: TemporalClause,
) -> ClauseResult:
    """Evaluate a temporal clause exclusively with one comparable monotonic clock."""

    trigger = clause.trigger
    requirement = clause.requirement
    possible_triggers = _observations(
        trajectory,
        kind=ObservationKind.event,
        name=trigger.event,
    )
    matching_triggers = [
        item
        for item in possible_triggers
        if _attribute_state(item.attributes, trigger.conditions) == "match"
    ]
    ambiguous_triggers = [
        item
        for item in possible_triggers
        if _attribute_state(item.attributes, trigger.conditions) == "missing"
    ]
    if not matching_triggers:
        if ambiguous_triggers:
            return _result(
                document,
                trajectory,
                clause,
                status=ClauseStatus.indeterminate,
                reason="A possible trigger is missing attributes required to evaluate it.",
                evidence=[_evidence_for(item) for item in ambiguous_triggers],
            )
        if not trajectory.complete:
            return _result(
                document,
                trajectory,
                clause,
                status=ClauseStatus.indeterminate,
                reason=(
                    "The trajectory is incomplete; absence of the trigger "
                    "cannot establish that this obligation was not applicable."
                ),
                evidence=[],
            )
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.not_applicable,
            reason="No matching trigger was observed in the complete trajectory.",
            evidence=[],
        )

    if trajectory.monotonic_clock_id is None or trajectory.evaluated_monotonic_ns is None:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason="The trajectory has no monotonic evaluation clock evidence.",
            evidence=[_evidence_for(item) for item in matching_triggers],
        )
    if any(
        item.monotonic_ns is not None
        and item.monotonic_clock_id == trajectory.monotonic_clock_id
        and item.monotonic_ns > trajectory.evaluated_monotonic_ns
        for item in trajectory.observations
    ):
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason="Trajectory monotonic timestamps are inconsistent with the evaluation counter.",
            evidence=[_evidence_for(item) for item in matching_triggers],
        )

    matching_triggers.sort(
        key=lambda item: (
            item.monotonic_ns is None,
            item.monotonic_ns if item.monotonic_ns is not None else 0,
            item.ref,
        )
    )
    actions = _observations(
        trajectory,
        kind=ObservationKind.action,
        name=requirement.action,
    )
    evidence: list[EvidenceRef] = []
    failed = 0
    indeterminate = len(ambiguous_triggers)
    deadline_delta = clause.window.within_seconds * 1_000_000_000
    used_action_refs: set[str] = set()

    for trigger_observation in matching_triggers:
        evidence.append(_evidence_for(trigger_observation))
        trigger_ns = trigger_observation.monotonic_ns
        if (
            trigger_ns is None
            or trigger_observation.monotonic_clock_id != trajectory.monotonic_clock_id
            or trigger_ns > trajectory.evaluated_monotonic_ns
        ):
            indeterminate += 1
            continue

        deadline_ns = trigger_ns + deadline_delta
        timely_actions: list[TrajectoryObservation] = []
        late_actions: list[TrajectoryObservation] = []
        ambiguous_actions: list[TrajectoryObservation] = []
        incomparable_actions: list[TrajectoryObservation] = []
        for action in actions:
            if action.ref in used_action_refs:
                continue
            attribute_state = _attribute_state(action.attributes, requirement.parameters)
            if attribute_state == "mismatch":
                continue
            if (
                action.monotonic_ns is None
                or action.monotonic_clock_id != trajectory.monotonic_clock_id
                or action.monotonic_ns > trajectory.evaluated_monotonic_ns
            ):
                incomparable_actions.append(action)
                continue
            if action.monotonic_ns < trigger_ns:
                continue
            if attribute_state == "missing":
                if action.monotonic_ns <= deadline_ns:
                    ambiguous_actions.append(action)
            elif action.monotonic_ns <= deadline_ns:
                timely_actions.append(action)
            else:
                late_actions.append(action)

        if timely_actions:
            action = min(timely_actions, key=lambda item: (item.monotonic_ns or 0, item.ref))
            used_action_refs.add(action.ref)
            evidence.append(_evidence_for(action))
            continue
        if ambiguous_actions or incomparable_actions:
            indeterminate += 1
            evidence.extend(
                _evidence_for(item)
                for item in [*ambiguous_actions, *incomparable_actions]
            )
            continue

        window_closed = (
            trajectory.complete
            or trajectory.evaluated_monotonic_ns > deadline_ns
        )
        if window_closed:
            failed += 1
            if late_actions:
                evidence.append(
                    _evidence_for(
                        min(late_actions, key=lambda item: (item.monotonic_ns or 0, item.ref))
                    )
                )
        else:
            indeterminate += 1

    if failed:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.failed,
            reason=f"{failed} triggered obligation(s) were absent, late, or mismatched.",
            evidence=evidence,
        )
    if indeterminate:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason=f"{indeterminate} triggered obligation(s) lack complete evidence.",
            evidence=evidence,
        )
    return _result(
        document,
        trajectory,
        clause,
        status=ClauseStatus.passed,
        reason=f"All {len(matching_triggers)} triggered obligation(s) were satisfied.",
        evidence=evidence,
    )


def _evaluate_obligation(
    document: ContractDocument,
    trajectory: Trajectory,
    clause: DebeClause | ConditionalClause | TemporalClause,
) -> ClauseResult:
    if isinstance(clause, ConditionalClause):
        trigger = clause.if_
        requirement = clause.then
        within_seconds = requirement.within_seconds
    elif isinstance(clause, TemporalClause):
        trigger = clause.trigger
        requirement = clause.requirement
        within_seconds = clause.window.within_seconds
    else:
        trigger = clause.trigger
        requirement = clause.requirement
        within_seconds = requirement.within_seconds

    if isinstance(clause, TemporalClause) and clause.window.clock.value == "monotonic":
        return _evaluate_monotonic_obligation(document, trajectory, clause)

    possible_triggers = _observations(
        trajectory,
        kind=ObservationKind.event,
        name=trigger.event,
    )
    matching_triggers = [
        observation
        for observation in possible_triggers
        if _attribute_state(observation.attributes, trigger.conditions) == "match"
    ]
    ambiguous_triggers = [
        observation
        for observation in possible_triggers
        if _attribute_state(observation.attributes, trigger.conditions) == "missing"
    ]

    if not matching_triggers:
        if ambiguous_triggers:
            return _result(
                document,
                trajectory,
                clause,
                status=ClauseStatus.indeterminate,
                reason="A possible trigger is missing attributes required to evaluate it.",
                evidence=[_evidence_for(observation) for observation in ambiguous_triggers],
            )
        if not trajectory.complete:
            return _result(
                document,
                trajectory,
                clause,
                status=ClauseStatus.indeterminate,
                reason=(
                    "The trajectory is incomplete; absence of the trigger "
                    "cannot establish that this obligation was not applicable."
                ),
                evidence=[],
            )
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.not_applicable,
            reason="No matching trigger was observed in the complete trajectory.",
            evidence=[],
        )

    actions = _observations(
        trajectory,
        kind=ObservationKind.action,
        name=requirement.action,
    )
    used_action_refs: set[str] = set()
    evidence: list[EvidenceRef] = []
    failed = 0
    indeterminate = 0

    for trigger_observation in matching_triggers:
        evidence.append(_evidence_for(trigger_observation))
        trigger_time = parse_timestamp(trigger_observation.observed_at)
        trigger_clock = trigger_time.timestamp()
        deadline = trigger_clock + within_seconds if within_seconds is not None else None
        candidate_actions = [action for action in actions if action.ref not in used_action_refs]
        after_trigger = [
            action
            for action in candidate_actions
            if parse_timestamp(action.observed_at) >= trigger_time
        ]
        matching_actions = [
            action
            for action in after_trigger
            if _attribute_state(action.attributes, requirement.parameters) == "match"
        ]
        timely_actions = [
            action
            for action in matching_actions
            if deadline is None
            or parse_timestamp(action.observed_at).timestamp() <= deadline
        ]

        if timely_actions:
            action = timely_actions[0]
            used_action_refs.add(action.ref)
            evidence.append(_evidence_for(action))
            continue

        ambiguous_actions = [
            action
            for action in after_trigger
            if _attribute_state(action.attributes, requirement.parameters) == "missing"
            and (
                deadline is None
                or parse_timestamp(action.observed_at).timestamp() <= deadline
            )
        ]
        if ambiguous_actions:
            indeterminate += 1
            evidence.extend(_evidence_for(action) for action in ambiguous_actions)
            continue

        window_closed = trajectory.complete
        if deadline is not None:
            window_closed = window_closed or (
                parse_timestamp(trajectory.evaluated_at).timestamp() > deadline
            )

        if window_closed:
            failed += 1
            if matching_actions:
                evidence.append(_evidence_for(matching_actions[0]))
        else:
            indeterminate += 1

    if failed:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.failed,
            reason=f"{failed} triggered obligation(s) were absent, late, or mismatched.",
            evidence=evidence,
        )
    if indeterminate:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason=f"{indeterminate} triggered obligation(s) lack complete evidence.",
            evidence=evidence,
        )
    return _result(
        document,
        trajectory,
        clause,
        status=ClauseStatus.passed,
        reason=f"All {len(matching_triggers)} triggered obligation(s) were satisfied.",
        evidence=evidence,
    )


def _evaluate_prohibition(
    document: ContractDocument,
    trajectory: Trajectory,
    clause: NoDebeClause,
) -> ClauseResult:
    actions = _observations(
        trajectory,
        kind=ObservationKind.action,
        name=clause.prohibition.action,
    )
    violations = [
        action
        for action in actions
        if _attribute_state(action.attributes, clause.prohibition.conditions) == "match"
    ]
    if violations:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.failed,
            reason=f"Observed {len(violations)} prohibited action(s).",
            evidence=[_evidence_for(action) for action in violations],
        )

    ambiguous = [
        action
        for action in actions
        if _attribute_state(action.attributes, clause.prohibition.conditions) == "missing"
    ]
    if ambiguous:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason="A possible prohibited action is missing required condition attributes.",
            evidence=[_evidence_for(action) for action in ambiguous],
        )
    if not trajectory.complete:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason="The observation window is incomplete; absence cannot yet be established.",
            evidence=[],
        )
    return _result(
        document,
        trajectory,
        clause,
        status=ClauseStatus.passed,
        reason="No prohibited action was observed in the complete trajectory.",
        evidence=[],
    )


def _evaluate_recovery(
    document: ContractDocument,
    trajectory: Trajectory,
    clause: RecoveryClause,
    current_results: dict[str, ClauseResult],
) -> ClauseResult:
    """Evaluate a ``recuperacion_condicion`` clause.

    The clause is activated by either an explicit ``prior_failure`` in the
    trajectory's ``RecoveryContext`` or by an in-round failing result; with
    neither signal the clause returns ``not_applicable``. The evaluator
    maintains no state across calls — round boundaries are the caller's
    responsibility via ``candidate_version``+``evaluated_at`` on the
    trajectory.
    """
    activating_ids = set(clause.activation.clause_failure_any_of)
    prior_failures = [
        failure
        for failure in trajectory.recovery.prior_failures
        if failure.clause_id in activating_ids
    ]
    current_failures = [
        result
        for clause_id, result in current_results.items()
        if clause_id in activating_ids and result.status is ClauseStatus.failed
    ]
    if not prior_failures and not current_failures:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.not_applicable,
            reason="No activating clause failure was observed or supplied as prior context.",
            evidence=[],
        )

    failure_times = [parse_timestamp(failure.failed_at) for failure in prior_failures]
    failure_times.extend(parse_timestamp(result.observed_at) for result in current_failures)
    latest_failure = max(failure_times)
    evidence: list[EvidenceRef] = []
    unmet: list[str] = []
    missing: list[str] = []
    if current_failures:
        evidence.extend(
            _evidence_for(observation)
            for result in current_failures
            for observation in trajectory.observations
            if observation.ref in {item.ref for item in result.evidence}
        )
    requirements = clause.requirements
    recovery = trajectory.recovery

    def proof_for(requirement: str, clause_id: str | None = None) -> RecoveryEvidence | None:
        return next(
            (
                item
                for item in recovery.evidence
                if item.requirement == requirement and item.clause_id == clause_id
            ),
            None,
        )

    def proof_is_bound(proof: RecoveryEvidence | None) -> bool:
        return bool(
            proof is not None
            and proof.contract_id == document.contract.id
            and proof.contract_version == document.contract.version
            and proof.candidate_version == trajectory.candidate_version
        )

    def proof_value_matches(proof: RecoveryEvidence | None, value: Any) -> bool:
        return bool(proof is not None and proof.value_sha256 == _value_digest(value))

    def proof_is_fresh(proof: RecoveryEvidence | None) -> bool:
        return bool(proof is not None and parse_timestamp(proof.observed_at) > latest_failure)

    for failure in prior_failures:
        proof = proof_for("prior_failure", failure.clause_id)
        if (
            proof is None
            or not proof_is_bound(proof)
            or proof.observed_at != failure.failed_at
            or not proof_value_matches(proof, failure.model_dump(mode="json"))
        ):
            missing.append(f"prior_failure:{failure.clause_id}")
        else:
            evidence.append(
                EvidenceRef(
                    kind=EvidenceKind.event,
                    ref=proof.ref,
                    observed_at=proof.observed_at,
                    sha256=proof.sha256,
                    value_sha256=proof.value_sha256,
                )
            )

    if requirements.human_approval:
        approval = recovery.human_approval
        proof = proof_for("human_approval")
        if approval is None:
            missing.append("human_approval")
        elif not approval.approved:
            unmet.append("human_approval")
        elif parse_timestamp(approval.observed_at) <= latest_failure:
            unmet.append("human_approval_stale")
        elif (
            proof is None
            or not proof_is_bound(proof)
            or proof.observed_at != approval.observed_at
            or not proof_value_matches(proof, approval.model_dump(mode="json"))
        ):
            missing.append("human_approval")
        # ``<=`` is intentional: an approval whose ``observed_at`` EQUALS
        # ``failed_at`` is treated as stale — only observations strictly
        # AFTER the failure count as fresh. This sub-second decision is
        # contractually irrelevant in practice (real attestations are
        # human-attributable minutes/hours after the failure) but the test
        # ``test_round2_human_approval_one_second_after_failure_passes``
        # pins the upper bound and ``test_round2_human_approval_stale_*``
        # pins stale. Change with care.
        else:
            evidence.append(
                EvidenceRef(
                    kind=EvidenceKind.human_attestation,
                    ref=proof.ref,
                    observed_at=proof.observed_at,
                    sha256=proof.sha256,
                    value_sha256=proof.value_sha256,
                )
            )

    if requirements.consecutive_passing_runs is not None:
        actual = recovery.consecutive_passing_runs
        proof = proof_for("consecutive_passing_runs")
        if actual is None:
            missing.append("consecutive_passing_runs")
        elif actual < requirements.consecutive_passing_runs:
            unmet.append("consecutive_passing_runs")
        elif (
            proof is None
            or not proof_is_bound(proof)
            or not proof_is_fresh(proof)
            or not proof_value_matches(proof, actual)
        ):
            missing.append("consecutive_passing_runs")
        else:
            evidence.append(
                EvidenceRef(
                    kind=EvidenceKind.metric,
                    ref=proof.ref,
                    observed_at=proof.observed_at,
                    sha256=proof.sha256,
                    value_sha256=proof.value_sha256,
                )
            )

    if requirements.minimum_cis is not None:
        actual_cis = recovery.cis
        proof = proof_for("minimum_cis")
        if actual_cis is None:
            missing.append("minimum_cis")
        elif actual_cis < requirements.minimum_cis:
            unmet.append("minimum_cis")
        elif (
            proof is None
            or not proof_is_bound(proof)
            or not proof_is_fresh(proof)
            or not proof_value_matches(proof, actual_cis)
        ):
            missing.append("minimum_cis")
        else:
            evidence.append(
                EvidenceRef(
                    kind=EvidenceKind.metric,
                    ref=proof.ref,
                    observed_at=proof.observed_at,
                    sha256=proof.sha256,
                    value_sha256=proof.value_sha256,
                )
            )

    if requirements.rollback_available:
        proof = proof_for("rollback_available")
        if recovery.rollback_available is None:
            missing.append("rollback_available")
        elif not recovery.rollback_available:
            unmet.append("rollback_available")
        elif (
            proof is None
            or not proof_is_bound(proof)
            or not proof_is_fresh(proof)
            or not proof_value_matches(proof, recovery.rollback_available)
        ):
            missing.append("rollback_available")
        else:
            evidence.append(
                EvidenceRef(
                    kind=EvidenceKind.event,
                    ref=proof.ref,
                    observed_at=proof.observed_at,
                    sha256=proof.sha256,
                    value_sha256=proof.value_sha256,
                )
            )

    if requirements.cooldown_seconds is not None:
        elapsed = (parse_timestamp(trajectory.evaluated_at) - latest_failure).total_seconds()
        if elapsed < requirements.cooldown_seconds:
            unmet.append("cooldown_seconds")
        else:
            evidence.append(
                EvidenceRef(
                    kind=EvidenceKind.metric,
                    ref=f"cooldown-seconds/{int(elapsed)}",
                    observed_at=trajectory.evaluated_at,
                )
            )

    if unmet:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.failed,
            reason="Recovery requirements not met: " + ", ".join(unmet) + ".",
            evidence=evidence,
        )
    if missing:
        return _result(
            document,
            trajectory,
            clause,
            status=ClauseStatus.indeterminate,
            reason="Recovery evidence missing: " + ", ".join(missing) + ".",
            evidence=evidence,
        )
    return _result(
        document,
        trajectory,
        clause,
        status=ClauseStatus.passed,
        reason="All declared recovery requirements are satisfied for the active failure.",
        evidence=evidence,
    )


def _validate_inputs(document: ContractDocument, trajectory: Trajectory) -> None:
    issues: list[ValidationIssue] = []
    if trajectory.contract_id != document.contract.id:
        issues.append(
            ValidationIssue("trajectory.contract_id", "does not match the contract id")
        )
    if trajectory.contract_version != document.contract.version:
        issues.append(
            ValidationIssue("trajectory.contract_version", "does not match the contract version")
        )

    attributes = document.vocabulary.attributes
    for index, observation in enumerate(trajectory.observations):
        for name, value in observation.attributes.items():
            expected = attributes.get(name)
            if expected is not None and not value_matches_attribute_type(value, expected):
                issues.append(
                    ValidationIssue(
                        f"trajectory.observations[{index}].attributes.{name}",
                        f"expected {expected.value}, got {type(value).__name__}",
                    )
                )

    clause_ids = {clause.id for clause in document.clauses}
    for index, failure in enumerate(trajectory.recovery.prior_failures):
        if failure.clause_id not in clause_ids:
            issues.append(
                ValidationIssue(
                    f"trajectory.recovery.prior_failures[{index}].clause_id",
                    "does not exist in the contract",
                )
            )
    if issues:
        raise DSLValidationError(issues)


def evaluate_contract(
    document: ContractDocument,
    trajectory: Trajectory,
) -> ContractEvaluation:
    """Evaluate a typed trajectory deterministically against a typed contract."""

    _validate_inputs(document, trajectory)
    results: list[ClauseResult] = []

    for clause in document.clauses:
        if isinstance(clause, RecoveryClause):
            continue
        if isinstance(clause, NoDebeClause):
            result = _evaluate_prohibition(document, trajectory, clause)
        elif isinstance(clause, (DebeClause, ConditionalClause, TemporalClause)):
            result = _evaluate_obligation(document, trajectory, clause)
        else:  # pragma: no cover - discriminated model union makes this unreachable
            raise TypeError(f"unsupported clause type: {type(clause).__name__}")
        results.append(result)

    current_results = {result.clause_id: result for result in results}
    for clause in document.clauses:
        if isinstance(clause, RecoveryClause):
            results.append(
                _evaluate_recovery(document, trajectory, clause, current_results)
            )

    statuses = {result.status for result in results}
    if ClauseStatus.failed in statuses:
        status = ClauseStatus.failed
    elif ClauseStatus.indeterminate in statuses:
        status = ClauseStatus.indeterminate
    elif ClauseStatus.passed in statuses:
        status = ClauseStatus.passed
    else:
        status = ClauseStatus.not_applicable

    return ContractEvaluation(
        contract_id=document.contract.id,
        contract_version=document.contract.version,
        trajectory_id=trajectory.trajectory_id,
        candidate_version=trajectory.candidate_version,
        status=status,
        evaluated_at=trajectory.evaluated_at,
        recommended_autonomy=_least_permissive(
            result.recommended_autonomy for result in results
        ),
        block_release=any(result.block_release for result in results),
        clause_results=results,
    )
