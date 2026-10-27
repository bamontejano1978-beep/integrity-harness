"""System 1 A2: signed trajectory producer for complete harness event windows.

This producer consumes only the declared local harness schema. It is not a
runtime telemetry collector. The signed trajectory authenticates the
configured producer key and the exact minimized projection; it cannot prove
that the harness observed every external runtime event.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from autopilot.contracts import DSLValidationError, load_contract
from autopilot.enforcement import SignedTrajectory, sign_trajectory_payload, verify_trajectory_envelope
from autopilot.system1_events import HarnessEventWindow
from autopilot.trajectory import ObservationKind, Trajectory, TrajectoryObservation, parse_timestamp

if TYPE_CHECKING:
    from autopilot.audit import AuditLedger


class System1SensorError(ValueError):
    """Declared A2 refusal with a typed reason for the CLI ledger boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class System1TrajectoryEmission(BaseModel):
    """Append-only evidence binding a signed trajectory to its complete source window."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    kind: Literal["system1_trajectory_emitted"] = "system1_trajectory_emitted"
    emission_id: str
    emitted_at: str
    source_id: str
    window_id: str
    window_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_count: int = Field(ge=0, strict=True)
    coverage_status: Literal["complete"]
    trajectory_id: str
    producer: str
    producer_key_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    signed_trajectory: dict[str, Any]

    @field_validator("emitted_at")
    @classmethod
    def validate_emitted_at(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @property
    def verification_valid(self) -> bool:
        """Re-validate canonical trajectory and embedded Ed25519 signature."""

        try:
            envelope = SignedTrajectory.model_validate(self.signed_trajectory)
            verification = verify_trajectory_envelope(envelope)
            trajectory = Trajectory.model_validate(envelope.payload)
        except (ValidationError, ValueError):
            return False
        source = envelope.payload.get("x_system1_source")
        if not isinstance(source, dict):
            return False
        return (
            verification.valid
            and envelope.signature.key_id == self.producer_key_id
            and envelope.producer == self.producer
            and envelope.trajectory_id == self.trajectory_id
            and trajectory.trajectory_id == self.trajectory_id
            and source.get("source_kind") == "harness"
            and source.get("source_id") == self.source_id
            and source.get("window_id") == self.window_id
            and source.get("window_sha256") == self.window_sha256
            and source.get("coverage_status") == self.coverage_status
            and source.get("heartbeat_at") is not None
            and source.get("event_count") == self.event_count
        )


class System1SensorRefusal(BaseModel):
    """Observation-only record of an A2 window or signing refusal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    kind: Literal["system1_sensor_refusal"] = "system1_sensor_refusal"
    refusal_id: str
    refused_at: str
    produced_by: str
    reason_code: Literal[
        "coverage_incomplete",
        "contract_not_accepted",
        "observation_not_in_contract",
        "signing_key_invalid",
        "output_already_exists",
        "emission_not_accepted",
        "invalid_event_window",
    ]
    reason: str = Field(min_length=1, max_length=4000)
    source_id: str | None = None
    window_id: str | None = None
    window_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("refused_at")
    @classmethod
    def validate_refused_at(cls, value: str) -> str:
        parse_timestamp(value)
        return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _window_digest(window: HarnessEventWindow) -> str:
    return hashlib.sha256(_canonical_bytes(window.model_dump(mode="json"))).hexdigest()


def _write_new_atomically(path: Path, document: str) -> None:
    """Create ``path`` without clobbering existing evidence, using a temp file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise System1SensorError("output_already_exists", f"output already exists: {path}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_harness_trajectory(
    window: HarnessEventWindow,
    *,
    contract_path: str | Path,
    candidate_version: str,
) -> Trajectory:
    """Map declared harness observations to a canonical trajectory, without guessing semantics."""

    if not window.eligible_for_harness_window:
        raise System1SensorError(
            "coverage_incomplete",
            "only a complete, gap-free harness event window can produce a trajectory",
        )
    try:
        contract = load_contract(contract_path)
    except (DSLValidationError, OSError, ValueError) as exc:
        raise System1SensorError("contract_not_accepted", f"contract is not valid: {exc}") from exc

    observations: list[TrajectoryObservation] = []
    for event in window.events:
        projection = event.observation
        vocabulary = (
            contract.vocabulary.events
            if projection.kind is ObservationKind.event
            else contract.vocabulary.actions
        )
        if projection.name not in vocabulary:
            raise System1SensorError(
                "observation_not_in_contract",
                f"{projection.kind.value} {projection.name!r} is absent from contract vocabulary",
            )
        attributes = dict(projection.attributes)
        attributes.update(
            {
                "harness_event_sequence": event.sequence,
                "harness_event_id": event.event_id,
                "harness_projection_sha256": event.payload_sha256,
                "harness_data_labels": ",".join(event.data_labels),
            }
        )
        observations.append(
            TrajectoryObservation(
                ref=f"{window.window_id}/{event.event_id}",
                kind=projection.kind,
                name=projection.name,
                observed_at=event.observed_at,
                attributes=attributes,
            )
        )

    try:
        return Trajectory(
            trajectory_id=f"system1-{window.window_id}",
            contract_id=contract.contract.id,
            contract_version=contract.contract.version,
            candidate_version=candidate_version,
            complete=True,
            evaluated_at=window.produced_at,
            observations=observations,
            recovery={},
            x_system1_source={
                "source_kind": window.source_kind,
                "source_id": window.source_id,
                "window_id": window.window_id,
                "window_sha256": _window_digest(window),
                "coverage_status": window.coverage.status,
                "heartbeat_at": window.coverage.heartbeat_at,
                "event_count": len(window.events),
            },
        )
    except (ValidationError, ValueError) as exc:
        raise System1SensorError("emission_not_accepted", f"projected trajectory is invalid: {exc}") from exc


def emit_harness_trajectory(
    window: HarnessEventWindow,
    *,
    contract_path: str | Path,
    candidate_version: str,
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    trust_public_key: str | Path,
    output_path: str | Path,
    ledger: AuditLedger,
    produced_by: str = "integrity-system1-harness-producer",
    emitted_at: str | None = None,
) -> System1TrajectoryEmission:
    """Sign, verify, persist, and ledger-record one complete harness-derived trajectory."""

    trajectory = build_harness_trajectory(
        window,
        contract_path=contract_path,
        candidate_version=candidate_version,
    )
    try:
        envelope = sign_trajectory_payload(
            trajectory.model_dump(mode="json", by_alias=True, exclude_none=True),
            producer_private_key,
            producer_public_key,
            producer=produced_by,
        )
        verification = verify_trajectory_envelope(envelope, trust_public_key)
    except (OSError, ValueError) as exc:
        raise System1SensorError("signing_key_invalid", f"producer signing failed: {exc}") from exc
    if not verification.valid:
        raise System1SensorError(
            "signing_key_invalid",
            "signed trajectory did not verify against configured producer trust key: "
            + "; ".join(verification.problems),
        )

    moment = emitted_at or datetime.now(timezone.utc).isoformat()
    try:
        parse_timestamp(moment)
    except ValueError as exc:
        raise System1SensorError("emission_not_accepted", f"emitted_at is invalid: {exc}") from exc
    digest = _window_digest(window)
    emission = System1TrajectoryEmission(
        emission_id="S1-EMIT-" + hashlib.sha256(
            _canonical_bytes(
                {
                    "window_sha256": digest,
                    "trajectory_id": trajectory.trajectory_id,
                    "producer_key_id": envelope.signature.key_id,
                    "emitted_at": moment,
                }
            )
        ).hexdigest()[:24].upper(),
        emitted_at=moment,
        source_id=window.source_id,
        window_id=window.window_id,
        window_sha256=digest,
        event_count=len(window.events),
        coverage_status="complete",
        trajectory_id=trajectory.trajectory_id,
        producer=produced_by,
        producer_key_id=envelope.signature.key_id,
        signed_trajectory=envelope.model_dump(mode="json"),
    )
    output = Path(output_path)
    if output.exists():
        raise System1SensorError("output_already_exists", f"output already exists: {output}")
    artifact_created = False
    try:
        _write_new_atomically(output, json.dumps(envelope.model_dump(mode="json"), indent=2) + "\n")
        artifact_created = True
        from autopilot.audit import record_system1_trajectory_emission

        record_system1_trajectory_emission(ledger, emission)
    except System1SensorError:
        if artifact_created:
            output.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        if artifact_created:
            output.unlink(missing_ok=True)
        raise System1SensorError("emission_not_accepted", f"could not persist A2 evidence: {exc}") from exc
    return emission


def record_system1_sensor_refusal(
    ledger: AuditLedger,
    *,
    reason_code: Literal[
        "coverage_incomplete",
        "contract_not_accepted",
        "observation_not_in_contract",
        "signing_key_invalid",
        "output_already_exists",
        "emission_not_accepted",
        "invalid_event_window",
    ],
    reason: str,
    produced_by: str = "integrity-system1-harness-producer",
    source_id: str | None = None,
    window_id: str | None = None,
    window_sha256: str | None = None,
    refused_at: str | None = None,
) -> System1SensorRefusal:
    """Append a refusal record before the CLI returns a fail-closed status."""

    moment = refused_at or datetime.now(timezone.utc).isoformat()
    refusal = System1SensorRefusal(
        refusal_id="S1-REFUSED-" + hashlib.sha256(
            _canonical_bytes(
                {
                    "reason_code": reason_code,
                    "reason": reason,
                    "produced_by": produced_by,
                    "refused_at": moment,
                    "source_id": source_id,
                    "window_id": window_id,
                    "window_sha256": window_sha256,
                }
            )
        ).hexdigest()[:24].upper(),
        refused_at=moment,
        produced_by=produced_by,
        reason_code=reason_code,
        reason=reason,
        source_id=source_id,
        window_id=window_id,
        window_sha256=window_sha256,
    )
    from autopilot.audit import AuditEventType

    ledger.append(
        AuditEventType.system1_sensor_refusal,
        refusal.refusal_id,
        moment,
        refusal.model_dump(mode="json"),
    )
    return refusal

