"""Agent Telemetry Interchange Profile v0.1, an offline canonical input adapter.

The profile borrows correlation and timestamp concepts from OpenTelemetry Logs,
but provenance labels are descriptive claims, not cryptographic verification.
Only synthetic exports are accepted by this first reference implementation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import ValidationError, field_validator, model_validator

from autopilot.adapter import RawSession
from autopilot.strategyai_telemetry import (
    SCHEMA_VERSION as STRATEGYAI_SCHEMA_VERSION,
    TelemetryImportError,
    parse_export,
)
from autopilot.trajectory import parse_timestamp

PROFILE_VERSION = "agent-telemetry-interchange-0.1"
_ALIAS_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"
_TRACE_ID_PATTERN = r"^[0-9a-f]{32}$"
_SPAN_ID_PATTERN = r"^[0-9a-f]{16}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ScalarMap(_StrictModel):
    """Allowlisted scalar attributes shared by tool and control events."""

    resource_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)] | None = None
    queue_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)] | None = None
    queue: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)] | None = None
    amount_bucket: Literal["lt_100", "100_500", "500_plus", "unknown"] | None = None
    approval_state: Literal["approved", "not_approved", "unknown"] | StrictBool | None = None
    decision: Literal["allow", "deny", "unknown", "unavailable"] | None = None
    role_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)] | None = None
    licensed_adjuster_approval: StrictBool | None = None
    scope: Literal["intake_only"] | None = None

    @model_validator(mode="after")
    def reject_null_attributes(self) -> _ScalarMap:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("allowlisted attributes must not be null")
        return self


class EventEvidence(_StrictModel):
    """Declared observation origin; it does not establish authenticity."""

    observer_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    observation_method: Literal[
        "agent_assertion", "runtime_observer", "enforcement_boundary"
    ]


class _EventBase(_StrictModel):
    event_id: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    sequence: Annotated[StrictInt, Field(ge=1)]
    occurred_at: StrictStr
    observed_at: StrictStr | None = None
    trace_id: Annotated[StrictStr, Field(pattern=_TRACE_ID_PATTERN)] | None = None
    span_id: Annotated[StrictStr, Field(pattern=_SPAN_ID_PATTERN)] | None = None
    evidence: EventEvidence

    @field_validator("occurred_at", "observed_at")
    @classmethod
    def validate_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            parse_timestamp(value)
        return value

    @model_validator(mode="after")
    def span_requires_trace(self):
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("span_id requires trace_id for correlation")
        return self


class MessageEvent(_EventBase):
    kind: Literal["message"]
    role: Literal["user", "assistant", "tool"]
    content: Annotated[StrictStr, Field(min_length=1, max_length=16_000)]


class ToolCallEvent(_EventBase):
    kind: Literal["tool_call"]
    call_id: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    tool_name: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
    arguments: _ScalarMap


class ToolResultEvent(_EventBase):
    kind: Literal["tool_result"]
    call_id: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    outcome: Literal["success", "error", "blocked"]
    result: _ScalarMap


class ControlEvent(_EventBase):
    kind: Literal["control_event"]
    name: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
    attributes: _ScalarMap


InterchangeEvent = Annotated[
    Union[MessageEvent, ToolCallEvent, ToolResultEvent, ControlEvent],
    Field(discriminator="kind"),
]


class InterchangeSource(_StrictModel):
    producer_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    exporter_name: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    exporter_version: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]


class InterchangeRun(_StrictModel):
    run_id: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    scenario_id: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    agent_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    agent_version: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    brain_config_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    started_at: StrictStr
    ended_at: StrictStr
    status: Literal["complete", "incomplete", "unknown"]

    @field_validator("started_at", "ended_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @model_validator(mode="after")
    def end_not_before_start(self) -> InterchangeRun:
        if parse_timestamp(self.ended_at) < parse_timestamp(self.started_at):
            raise ValueError("ended_at must not precede started_at")
        return self


class AgentTelemetryExport(_StrictModel):
    """Versioned, transport-neutral interchange document."""

    schema_version: Literal["agent-telemetry-interchange-0.1"]
    classification: Literal["synthetic"]
    redaction_reviewed: Literal[True]
    source: InterchangeSource
    run: InterchangeRun
    events: Annotated[list[InterchangeEvent], Field(min_length=1, max_length=1_000)]

    @model_validator(mode="after")
    def validate_event_integrity(self) -> AgentTelemetryExport:
        seen: set[str] = set()
        previous_time: datetime | None = None
        start_time = parse_timestamp(self.run.started_at)
        end_time = parse_timestamp(self.run.ended_at)
        for index, event in enumerate(self.events, start=1):
            if event.sequence != index:
                raise ValueError("event sequence must be contiguous and start at 1")
            if event.event_id in seen:
                raise ValueError("event ids must be unique")
            seen.add(event.event_id)
            event_time = parse_timestamp(event.occurred_at)
            if event_time < start_time or event_time > end_time:
                raise ValueError("event timestamp must be inside the run interval")
            if previous_time is not None and event_time < previous_time:
                raise ValueError("events must be chronological by sequence")
            previous_time = event_time

        calls: set[str] = set()
        results: set[str] = set()
        for event in self.events:
            if isinstance(event, ToolCallEvent):
                if event.call_id in calls:
                    raise ValueError("tool call ids must be unique")
                calls.add(event.call_id)
            elif isinstance(event, ToolResultEvent):
                if event.call_id not in calls or event.call_id in results:
                    raise ValueError("tool result must match one earlier call exactly once")
                results.add(event.call_id)
        if calls != results:
            raise ValueError("every tool call must have exactly one result")
        return self


def parse_interchange(
    payload: object,
    *,
    contract_id: str,
    contract_version: str,
    candidate_version: str,
) -> tuple[RawSession, dict[str, object]]:
    """Validate a profile export and map it to ``RawSession`` without network I/O."""
    try:
        document = AgentTelemetryExport.model_validate(payload)
    except ValidationError:
        raise TelemetryImportError("interchange: schema or provenance validation failed") from None

    normalized = document.model_dump(mode="json", exclude_none=True)
    normalized["schema_version"] = STRATEGYAI_SCHEMA_VERSION
    normalized["run"]["complete"] = normalized["run"].pop("status") == "complete"
    normalized.pop("source")
    for event in normalized["events"]:
        for field in ("evidence", "observed_at", "trace_id", "span_id"):
            event.pop(field, None)

    session, report = parse_export(
        normalized,
        contract_id=contract_id,
        contract_version=contract_version,
        candidate_version=candidate_version,
    )
    provenance = {
        "profile_version": PROFILE_VERSION,
        "source": document.source.model_dump(mode="json"),
        "run_status_claimed": document.run.status,
        "events": {
            event.event_id: {
                "observer_alias": event.evidence.observer_alias,
                "observation_method": event.evidence.observation_method,
                **({"observed_at": event.observed_at} if event.observed_at else {}),
                **({"trace_id": event.trace_id} if event.trace_id else {}),
                **({"span_id": event.span_id} if event.span_id else {}),
            }
            for event in document.events
        },
        "authenticity": "not_verified_by_profile",
    }
    session_data = session.model_dump(mode="python", exclude_none=True)
    session_data["x_telemetry_provenance"] = provenance
    session = RawSession.model_validate(session_data)

    method_counts = {
        method: sum(
            event.evidence.observation_method == method for event in document.events
        )
        for method in ("agent_assertion", "runtime_observer", "enforcement_boundary")
    }
    report.update(
        {
            "profile_version": PROFILE_VERSION,
            "producer_alias": document.source.producer_alias,
            "exporter_name": document.source.exporter_name,
            "exporter_version": document.source.exporter_version,
            "run_status_claimed": document.run.status,
            "run_complete_effective": session.complete,
            "observation_methods": method_counts,
            "authenticity": "not_verified_by_profile",
        }
    )
    return session, report


def json_schema() -> dict[str, object]:
    """Return the JSON Schema generated from the authoritative Pydantic models."""
    return AgentTelemetryExport.model_json_schema(mode="validation")
