"""Typed models for Behavioural Contracts DSL v0.1."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

IDENTIFIER_PATTERN = r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"
SNAKE_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"

Identifier = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
SnakeName = Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
SemVer = Annotated[str, Field(pattern=SEMVER_PATTERN)]
Scalar = StrictBool | StrictInt | StrictFloat | StrictStr
Conditions = dict[SnakeName, Scalar]
Parameters = dict[SnakeName, Scalar]


class ExtensibleModel(BaseModel):
    """Strict model that only permits unknown fields carrying the ``x_`` prefix."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        allowed: set[str] = set()
        for name, field in cls.model_fields.items():
            allowed.add(name)
            if isinstance(field.alias, str):
                allowed.add(field.alias)

        unknown = sorted(
            str(key)
            for key in value
            if key not in allowed and not (isinstance(key, str) and key.startswith("x_"))
        )
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(unknown)}")
        return value


class ContractStatus(str, Enum):
    draft = "draft"
    active = "active"
    retired = "retired"


class AttributeType(str, Enum):
    boolean = "boolean"
    integer = "integer"
    number = "number"
    string = "string"
    timestamp = "timestamp"


def value_matches_attribute_type(value: Any, expected: AttributeType) -> bool:
    """Return whether a strict scalar value matches a DSL vocabulary type."""

    if expected is AttributeType.boolean:
        return type(value) is bool
    if expected is AttributeType.integer:
        return type(value) is int
    if expected is AttributeType.number:
        return type(value) in {int, float}
    if expected is AttributeType.string:
        return type(value) is str
    if expected is AttributeType.timestamp:
        if type(value) is not str:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None
    return False


class EvaluationMethod(str, Enum):
    rule_based = "rule_based"
    llm_judge = "llm_judge"
    hybrid = "hybrid"
    manual = "manual"


class IndeterminatePolicy(str, Enum):
    manual_review = "manual_review"
    fail_closed = "fail_closed"
    warn = "warn"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class AutonomyLevel(str, Enum):
    COACH = "COACH"
    SUPERVISE = "SUPERVISE"
    RESTRICT = "RESTRICT"
    QUARANTINE = "QUARANTINE"


class ClockType(str, Enum):
    monotonic = "monotonic"
    wall = "wall"


class EvaluationPolicy(ExtensibleModel):
    method: EvaluationMethod
    confidence_floor: Annotated[float, Field(ge=0.0, le=1.0, strict=True)]
    on_indeterminate: IndeterminatePolicy


class FailurePolicy(ExtensibleModel):
    autonomy_level: AutonomyLevel
    block_release: StrictBool
    notify: list[SnakeName] = Field(default_factory=list)


class ContractDefaults(ExtensibleModel):
    evaluation: EvaluationPolicy
    on_failure: FailurePolicy


class ContractMetadata(ExtensibleModel):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: SemVer
    status: ContractStatus
    domain: SnakeName
    owner: SnakeName
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    defaults: ContractDefaults
    effective_from: str | None = None
    supersedes: SemVer | None = None
    tags: list[SnakeName] = Field(default_factory=list)
    # B2 additive pre-stage: optional top-level standing-approval envelope.
    # Existing v0.1 contracts omit this field and keep their current
    # direct-Ed25519 review flow.
    standing_approval_window: StandingApprovalWindow | None = None

    @field_validator("effective_from")
    @classmethod
    def validate_rfc3339(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("must include a timezone")
        return value


class Vocabulary(ExtensibleModel):
    events: list[SnakeName]
    actions: list[SnakeName]
    attributes: dict[SnakeName, AttributeType]


class EvaluationOverride(ExtensibleModel):
    method: EvaluationMethod
    confidence_floor: Annotated[float, Field(ge=0.0, le=1.0, strict=True)]
    on_indeterminate: IndeterminatePolicy


class Trigger(ExtensibleModel):
    event: SnakeName
    conditions: Conditions = Field(default_factory=dict)


class ActionRequirement(ExtensibleModel):
    action: SnakeName
    parameters: Parameters = Field(default_factory=dict)
    within_seconds: Annotated[int, Field(ge=1, le=31_536_000, strict=True)] | None = None


class TemporalActionRequirement(ExtensibleModel):
    action: SnakeName
    parameters: Parameters = Field(default_factory=dict)


class Prohibition(ExtensibleModel):
    action: SnakeName
    conditions: Conditions = Field(default_factory=dict)


class TemporalWindow(ExtensibleModel):
    within_seconds: Annotated[int, Field(ge=1, le=31_536_000, strict=True)]
    clock: ClockType


class StandingApprovalWindow(ExtensibleModel):
    """Top-level standing-approval envelope (B2 additive pre-stage, v0.1-compatible).

    A bounded auto-signature envelope that, in B2 proper, lets the
    human-conductor auto-sign releases whose scope satisfies the listed
    clause ids and whose accumulated auto-sign count does not exceed
    ``risk_budget`` before ``expiry``. v0.1 stores the envelope as
    declarative metadata only; the B2 release-review module is the
    interpreter. Migration to v0.2 keeps the field semantics.

    The envelope is optional; contracts that omit it continue to use the
    direct Ed25519 release-review path with no change in semantics.
    """

    risk_budget: Annotated[int, Field(ge=1, le=10_000, strict=True)]
    scope: list[Identifier] = Field(min_length=1)
    expiry: str

    @field_validator("expiry")
    @classmethod
    def validate_rfc3339(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an RFC 3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("must include a timezone")
        return value

    @model_validator(mode="after")
    def scope_ids_unique(self) -> StandingApprovalWindow:
        duplicates = sorted(
            {clause_id for clause_id in self.scope if self.scope.count(clause_id) > 1}
        )
        if duplicates:
            raise ValueError(
                "scope clause ids must be unique: " + ", ".join(duplicates)
            )
        return self


class OperationalBudgets(ExtensibleModel):
    """Hard execution limits enforced from environment-observed telemetry."""

    max_tool_calls: Annotated[int, Field(ge=0, le=100_000, strict=True)]
    max_claim_payment_eur: Annotated[
        float, Field(ge=0.0, le=1_000_000_000.0, strict=True)
    ]


class OperationalEvidencePolicy(ExtensibleModel):
    """Minimum provenance guarantees for containment evaluation."""

    require_external_observation: StrictBool = True
    require_complete_trace: StrictBool = True


class OperationalIntegrityPolicy(ExtensibleModel):
    """Optional containment policy, independent from behavioural clauses."""

    allowed_tools: list[SnakeName] = Field(default_factory=list)
    stop_conditions: list[SnakeName] = Field(default_factory=list)
    budgets: OperationalBudgets
    require_human_authorization_for_payment: StrictBool = True
    evidence: OperationalEvidencePolicy = Field(default_factory=OperationalEvidencePolicy)

    @model_validator(mode="after")
    def values_are_unique(self) -> OperationalIntegrityPolicy:
        for name, values in (
            ("allowed_tools", self.allowed_tools),
            ("stop_conditions", self.stop_conditions),
        ):
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"{name} values must be unique: {', '.join(duplicates)}")
        return self


class ClauseBase(ExtensibleModel):
    id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    severity: Severity
    tags: list[SnakeName] = Field(default_factory=list)
    evaluation: EvaluationOverride | None = None
    on_failure: FailurePolicy | None = None
    # B2 additive pre-stage: optional per-clause delegation chain. When
    # present, the ordered list of ``SnakeName`` actor ids represents the
    # signing chain (root → … → delegate) that the B2 release-review
    # verifier will validate. v0.1 stores it as declarative metadata only.
    # Defaults to ``None`` so every existing v0.1 example keeps parsing.
    delegate_to: list[SnakeName] | None = None


class DebeClause(ClauseBase):
    type: Literal["debe"]
    trigger: Trigger
    requirement: ActionRequirement


class NoDebeClause(ClauseBase):
    type: Literal["no_debe"]
    prohibition: Prohibition
    patterns_to_avoid: list[Annotated[str, Field(min_length=1)]] | None = None

    @field_validator("patterns_to_avoid")
    @classmethod
    def reject_empty_patterns(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not value:
            raise ValueError("must contain at least one pattern when provided")
        return value


class ConditionalClause(ClauseBase):
    type: Literal["si_entonces"]
    if_: Trigger = Field(alias="if")
    then: ActionRequirement


class TemporalClause(ClauseBase):
    type: Literal["limite_temporal"]
    trigger: Trigger
    requirement: TemporalActionRequirement
    window: TemporalWindow


class RecoveryActivation(ExtensibleModel):
    clause_failure_any_of: list[Identifier] = Field(min_length=1)


class RecoveryAutonomy(ExtensibleModel):
    on_activation: AutonomyLevel
    restore_to: AutonomyLevel


class RecoveryRequirements(ExtensibleModel):
    human_approval: StrictBool | None = None
    consecutive_passing_runs: Annotated[int, Field(ge=1, le=1000, strict=True)] | None = None
    minimum_cis: Annotated[int, Field(ge=0, le=100, strict=True)] | None = None
    rollback_available: StrictBool | None = None
    cooldown_seconds: Annotated[int, Field(ge=0, le=31_536_000, strict=True)] | None = None

    @model_validator(mode="after")
    def require_at_least_one_condition(self) -> RecoveryRequirements:
        declared = {
            "human_approval": self.human_approval,
            "consecutive_passing_runs": self.consecutive_passing_runs,
            "minimum_cis": self.minimum_cis,
            "rollback_available": self.rollback_available,
            "cooldown_seconds": self.cooldown_seconds,
        }
        if all(value is None for value in declared.values()):
            raise ValueError("must declare at least one recovery requirement")
        if self.human_approval is False:
            raise ValueError("human_approval must be true when declared")
        if self.rollback_available is False:
            raise ValueError("rollback_available must be true when declared")
        return self


class RecoveryClause(ClauseBase):
    type: Literal["recuperacion_condicion"]
    activation: RecoveryActivation
    autonomy: RecoveryAutonomy
    requirements: RecoveryRequirements


Clause = Annotated[
    DebeClause | NoDebeClause | ConditionalClause | TemporalClause | RecoveryClause,
    Field(discriminator="type"),
]


class ContractDocument(ExtensibleModel):
    dsl_version: Literal["0.1"]
    contract: ContractMetadata
    vocabulary: Vocabulary
    clauses: list[Clause] = Field(min_length=1)
    operational_integrity: OperationalIntegrityPolicy | None = None
