"""Canonical trajectory input models for deterministic contract evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, ValidationError, field_validator, model_validator

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import (
    Conditions,
    ExtensibleModel,
    Identifier,
    SemVer,
    SnakeName,
)


def parse_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 timestamp and require an explicit timezone."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


def _validate_timestamp(value: str) -> str:
    parse_timestamp(value)
    return value


class ObservationKind(str, Enum):
    event = "event"
    action = "action"


class TrajectoryObservation(ExtensibleModel):
    ref: Annotated[str, Field(min_length=1, max_length=255)]
    kind: ObservationKind
    name: SnakeName
    observed_at: str
    monotonic_ns: Annotated[int, Field(ge=0, strict=True)] | None = None
    monotonic_clock_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    attributes: Conditions = Field(default_factory=dict)

    _timestamp = field_validator("observed_at")(_validate_timestamp)


class PriorClauseFailure(ExtensibleModel):
    clause_id: Identifier
    failed_at: str
    candidate_version: SemVer

    _timestamp = field_validator("failed_at")(_validate_timestamp)


class RecoveryEvidence(ExtensibleModel):
    """Digest-bound pointer to an externally retained recovery artifact.

    The evaluator checks identity binding and digest syntax. The referenced
    artifact must still be verified against its digest by the evidence store;
    this pointer alone does not authenticate its issuer.
    """

    requirement: Literal[
        "prior_failure",
        "human_approval",
        "consecutive_passing_runs",
        "minimum_cis",
        "rollback_available",
    ]
    ref: Annotated[str, Field(min_length=1, max_length=512)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    value_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    contract_id: Identifier
    contract_version: SemVer
    candidate_version: SemVer
    observed_at: str
    clause_id: Identifier | None = None

    _timestamp = field_validator("observed_at")(_validate_timestamp)

    @model_validator(mode="after")
    def validate_subject(self) -> RecoveryEvidence:
        if (self.requirement == "prior_failure") != (self.clause_id is not None):
            raise ValueError("clause_id is required only for prior_failure evidence")
        return self


class HumanApproval(ExtensibleModel):
    approved: StrictBool
    actor: SnakeName
    observed_at: str

    _timestamp = field_validator("observed_at")(_validate_timestamp)


class RecoveryContext(ExtensibleModel):
    prior_failures: list[PriorClauseFailure] = Field(default_factory=list)
    human_approval: HumanApproval | None = None
    consecutive_passing_runs: Annotated[int, Field(ge=0, le=1000, strict=True)] | None = None
    cis: Annotated[int, Field(ge=0, le=100, strict=True)] | None = None
    rollback_available: StrictBool | None = None
    evidence: list[RecoveryEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence_requirements(self) -> RecoveryContext:
        keys = [(item.requirement, item.clause_id) for item in self.evidence]
        if len(keys) != len(set(keys)):
            raise ValueError("recovery evidence requirement references must be unique")
        return self


class Trajectory(ExtensibleModel):
    trajectory_id: Annotated[str, Field(min_length=1, max_length=255)]
    contract_id: Identifier
    contract_version: SemVer
    candidate_version: SemVer
    complete: StrictBool
    evaluated_at: str
    evaluated_monotonic_ns: Annotated[int, Field(ge=0, strict=True)] | None = None
    monotonic_clock_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    observations: list[TrajectoryObservation] = Field(default_factory=list)
    recovery: RecoveryContext = Field(default_factory=RecoveryContext)

    _timestamp = field_validator("evaluated_at")(_validate_timestamp)

    @model_validator(mode="after")
    def validate_unique_references_and_time_bounds(self) -> Trajectory:
        refs = [observation.ref for observation in self.observations]
        if len(refs) != len(set(refs)):
            raise ValueError("observation refs must be unique")

        evaluated_at = parse_timestamp(self.evaluated_at)
        future_refs = [
            observation.ref
            for observation in self.observations
            if parse_timestamp(observation.observed_at) > evaluated_at
            and not (
                self.monotonic_clock_id is not None
                and observation.monotonic_clock_id == self.monotonic_clock_id
                and observation.monotonic_ns is not None
                and self.evaluated_monotonic_ns is not None
                and observation.monotonic_ns <= self.evaluated_monotonic_ns
            )
        ]
        if future_refs:
            raise ValueError(
                "observations cannot occur after evaluated_at: " + ", ".join(future_refs)
            )

        future_failures = [
            failure.clause_id
            for failure in self.recovery.prior_failures
            if parse_timestamp(failure.failed_at) > evaluated_at
        ]
        if future_failures:
            raise ValueError(
                "prior failures cannot occur after evaluated_at: "
                + ", ".join(future_failures)
            )
        if (
            self.recovery.human_approval is not None
            and parse_timestamp(self.recovery.human_approval.observed_at) > evaluated_at
        ):
            raise ValueError("human approval cannot occur after evaluated_at")
        monotonic_values = [
            (item.monotonic_ns, item.monotonic_clock_id)
            for item in self.observations
            if item.monotonic_ns is not None or item.monotonic_clock_id is not None
        ]
        if self.evaluated_monotonic_ns is not None and any(
            counter is not None
            and clock_id == self.monotonic_clock_id
            and counter > self.evaluated_monotonic_ns
            for counter, clock_id in monotonic_values
        ):
            raise ValueError("observation monotonic_ns cannot exceed evaluated_monotonic_ns")
        for index, item in enumerate(self.recovery.evidence):
            if parse_timestamp(item.observed_at) > evaluated_at:
                raise ValueError(f"recovery evidence[{index}] cannot occur after evaluated_at")
        return self


def parse_trajectory(text: str, *, source: str = "<memory>") -> Trajectory:
    """Parse a canonical trajectory from safe YAML or JSON text."""

    if not text.strip():
        raise DSLValidationError([ValidationIssue("document", f"{source} is empty")])
    raw = load_yaml_mapping(text)
    try:
        return Trajectory.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def load_trajectory(path: str | Path) -> Trajectory:
    """Load a UTF-8 canonical trajectory from disk."""

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
    return parse_trajectory(text, source=str(resolved))
