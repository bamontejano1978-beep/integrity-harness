"""Deterministic diagnostic hypotheses and non-executable change cards."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.models import (
    AutonomyLevel,
    ConditionalClause,
    ContractDocument,
    DebeClause,
    NoDebeClause,
    RecoveryClause,
    TemporalClause,
)
from autopilot.incidents import Incident, IncidentBatch

GENERATOR_NAME = "deterministic_diagnostic_change_card_generator"
GENERATOR_VERSION = "0.1.0"


class DiagnosisStatus(str, Enum):
    hypotheses_only = "hypotheses_only"


class HypothesisStatus(str, Enum):
    untested = "untested"


class CauseLayer(str, Enum):
    prompt = "prompt"
    model = "model"
    memory = "memory"
    tool = "tool"
    state = "state"
    evaluator = "evaluator"
    orchestration = "orchestration"


class ChangeType(str, Enum):
    prompt_policy = "prompt_policy"
    state_guard = "state_guard"
    tool_gate = "tool_gate"
    routing_policy = "routing_policy"
    adapter_rule = "adapter_rule"
    evaluator_rule = "evaluator_rule"
    recovery_evidence = "recovery_evidence"


class ChangeRisk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class DiagnosticHypothesis(BaseModel):
    """A falsifiable lead, explicitly not a confirmed causal finding."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    rank: int = Field(ge=1, le=3)
    layer: CauseLayer
    status: HypothesisStatus = HypothesisStatus.untested
    statement: str
    rationale: str
    evidence_refs: list[str]
    verification_steps: list[str] = Field(min_length=1)
    falsification_condition: str


class CorrectionCandidate(BaseModel):
    """Description-only correction candidate; never executable by this module."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    rank: int = Field(ge=1, le=3)
    change_type: ChangeType
    target_layer: CauseLayer
    summary: str
    proposed_change: str
    addresses_hypothesis_ids: list[str] = Field(min_length=1)
    risk: ChangeRisk
    verification_steps: list[str] = Field(min_length=1)
    rollback_plan: str
    description_only: Literal[True] = True
    patch_generated: Literal[False] = False
    requires_human_approval: Literal[True] = True


class AcceptanceCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    must_pass_clause_ids: list[str] = Field(min_length=1)
    no_regression_clause_ids: list[str]
    related_recovery_clause_ids: list[str]
    required_consecutive_passing_runs: int = Field(ge=1)
    minimum_cis: int | None = Field(default=None, ge=0, le=100)
    rollback_required: Literal[True] = True


class ReleasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_current_autonomy: AutonomyLevel
    automatic_application: Literal[False] = False
    automatic_release: Literal[False] = False
    human_approval_required: Literal[True] = True
    rollback_required: Literal[True] = True


class ChangeCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    card_id: str
    generated_at: str
    diagnosis_status: DiagnosisStatus = DiagnosisStatus.hypotheses_only
    incident: Incident
    hypotheses: list[DiagnosticHypothesis] = Field(min_length=1, max_length=3)
    candidates: list[CorrectionCandidate] = Field(min_length=1, max_length=3)
    acceptance: AcceptanceCriteria
    release_policy: ReleasePolicy
    generator_name: str = GENERATOR_NAME
    generator_version: str = GENERATOR_VERSION
    disclaimer: str = (
        "These are untested diagnostic hypotheses, not causal findings. "
        "No change or release is applied automatically."
    )


class ChangeCardBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str
    contract_version: str
    trajectory_id: str
    candidate_version: str
    generated_at: str
    card_count: int = Field(ge=0)
    cards: list[ChangeCard]


class _HypothesisTemplate(BaseModel):
    layer: CauseLayer
    statement: str
    rationale: str
    verification_steps: list[str]
    falsification_condition: str


class _CandidateTemplate(BaseModel):
    change_type: ChangeType
    target_layer: CauseLayer
    summary: str
    proposed_change: str
    hypothesis_ranks: list[int]
    risk: ChangeRisk
    verification_steps: list[str]
    rollback_plan: str


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _templates_for_clause(clause) -> tuple[list[_HypothesisTemplate], list[_CandidateTemplate]]:
    evidence_check = "Replay the incident fixture and inspect the cited evidence references."

    if isinstance(clause, DebeClause):
        hypotheses = [
            _HypothesisTemplate(
                layer=CauseLayer.prompt,
                statement="The positive obligation may be absent or insufficiently explicit in the active prompt.",
                rationale="A required action was not observed after its trigger.",
                verification_steps=["Inspect the effective prompt and examples for the required action."],
                falsification_condition="The active prompt states the obligation unambiguously and prompt-level tests select it reliably.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.orchestration,
                statement="The orchestration path may have failed to schedule the required action after the trigger.",
                rationale="The trigger was observed but the matching action was not completed.",
                verification_steps=["Trace trigger handling through routing and action scheduling."],
                falsification_condition="The trace proves the action was scheduled and successfully completed within the allowed window.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.evaluator,
                statement="The adapter or evaluator may not have recognized a semantically valid required action.",
                rationale="The observed failure can also result from incomplete action instrumentation.",
                verification_steps=[evidence_check, "Compare raw action telemetry with the canonical trajectory."],
                falsification_condition="Canonical telemetry contains no valid instance of the required action.",
            ),
        ]
        candidates = [
            _CandidateTemplate(
                change_type=ChangeType.prompt_policy,
                target_layer=CauseLayer.prompt,
                summary="Make the positive obligation explicit.",
                proposed_change="Add a concise obligation and one compliant example for the required action.",
                hypothesis_ranks=[1],
                risk=ChangeRisk.medium,
                verification_steps=["Run the incident fixture and the full contract regression set."],
                rollback_plan="Restore the previous versioned prompt if acceptance criteria fail.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.routing_policy,
                target_layer=CauseLayer.orchestration,
                summary="Guard trigger-to-action routing.",
                proposed_change="Require an explicit scheduling decision when this trigger is received.",
                hypothesis_ranks=[2],
                risk=ChangeRisk.medium,
                verification_steps=["Verify the action is scheduled and completed in a replay."],
                rollback_plan="Disable the new routing rule and restore its prior configuration.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.adapter_rule,
                target_layer=CauseLayer.evaluator,
                summary="Calibrate recognition using labelled telemetry.",
                proposed_change="Add a reviewed positive fixture before changing any recognition rule.",
                hypothesis_ranks=[3],
                risk=ChangeRisk.low,
                verification_steps=["Confirm the fixture is detected without changing negative fixtures."],
                rollback_plan="Remove the added rule or fixture mapping and rerun the prior suite.",
            ),
        ]
    elif isinstance(clause, NoDebeClause):
        hypotheses = [
            _HypothesisTemplate(
                layer=CauseLayer.prompt,
                statement="The prohibition may be weak, ambiguous, or missing from the active prompt policy.",
                rationale="A prohibited action was observed under the clause conditions.",
                verification_steps=["Inspect the effective prompt, precedence, and refusal examples."],
                falsification_condition="The effective prompt contains an unambiguous prohibition and adversarial prompt tests consistently refuse the action.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.state,
                statement="The authorization state may have been absent, stale, or unavailable at decision time.",
                rationale="The prohibition depends on decision-time state conditions.",
                verification_steps=["Trace the condition attributes and their provenance at the prohibited action timestamp."],
                falsification_condition="The trace proves fresh, authoritative state was available and correctly consumed before the action.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.evaluator,
                statement="The adapter or evaluator may have misclassified the observed action as prohibited.",
                rationale="A detection error remains possible until the cited evidence is manually reviewed.",
                verification_steps=[evidence_check, "Compare the raw event with the prohibition parameters and conditions."],
                falsification_condition="Manual review confirms the raw event satisfies the prohibited action and all conditions.",
            ),
        ]
        candidates = [
            _CandidateTemplate(
                change_type=ChangeType.prompt_policy,
                target_layer=CauseLayer.prompt,
                summary="Strengthen the explicit prohibition and refusal path.",
                proposed_change="Add the prohibited action, its conditions, and an approved refusal example to the versioned policy.",
                hypothesis_ranks=[1],
                risk=ChangeRisk.medium,
                verification_steps=["Run positive, negative, and adversarial contract fixtures."],
                rollback_plan="Restore the previous versioned prompt policy.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.state_guard,
                target_layer=CauseLayer.state,
                summary="Fail closed when required authorization state is unavailable.",
                proposed_change="Add a pre-action guard that blocks the action unless authoritative condition state is present and valid.",
                hypothesis_ranks=[2],
                risk=ChangeRisk.high,
                verification_steps=["Test valid, invalid, missing, and stale authorization states."],
                rollback_plan="Revert the guard configuration and retain quarantine until review.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.evaluator_rule,
                target_layer=CauseLayer.evaluator,
                summary="Calibrate the detector with reviewed evidence.",
                proposed_change="Add labelled boundary fixtures before adjusting the prohibition classifier or adapter rule.",
                hypothesis_ranks=[3],
                risk=ChangeRisk.medium,
                verification_steps=["Measure false positives and false negatives on the reviewed fixture set."],
                rollback_plan="Restore the prior rule version and its expected fixture outputs.",
            ),
        ]
    elif isinstance(clause, ConditionalClause):
        hypotheses = [
            _HypothesisTemplate(
                layer=CauseLayer.state,
                statement="The trigger condition may not have reached the decision context intact.",
                rationale="The conditional response was not observed after a matching event.",
                verification_steps=["Trace event attributes from ingestion to the model and router context."],
                falsification_condition="All condition attributes are present, fresh, and identical throughout the decision trace.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.prompt,
                statement="The active policy may not express the conditional branch clearly enough.",
                rationale="The expected branch was skipped even though its conditions matched.",
                verification_steps=["Inspect the effective prompt for a direct condition-to-action rule."],
                falsification_condition="Branch tests reliably select the expected action with the current prompt.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.orchestration,
                statement="Routing may have selected or executed the wrong branch.",
                rationale="A route-level failure can prevent an otherwise valid conditional response.",
                verification_steps=["Replay and inspect branch selection and downstream completion telemetry."],
                falsification_condition="The correct route and action completion are both proven by the trace.",
            ),
        ]
        candidates = [
            _CandidateTemplate(
                change_type=ChangeType.state_guard,
                target_layer=CauseLayer.state,
                summary="Bind the trigger state to the decision context.",
                proposed_change="Require validated condition attributes before evaluating the conditional branch.",
                hypothesis_ranks=[1],
                risk=ChangeRisk.medium,
                verification_steps=["Replay with matching, non-matching, missing, and stale conditions."],
                rollback_plan="Restore the previous state-binding rule.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.prompt_policy,
                target_layer=CauseLayer.prompt,
                summary="Make the condition-to-action branch explicit.",
                proposed_change="Add one direct conditional instruction and counterexample to the versioned policy.",
                hypothesis_ranks=[2],
                risk=ChangeRisk.medium,
                verification_steps=["Run branch coverage fixtures and the full regression set."],
                rollback_plan="Restore the previous versioned prompt policy.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.routing_policy,
                target_layer=CauseLayer.orchestration,
                summary="Add a deterministic route guard for the matched condition.",
                proposed_change="Require the expected route when validated condition state matches.",
                hypothesis_ranks=[3],
                risk=ChangeRisk.high,
                verification_steps=["Verify route choice, completion, and non-matching behavior."],
                rollback_plan="Disable the route guard and restore its previous configuration.",
            ),
        ]
    elif isinstance(clause, TemporalClause):
        hypotheses = [
            _HypothesisTemplate(
                layer=CauseLayer.tool,
                statement="The required tool or handoff may have failed or remained incomplete.",
                rationale="The required action did not complete within the contractual window.",
                verification_steps=["Inspect tool request, acknowledgement, completion, and error telemetry."],
                falsification_condition="Tool telemetry proves successful completion within the allowed window.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.orchestration,
                statement="Timeout, retry, or escalation handling may not enforce the deadline.",
                rationale="A temporal failure can occur even when the correct action was initially scheduled.",
                verification_steps=["Inspect scheduling, retry, timeout, and escalation timestamps."],
                falsification_condition="The orchestration trace enforces the deadline and completed escalation before expiry.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.evaluator,
                statement="Timestamp or completion instrumentation may be incomplete or use an inconsistent clock.",
                rationale="The evaluator requires comparable trigger and completion times.",
                verification_steps=[evidence_check, "Compare raw and canonical timestamps and clock sources."],
                falsification_condition="All timestamps are complete, correctly ordered, and use the declared clock semantics.",
            ),
        ]
        candidates = [
            _CandidateTemplate(
                change_type=ChangeType.tool_gate,
                target_layer=CauseLayer.tool,
                summary="Require confirmed tool completion.",
                proposed_change="Track acknowledgement and completion separately and treat missing completion as an error.",
                hypothesis_ranks=[1],
                risk=ChangeRisk.high,
                verification_steps=["Test success, timeout, partial completion, and tool error paths."],
                rollback_plan="Restore the previous tool integration and retain the release block.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.routing_policy,
                target_layer=CauseLayer.orchestration,
                summary="Add deadline-aware retry and escalation.",
                proposed_change="Define bounded retries and an escalation route before the contractual deadline.",
                hypothesis_ranks=[2],
                risk=ChangeRisk.high,
                verification_steps=["Run boundary-time and injected-failure scenarios."],
                rollback_plan="Disable the retry policy and restore the previous scheduler configuration.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.adapter_rule,
                target_layer=CauseLayer.evaluator,
                summary="Align completion timestamp instrumentation.",
                proposed_change="Add reviewed timestamp fixtures and normalize only the declared clock source.",
                hypothesis_ranks=[3],
                risk=ChangeRisk.medium,
                verification_steps=["Verify exact deadline boundaries and clock consistency."],
                rollback_plan="Restore the prior timestamp mapping and expected results.",
            ),
        ]
    elif isinstance(clause, RecoveryClause):
        hypotheses = [
            _HypothesisTemplate(
                layer=CauseLayer.state,
                statement="One or more required recovery signals may be absent, stale, or below threshold.",
                rationale="The recovery gate remained blocked because its declared evidence was not satisfied.",
                verification_steps=["Inspect every declared recovery signal, value, timestamp, and provenance."],
                falsification_condition="All declared recovery requirements are proven satisfied by fresh attributable evidence.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.orchestration,
                statement="The candidate may not yet have completed the required recovery workflow.",
                rationale="Approval, passing runs, cooldown, or rollback checks may still be pending.",
                verification_steps=["Trace each recovery step and identify the first unmet gate."],
                falsification_condition="The recovery workflow shows every gate completed in the required order.",
            ),
            _HypothesisTemplate(
                layer=CauseLayer.evaluator,
                statement="Recovery evidence may not be attributable to the incident candidate or contract version.",
                rationale="Unattributed evidence cannot safely restore autonomy.",
                verification_steps=[evidence_check, "Verify candidate, contract, and run identifiers on every recovery signal."],
                falsification_condition="Every signal is cryptographically or operationally attributable to this candidate and contract version.",
            ),
        ]
        candidates = [
            _CandidateTemplate(
                change_type=ChangeType.recovery_evidence,
                target_layer=CauseLayer.state,
                summary="Collect complete, fresh recovery evidence.",
                proposed_change="Record each declared recovery signal with value, timestamp, provenance, and candidate identity.",
                hypothesis_ranks=[1],
                risk=ChangeRisk.low,
                verification_steps=["Re-evaluate the unchanged recovery threshold with the complete evidence set."],
                rollback_plan="Discard the new evidence record if provenance validation fails; do not restore autonomy.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.routing_policy,
                target_layer=CauseLayer.orchestration,
                summary="Complete the existing recovery workflow without weakening it.",
                proposed_change="Keep quarantine and execute each pending approval, regression, cooldown, and rollback check.",
                hypothesis_ranks=[2],
                risk=ChangeRisk.low,
                verification_steps=["Confirm all existing gate conditions pass without threshold changes."],
                rollback_plan="Keep or restore quarantine if any recovery check fails.",
            ),
            _CandidateTemplate(
                change_type=ChangeType.evaluator_rule,
                target_layer=CauseLayer.evaluator,
                summary="Repair evidence attribution before re-evaluation.",
                proposed_change="Bind recovery signals to candidate, contract version, and run identity; do not lower thresholds.",
                hypothesis_ranks=[3],
                risk=ChangeRisk.medium,
                verification_steps=["Reject mismatched identities and accept reviewed matching evidence."],
                rollback_plan="Restore the previous evaluator and maintain quarantine pending manual review.",
            ),
        ]
    else:  # pragma: no cover - the contract union is closed
        raise TypeError(f"unsupported clause type: {type(clause).__name__}")

    return hypotheses, candidates


def _related_recovery_clauses(document: ContractDocument, clause_id: str) -> list[RecoveryClause]:
    related: list[RecoveryClause] = []
    for clause in document.clauses:
        if not isinstance(clause, RecoveryClause):
            continue
        if clause.id == clause_id or clause_id in clause.activation.clause_failure_any_of:
            related.append(clause)
    return sorted(related, key=lambda item: item.id)


def _build_card(document: ContractDocument, incident: Incident) -> ChangeCard:
    clause = next(clause for clause in document.clauses if clause.id == incident.clause_id)
    hypothesis_templates, candidate_templates = _templates_for_clause(clause)
    evidence_refs = sorted({item.ref for item in incident.evidence})

    hypotheses: list[DiagnosticHypothesis] = []
    for rank, template in enumerate(hypothesis_templates, start=1):
        digest = _canonical_hash(
            {
                "incident_id": incident.incident_id,
                "rank": rank,
                "layer": template.layer.value,
                "statement": template.statement,
                "generator_version": GENERATOR_VERSION,
            }
        )
        hypotheses.append(
            DiagnosticHypothesis(
                hypothesis_id=f"HYP-{digest[:24].upper()}",
                rank=rank,
                layer=template.layer,
                statement=template.statement,
                rationale=template.rationale,
                evidence_refs=evidence_refs,
                verification_steps=template.verification_steps,
                falsification_condition=template.falsification_condition,
            )
        )

    candidates: list[CorrectionCandidate] = []
    for rank, template in enumerate(candidate_templates, start=1):
        addressed = [hypotheses[index - 1].hypothesis_id for index in template.hypothesis_ranks]
        digest = _canonical_hash(
            {
                "incident_id": incident.incident_id,
                "rank": rank,
                "change_type": template.change_type.value,
                "proposed_change": template.proposed_change,
                "generator_version": GENERATOR_VERSION,
            }
        )
        candidates.append(
            CorrectionCandidate(
                candidate_id=f"CAND-{digest[:24].upper()}",
                rank=rank,
                change_type=template.change_type,
                target_layer=template.target_layer,
                summary=template.summary,
                proposed_change=template.proposed_change,
                addresses_hypothesis_ids=addressed,
                risk=template.risk,
                verification_steps=template.verification_steps,
                rollback_plan=template.rollback_plan,
            )
        )

    related_recovery = _related_recovery_clauses(document, clause.id)
    passing_runs = max(
        [item.requirements.consecutive_passing_runs or 1 for item in related_recovery],
        default=1,
    )
    cis_values = [
        item.requirements.minimum_cis
        for item in related_recovery
        if item.requirements.minimum_cis is not None
    ]
    minimum_cis = max(cis_values) if cis_values else None
    card_digest = _canonical_hash(
        {
            "incident_id": incident.incident_id,
            "generator_name": GENERATOR_NAME,
            "generator_version": GENERATOR_VERSION,
        }
    )
    return ChangeCard(
        card_id=f"CARD-{card_digest[:24].upper()}",
        generated_at=incident.detected_at,
        incident=incident,
        hypotheses=hypotheses,
        candidates=candidates,
        acceptance=AcceptanceCriteria(
            must_pass_clause_ids=[clause.id],
            no_regression_clause_ids=sorted(
                item.id for item in document.clauses if item.id != clause.id
            ),
            related_recovery_clause_ids=[item.id for item in related_recovery],
            required_consecutive_passing_runs=passing_runs,
            minimum_cis=minimum_cis,
        ),
        release_policy=ReleasePolicy(
            recommended_current_autonomy=incident.recommended_autonomy,
        ),
    )


def generate_change_cards(
    document: ContractDocument,
    incident_batch: IncidentBatch,
) -> ChangeCardBatch:
    """Generate one deterministic, non-executable change card per incident."""

    issues: list[ValidationIssue] = []
    if incident_batch.contract_id != document.contract.id:
        issues.append(ValidationIssue("incident_batch.contract_id", "does not match the contract id"))
    if incident_batch.contract_version != document.contract.version:
        issues.append(
            ValidationIssue(
                "incident_batch.contract_version",
                "does not match the contract version",
            )
        )

    clause_ids = {clause.id for clause in document.clauses}
    for index, incident in enumerate(incident_batch.incidents):
        prefix = f"incident_batch.incidents[{index}]"
        if incident.clause_id not in clause_ids:
            issues.append(
                ValidationIssue(f"{prefix}.clause_id", "does not exist in the contract")
            )
        if incident.contract_id != incident_batch.contract_id:
            issues.append(
                ValidationIssue(f"{prefix}.contract_id", "does not match the incident batch")
            )
        if incident.contract_version != incident_batch.contract_version:
            issues.append(
                ValidationIssue(f"{prefix}.contract_version", "does not match the incident batch")
            )
    if incident_batch.incident_count != len(incident_batch.incidents):
        issues.append(
            ValidationIssue("incident_batch.incident_count", "does not match incidents length")
        )
    if issues:
        raise DSLValidationError(issues)

    cards = [_build_card(document, incident) for incident in incident_batch.incidents]
    cards.sort(key=lambda card: (card.incident.clause_id, card.card_id))
    return ChangeCardBatch(
        contract_id=incident_batch.contract_id,
        contract_version=incident_batch.contract_version,
        trajectory_id=incident_batch.trajectory_id,
        candidate_version=incident_batch.candidate_version,
        generated_at=incident_batch.detected_at,
        card_count=len(cards),
        cards=cards,
    )


def render_change_card_markdown(card: ChangeCard) -> str:
    """Render a review-friendly Markdown representation of a change card."""

    lines = [
        f"# Change card {card.card_id}",
        "",
        "## Incident",
        "",
        f"- Incident: `{card.incident.incident_id}`",
        f"- Clause: `{card.incident.clause_id}` (`{card.incident.clause_type}`)",
        f"- Severity: `{card.incident.severity.value}`",
        f"- Trajectory: `{card.incident.trajectory_id}`",
        f"- Reason: {card.incident.reason}",
        "",
        "## Diagnostic hypotheses (untested)",
        "",
    ]
    for hypothesis in card.hypotheses:
        lines.extend(
            [
                f"### {hypothesis.rank}. {hypothesis.hypothesis_id}",
                "",
                f"**Layer:** `{hypothesis.layer.value}`  ",
                f"**Hypothesis:** {hypothesis.statement}  ",
                f"**Rationale:** {hypothesis.rationale}  ",
                f"**Falsified if:** {hypothesis.falsification_condition}",
                "",
                "Verification:",
                "",
                *[f"- {step}" for step in hypothesis.verification_steps],
                "",
            ]
        )

    lines.extend(["## Correction candidates (description only)", ""])
    for candidate in card.candidates:
        lines.extend(
            [
                f"### {candidate.rank}. {candidate.summary}",
                "",
                f"- Candidate: `{candidate.candidate_id}`",
                f"- Type/layer: `{candidate.change_type.value}` / `{candidate.target_layer.value}`",
                f"- Risk: `{candidate.risk.value}`",
                f"- Proposal: {candidate.proposed_change}",
                f"- Rollback: {candidate.rollback_plan}",
                "- Human approval required: `true`",
                "",
            ]
        )

    acceptance = card.acceptance
    lines.extend(
        [
            "## Acceptance and recovery",
            "",
            "- Must pass: " + ", ".join(f"`{item}`" for item in acceptance.must_pass_clause_ids),
            "- No regression: "
            + ", ".join(f"`{item}`" for item in acceptance.no_regression_clause_ids),
            f"- Consecutive passing runs: `{acceptance.required_consecutive_passing_runs}`",
            f"- Minimum CIS: `{acceptance.minimum_cis if acceptance.minimum_cis is not None else 'not declared'}`",
            "- Rollback required: `true`",
            "",
            "## Release policy",
            "",
            "- Automatic application: `false`",
            "- Automatic release: `false`",
            "- Human approval required: `true`",
            f"- Recommended current autonomy: `{card.release_policy.recommended_current_autonomy.value}`",
            "",
            "> " + card.disclaimer,
            "",
        ]
    )
    return "\n".join(lines)
