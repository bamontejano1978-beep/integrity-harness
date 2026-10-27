"""System 1 A2 bridge: agent-telemetry interchange exports to signed trajectories.

Only events a runtime observer or enforcement boundary reported are accepted.
Agent-asserted events are refused: they are the agent's own account and cannot
serve as sensor evidence. Raw message and tool content is never copied; each
event is reduced to its declared observation name and allowlisted attributes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from autopilot.system1_events import HarnessEvent, HarnessEventWindow, HarnessObservation, EventCoverage
from autopilot.system1_sensor import System1SensorError, System1TrajectoryEmission, emit_harness_trajectory
from autopilot.telemetry_interchange import (
    AgentTelemetryExport,
    ControlEvent,
    MessageEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from autopilot.trajectory import ObservationKind

if TYPE_CHECKING:
    from autopilot.audit import AuditLedger

MESSAGE_OBSERVATION_NAME = "agent_message"
TOOL_RESULT_OBSERVATION_NAME = "tool_result"
_SENSOR_METHODS = frozenset({"runtime_observer", "enforcement_boundary"})


def _project_event(event: ControlEvent | MessageEvent | ToolCallEvent | ToolResultEvent) -> HarnessEvent:
    method = event.evidence.observation_method
    if method not in _SENSOR_METHODS:
        raise System1SensorError(
            "invalid_event_window",
            f"event {event.event_id!r} has observation_method {method!r}; "
            "only runtime-observed events can feed A2",
        )
    if isinstance(event, MessageEvent):
        event_type = "model_output"
        kind = ObservationKind.event
        name = MESSAGE_OBSERVATION_NAME
        attributes: dict[str, object] = {"role": event.role}
    elif isinstance(event, ToolCallEvent):
        event_type = "tool_call"
        kind = ObservationKind.action
        name = event.tool_name
        attributes = event.arguments.model_dump(mode="python", exclude_none=True)
    elif isinstance(event, ToolResultEvent):
        event_type = "tool_result"
        kind = ObservationKind.event
        name = TOOL_RESULT_OBSERVATION_NAME
        attributes = {
            "call_id": event.call_id,
            "outcome": event.outcome,
            **event.result.model_dump(mode="python", exclude_none=True),
        }
    else:
        event_type = "runtime_event"
        kind = ObservationKind.event
        name = event.name
        attributes = event.attributes.model_dump(mode="python", exclude_none=True)
    observation = HarnessObservation(kind=kind, name=name, attributes=attributes)
    return HarnessEvent(
        sequence=event.sequence,
        event_id=event.event_id,
        event_type=event_type,
        observed_at=event.observed_at or event.occurred_at,
        observation=observation,
        payload_sha256=observation.digest(),
    )


def interchange_to_harness_window(
    payload: object,
    *,
    produced_at: str,
    heartbeat_at: str | None,
    window_id: str | None = None,
) -> HarnessEventWindow:
    """Project a validated interchange export into a minimized, sensor-ready window.

    Completeness is asserted only when the exporter reports a complete run and
    the caller supplies a source heartbeat. Otherwise the window is incomplete and
    the sensor refuses it.
    """

    try:
        export = AgentTelemetryExport.model_validate(payload)
    except ValidationError as exc:
        raise System1SensorError(
            "invalid_event_window", f"interchange export failed profile validation: {exc.error_count()} error(s)"
        ) from exc
    events = [_project_event(event) for event in export.events]
    if export.run.status == "complete" and heartbeat_at is not None:
        coverage = EventCoverage(
            status="complete",
            heartbeat_at=heartbeat_at,
            sequence_start=1,
            sequence_end=len(events),
            expected_event_count=len(events),
        )
    else:
        coverage = EventCoverage(status="unknown")
    try:
        return HarnessEventWindow(
            source_id=export.source.producer_alias,
            window_id=window_id or export.run.run_id,
            started_at=export.run.started_at,
            ended_at=export.run.ended_at,
            produced_at=produced_at,
            coverage=coverage,
            events=events,
        )
    except ValidationError as exc:
        raise System1SensorError("invalid_event_window", f"projected window is invalid: {exc}") from exc


def emit_interchange_trajectory(
    payload: object,
    *,
    produced_at: str,
    heartbeat_at: str | None,
    contract_path: str | Path,
    candidate_version: str,
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    trust_public_key: str | Path,
    output_path: str | Path,
    ledger: AuditLedger,
    window_id: str | None = None,
    produced_by: str = "integrity-system1-harness-producer",
    emitted_at: str | None = None,
) -> System1TrajectoryEmission:
    """Project an interchange export and emit its signed A2 trajectory via the sensor."""

    window = interchange_to_harness_window(
        payload,
        produced_at=produced_at,
        heartbeat_at=heartbeat_at,
        window_id=window_id,
    )
    return emit_harness_trajectory(
        window,
        contract_path=contract_path,
        candidate_version=candidate_version,
        producer_private_key=producer_private_key,
        producer_public_key=producer_public_key,
        trust_public_key=trust_public_key,
        output_path=output_path,
        ledger=ledger,
        produced_by=produced_by,
        emitted_at=emitted_at,
    )
