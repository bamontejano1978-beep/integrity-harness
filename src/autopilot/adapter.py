"""Deterministic adapter from raw sessions to canonical trajectories."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, ValidationError, field_validator, model_validator

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import (
    Conditions,
    ContractDocument,
    ExtensibleModel,
    Identifier,
    Scalar,
    SemVer,
    SnakeName,
    value_matches_attribute_type,
)
from autopilot.trajectory import (
    ObservationKind,
    RecoveryContext,
    Trajectory,
    TrajectoryObservation,
    parse_timestamp,
)


class MessageRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class AdapterSource(str, Enum):
    message = "message"
    tool_call = "tool_call"
    event = "event"


class AttributeSource(str, Enum):
    constant = "constant"
    data = "data"


class RawMessage(ExtensibleModel):
    ref: Annotated[str, Field(min_length=1, max_length=255)]
    role: MessageRole
    observed_at: str
    monotonic_ns: Annotated[int, Field(ge=0, strict=True)] | None = None
    monotonic_clock_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    content: Annotated[str, Field(max_length=100_000)]
    metadata: Conditions = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class RawToolCall(ExtensibleModel):
    ref: Annotated[str, Field(min_length=1, max_length=255)]
    name: SnakeName
    observed_at: str
    monotonic_ns: Annotated[int, Field(ge=0, strict=True)] | None = None
    monotonic_clock_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    arguments: Conditions = Field(default_factory=dict)
    result: Conditions = Field(default_factory=dict)
    success: StrictBool

    @field_validator("observed_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class RawEvent(ExtensibleModel):
    ref: Annotated[str, Field(min_length=1, max_length=255)]
    name: SnakeName
    observed_at: str
    monotonic_ns: Annotated[int, Field(ge=0, strict=True)] | None = None
    monotonic_clock_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    payload: Conditions = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class RawSession(ExtensibleModel):
    session_id: Annotated[str, Field(min_length=1, max_length=255)]
    contract_id: Identifier
    contract_version: SemVer
    candidate_version: SemVer
    complete: StrictBool
    evaluated_at: str
    evaluated_monotonic_ns: Annotated[int, Field(ge=0, strict=True)] | None = None
    monotonic_clock_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    messages: list[RawMessage] = Field(default_factory=list)
    tool_calls: list[RawToolCall] = Field(default_factory=list)
    events: list[RawEvent] = Field(default_factory=list)
    recovery: RecoveryContext = Field(default_factory=RecoveryContext)

    @field_validator("evaluated_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @model_validator(mode="after")
    def validate_references_and_time_bounds(self) -> RawSession:
        items = [*self.messages, *self.tool_calls, *self.events]
        refs = [item.ref for item in items]
        if len(refs) != len(set(refs)):
            raise ValueError("raw item refs must be unique across the session")
        evaluated_at = parse_timestamp(self.evaluated_at)
        future = [
            item.ref
            for item in items
            if parse_timestamp(item.observed_at) > evaluated_at
            and not (
                self.monotonic_clock_id is not None
                and item.monotonic_clock_id == self.monotonic_clock_id
                and item.monotonic_ns is not None
                and self.evaluated_monotonic_ns is not None
                and item.monotonic_ns <= self.evaluated_monotonic_ns
            )
        ]
        if future:
            raise ValueError("raw items cannot occur after evaluated_at: " + ", ".join(future))
        clock_values = [
            (item.monotonic_ns, item.monotonic_clock_id)
            for item in [*self.messages, *self.tool_calls, *self.events]
            if item.monotonic_ns is not None or item.monotonic_clock_id is not None
        ]
        if self.evaluated_monotonic_ns is not None and any(
            counter is not None
            and clock_id == self.monotonic_clock_id
            and counter > self.evaluated_monotonic_ns
            for counter, clock_id in clock_values
        ):
            raise ValueError("raw item monotonic_ns cannot exceed evaluated_monotonic_ns")
        return self


class MatchWhen(ExtensibleModel):
    role: MessageRole | None = None
    name: SnakeName | None = None
    success: StrictBool | None = None
    content_any_regex: list[Annotated[str, Field(min_length=1, max_length=500)]] | None = None
    data_equals: Conditions = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_criterion(self) -> MatchWhen:
        if (
            self.role is None
            and self.name is None
            and self.success is None
            and not self.content_any_regex
            and not self.data_equals
        ):
            raise ValueError("must declare at least one match criterion")
        return self


class AttributeMapping(ExtensibleModel):
    source: AttributeSource
    key: SnakeName | None = None
    value: Scalar | None = None

    @model_validator(mode="after")
    def validate_source_fields(self) -> AttributeMapping:
        if self.source is AttributeSource.constant:
            if self.value is None:
                raise ValueError("constant attribute mapping requires value")
            if self.key is not None:
                raise ValueError("constant attribute mapping must not declare key")
        else:
            if self.key is None:
                raise ValueError("data attribute mapping requires key")
            if self.value is not None:
                raise ValueError("data attribute mapping must not declare value")
        return self


class AdapterRule(ExtensibleModel):
    id: Identifier
    source: AdapterSource
    when: MatchWhen
    output_kind: ObservationKind
    output_name: SnakeName
    attributes: dict[SnakeName, AttributeMapping] = Field(default_factory=dict)
    ignore_case: StrictBool = True

    @field_validator("when")
    @classmethod
    def compile_regexes(cls, value: MatchWhen) -> MatchWhen:
        for pattern in value.content_any_regex or []:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_source_criteria(self) -> AdapterRule:
        if self.source is AdapterSource.message:
            if self.when.name is not None or self.when.success is not None:
                raise ValueError("message rule cannot match name or success")
        elif self.source is AdapterSource.tool_call:
            if self.when.name is None:
                raise ValueError("tool_call rule requires when.name")
            if self.when.role is not None or self.when.content_any_regex:
                raise ValueError("tool_call rule cannot match role or message content")
        elif self.source is AdapterSource.event:
            if self.when.name is None:
                raise ValueError("event rule requires when.name")
            if (
                self.when.role is not None
                or self.when.success is not None
                or self.when.content_any_regex
            ):
                raise ValueError("event rule can only match name and data_equals")
        return self


class AdapterMetadata(ExtensibleModel):
    id: Identifier
    version: SemVer
    contract_id: Identifier
    contract_version: SemVer
    description: Annotated[str, Field(min_length=1, max_length=1000)]


class AdapterConfig(ExtensibleModel):
    adapter_version: Literal["0.1"]
    adapter: AdapterMetadata
    rules: list[AdapterRule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> AdapterConfig:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("adapter rule ids must be unique")
        return self


def _parse_model(text: str, model: type[ExtensibleModel], *, source: str) -> Any:
    if not text.strip():
        raise DSLValidationError([ValidationIssue("document", f"{source} is empty")])
    raw = load_yaml_mapping(text)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def parse_adapter_config(text: str, *, source: str = "<memory>") -> AdapterConfig:
    return _parse_model(text, AdapterConfig, source=source)


def parse_raw_session(text: str, *, source: str = "<memory>") -> RawSession:
    return _parse_model(text, RawSession, source=source)


def _load_text(path: str | Path) -> tuple[Path, str]:
    resolved = Path(path)
    try:
        return resolved, resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc


def load_adapter_config(path: str | Path) -> AdapterConfig:
    resolved, text = _load_text(path)
    return parse_adapter_config(text, source=str(resolved))


def load_raw_session(path: str | Path) -> RawSession:
    resolved, text = _load_text(path)
    return parse_raw_session(text, source=str(resolved))


def _validate_config(contract: ContractDocument, config: AdapterConfig) -> None:
    issues: list[ValidationIssue] = []
    if config.adapter.contract_id != contract.contract.id:
        issues.append(ValidationIssue("adapter.contract_id", "does not match the contract id"))
    if config.adapter.contract_version != contract.contract.version:
        issues.append(
            ValidationIssue("adapter.contract_version", "does not match the contract version")
        )

    events = set(contract.vocabulary.events)
    actions = set(contract.vocabulary.actions)
    attributes = contract.vocabulary.attributes
    for index, rule in enumerate(config.rules):
        base = f"rules[{index}]"
        allowed_names = events if rule.output_kind is ObservationKind.event else actions
        if rule.output_name not in allowed_names:
            issues.append(
                ValidationIssue(
                    f"{base}.output_name",
                    f"is not a declared {rule.output_kind.value} name",
                )
            )
        for target, mapping in rule.attributes.items():
            expected = attributes.get(target)
            if expected is None:
                issues.append(
                    ValidationIssue(f"{base}.attributes.{target}", "is not declared in vocabulary")
                )
            elif (
                mapping.source is AttributeSource.constant
                and not value_matches_attribute_type(mapping.value, expected)
            ):
                issues.append(
                    ValidationIssue(
                        f"{base}.attributes.{target}.value",
                        f"expected {expected.value}, got {type(mapping.value).__name__}",
                    )
                )
    if issues:
        raise DSLValidationError(issues)


def _matches(
    rule: AdapterRule,
    *,
    role: MessageRole | None,
    name: str | None,
    success: bool | None,
    content: str,
    data: dict[str, Any],
) -> bool:
    when = rule.when
    if when.role is not None and role is not when.role:
        return False
    if when.name is not None and name != when.name:
        return False
    if when.success is not None and success is not when.success:
        return False
    if any(data.get(key) != value for key, value in when.data_equals.items()):
        return False
    if when.content_any_regex:
        flags = re.IGNORECASE if rule.ignore_case else 0
        if not any(re.search(pattern, content, flags=flags) for pattern in when.content_any_regex):
            return False
    return True


def _mapped_attributes(
    contract: ContractDocument,
    rule: AdapterRule,
    data: dict[str, Any],
    *,
    issue_path: str,
) -> tuple[dict[str, Any], list[ValidationIssue]]:
    attributes: dict[str, Any] = {}
    issues: list[ValidationIssue] = []
    for target, mapping in rule.attributes.items():
        if mapping.source is AttributeSource.constant:
            value = mapping.value
        elif mapping.key in data:
            value = data[mapping.key]
        else:
            continue

        expected = contract.vocabulary.attributes[target]
        if not value_matches_attribute_type(value, expected):
            issues.append(
                ValidationIssue(
                    f"{issue_path}.attributes.{target}",
                    f"expected {expected.value}, got {type(value).__name__}",
                )
            )
        else:
            attributes[target] = value
    return attributes, issues


def adapt_session(
    contract: ContractDocument,
    config: AdapterConfig,
    session: RawSession,
) -> Trajectory:
    """Transform a raw session into a validated canonical trajectory."""

    _validate_config(contract, config)
    issues: list[ValidationIssue] = []
    if session.contract_id != contract.contract.id:
        issues.append(ValidationIssue("session.contract_id", "does not match the contract id"))
    if session.contract_version != contract.contract.version:
        issues.append(
            ValidationIssue("session.contract_version", "does not match the contract version")
        )
    if issues:
        raise DSLValidationError(issues)

    source_items: list[
        tuple[AdapterSource, str, str, MessageRole | None, str | None, bool | None, str, dict]
    ] = []
    source_items.extend(
        (
            AdapterSource.message,
            item.ref,
            item.observed_at,
            item.role,
            None,
            None,
            item.content,
            item.metadata,
        )
        for item in session.messages
    )
    source_items.extend(
        (
            AdapterSource.tool_call,
            item.ref,
            item.observed_at,
            None,
            item.name,
            item.success,
            "",
            item.arguments,
        )
        for item in session.tool_calls
    )
    source_items.extend(
        (
            AdapterSource.event,
            item.ref,
            item.observed_at,
            None,
            item.name,
            None,
            "",
            item.payload,
        )
        for item in session.events
    )

    observations: list[TrajectoryObservation] = []
    raw_by_ref = {item.ref: item for item in [*session.messages, *session.tool_calls, *session.events]}
    for source, ref, observed_at, role, name, success, content, data in source_items:
        for rule_index, rule in enumerate(config.rules):
            if rule.source is not source:
                continue
            if not _matches(
                rule,
                role=role,
                name=name,
                success=success,
                content=content,
                data=data,
            ):
                continue
            attributes, attribute_issues = _mapped_attributes(
                contract,
                rule,
                data,
                issue_path=f"source[{ref}]/rules[{rule_index}]",
            )
            issues.extend(attribute_issues)
            observations.append(
                TrajectoryObservation(
                    ref=f"{ref}/mapping-{rule.id}",
                    kind=rule.output_kind,
                    name=rule.output_name,
                    observed_at=observed_at,
                    monotonic_ns=getattr(raw_by_ref[ref], "monotonic_ns", None),
                    monotonic_clock_id=getattr(raw_by_ref[ref], "monotonic_clock_id", None),
                    attributes=attributes,
                )
            )

    if issues:
        raise DSLValidationError(issues)

    observations.sort(
        key=lambda observation: (parse_timestamp(observation.observed_at), observation.ref)
    )
    return Trajectory(
        trajectory_id=f"{session.session_id}/adapted-{config.adapter.id}-v{config.adapter.version}",
        contract_id=session.contract_id,
        contract_version=session.contract_version,
        candidate_version=session.candidate_version,
        complete=session.complete,
        evaluated_at=session.evaluated_at,
        evaluated_monotonic_ns=session.evaluated_monotonic_ns,
        monotonic_clock_id=session.monotonic_clock_id,
        observations=observations,
        recovery=session.recovery,
        **{
            key: value
            for key, value in (session.model_extra or {}).items()
            if key.startswith("x_")
        },
    )
