"""System 1 A1 event-window contract for the deterministic harness source.

This is a schema/validation boundary, not a production telemetry collector. A
coverage declaration is self-reported by the harness until a separately
trusted producer signs it (A2); ``complete`` is therefore not proof that an
external runtime emitted every event.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from autopilot.contracts.models import Conditions, SnakeName
from autopilot.trajectory import ObservationKind, parse_timestamp


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


class HarnessObservation(BaseModel):
    """Minimized semantic observation consumed by the contract evaluator."""

    model_config = ConfigDict(extra="forbid")

    kind: ObservationKind
    name: SnakeName
    attributes: Conditions = Field(default_factory=dict)

    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.model_dump(mode="json"))).hexdigest()

EVENT_WINDOW_SCHEMA_VERSION = "0.1"


class HarnessEvent(BaseModel):
    """One minimized event reference; raw message/tool payloads are not accepted."""

    model_config = ConfigDict(extra="forbid")

    sequence: Annotated[int, Field(ge=1, strict=True)]
    event_id: Annotated[str, Field(min_length=1, max_length=255)]
    event_type: Literal["model_output", "tool_call", "tool_result", "handoff", "runtime_event"]
    observed_at: str
    observation: HarnessObservation
    payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    data_labels: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list
    )

    _timestamp = field_validator("observed_at")(lambda value: _validate_timestamp(value))

    @model_validator(mode="after")
    def labels_and_digest_are_valid(self) -> HarnessEvent:
        if len(self.data_labels) != len(set(self.data_labels)):
            raise ValueError("data_labels must be unique")
        if self.payload_sha256 != self.observation.digest():
            raise ValueError("payload_sha256 must match the minimized observation projection")
        return self


class EventCoverage(BaseModel):
    """Source-declared sequence range; unknown means completeness is not asserted."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "partial", "unknown"]
    heartbeat_at: str | None = None
    sequence_start: Annotated[int, Field(ge=1, strict=True)] | None = None
    sequence_end: Annotated[int, Field(ge=1, strict=True)] | None = None
    expected_event_count: Annotated[int, Field(ge=0, strict=True)] | None = None

    _heartbeat_timestamp = field_validator("heartbeat_at")(
        lambda value: _validate_timestamp(value) if value is not None else None
    )

    @model_validator(mode="after")
    def range_matches_expected_count(self) -> EventCoverage:
        declared = (self.sequence_start, self.sequence_end, self.expected_event_count)
        if self.status == "unknown":
            if any(value is not None for value in declared) or self.heartbeat_at is not None:
                raise ValueError("unknown coverage must not claim sequence bounds, count or heartbeat")
            return self
        if self.heartbeat_at is None:
            raise ValueError("complete/partial coverage requires a source heartbeat")
        if self.expected_event_count is None:
            raise ValueError("complete/partial coverage requires expected_event_count")
        if self.expected_event_count == 0:
            if self.sequence_start is not None or self.sequence_end is not None:
                raise ValueError("an empty covered range must not declare sequence bounds")
            if self.status == "partial":
                raise ValueError("an empty event range cannot be partial")
            return self
        if self.sequence_start is None or self.sequence_end is None:
            raise ValueError("non-empty coverage requires sequence_start and sequence_end")
        if self.sequence_end < self.sequence_start:
            raise ValueError("sequence_end must be greater than or equal to sequence_start")
        range_size = self.sequence_end - self.sequence_start + 1
        if range_size != self.expected_event_count:
            raise ValueError("sequence range size must equal expected_event_count")
        return self


class HarnessEventWindow(BaseModel):
    """A bounded, minimized event batch from the declared local harness source."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = EVENT_WINDOW_SCHEMA_VERSION
    source_kind: Literal["harness"] = "harness"
    source_id: Annotated[str, Field(min_length=1, max_length=255)]
    window_id: Annotated[str, Field(min_length=1, max_length=255)]
    started_at: str
    ended_at: str
    produced_at: str
    coverage: EventCoverage
    events: list[HarnessEvent] = Field(default_factory=list)

    _timestamps = field_validator("started_at", "ended_at", "produced_at")(
        lambda value: _validate_timestamp(value)
    )

    @model_validator(mode="after")
    def validate_window_and_coverage(self) -> HarnessEventWindow:
        start = parse_timestamp(self.started_at)
        end = parse_timestamp(self.ended_at)
        produced = parse_timestamp(self.produced_at)
        if end < start:
            raise ValueError("ended_at must not precede started_at")
        if produced < end:
            raise ValueError("produced_at must not precede ended_at")
        if self.coverage.heartbeat_at is not None:
            heartbeat = parse_timestamp(self.coverage.heartbeat_at)
            if heartbeat < end:
                raise ValueError("source heartbeat must be at or after ended_at")
            if heartbeat > produced:
                raise ValueError("source heartbeat must not occur after produced_at")

        sequences = [event.sequence for event in self.events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("event sequences must be strictly increasing and unique")
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event_id values must be unique within a window")
        for event in self.events:
            observed = parse_timestamp(event.observed_at)
            if observed < start or observed > end:
                raise ValueError(f"event {event.event_id} falls outside the declared window")

        declaration = self.coverage
        if declaration.status == "unknown":
            return self
        if len(self.events) > (declaration.expected_event_count or 0):
            raise ValueError("observed event count exceeds expected_event_count")
        if declaration.expected_event_count == 0:
            if self.events:
                raise ValueError("empty coverage declaration cannot contain events")
            return self

        assert declaration.sequence_start is not None and declaration.sequence_end is not None
        if any(
            sequence < declaration.sequence_start or sequence > declaration.sequence_end
            for sequence in sequences
        ):
            raise ValueError("event sequence lies outside the declared coverage range")
        expected = list(range(declaration.sequence_start, declaration.sequence_end + 1))
        missing = sorted(set(expected) - set(sequences))
        if declaration.status == "complete" and missing:
            raise ValueError("complete coverage declaration contains sequence gaps")
        if declaration.status == "partial" and not missing:
            raise ValueError("partial coverage declaration has no missing sequences")
        return self

    @property
    def missing_sequences(self) -> list[int] | None:
        """Return missing sequence numbers; ``None`` when coverage is unknown."""

        declaration = self.coverage
        if declaration.status == "unknown":
            return None
        if declaration.expected_event_count == 0:
            return []
        assert declaration.sequence_start is not None and declaration.sequence_end is not None
        observed = {event.sequence for event in self.events}
        return [
            sequence
            for sequence in range(declaration.sequence_start, declaration.sequence_end + 1)
            if sequence not in observed
        ]

    @property
    def eligible_for_harness_window(self) -> bool:
        """Only a source-declared complete window may feed a future baseline."""

        return self.coverage.status == "complete" and self.missing_sequences == []


def _validate_timestamp(value: str) -> str:
    parse_timestamp(value)
    return value




def load_harness_event_window(path: str | Path) -> HarnessEventWindow:
    """Load a UTF-8 JSON event window and refuse invalid or non-minimized input."""

    resolved = Path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load event window {resolved}: {exc}") from exc
    try:
        return HarnessEventWindow.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"event window {resolved} is invalid: {exc}") from exc


__all__ = [
    "EVENT_WINDOW_SCHEMA_VERSION",
    "EventCoverage",
    "HarnessEvent",
    "HarnessEventWindow",
    "HarnessObservation",
    "load_harness_event_window",
]
