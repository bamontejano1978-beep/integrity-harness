"""One-shot schedulable cycle for longitudinal AI Act readiness monitoring."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

from autopilot.ai_act_monitor import (
    LongitudinalReport,
    ObligationMap,
    SystemProfile,
    build_longitudinal_report,
    load_evaluations,
    write_report_bundle,
)
from autopilot.ai_act_store import (
    AIActLongitudinalStore,
    IngestionSummary,
    MonitorState,
)
from autopilot.contracts.models import AutonomyLevel
from autopilot.trajectory import parse_timestamp

CYCLE_SCHEMA_VERSION = "0.1"

_AUTONOMY_ORDER = {
    AutonomyLevel.COACH.value: 0,
    AutonomyLevel.SUPERVISE.value: 1,
    AutonomyLevel.RESTRICT.value: 2,
    AutonomyLevel.QUARANTINE.value: 3,
}


class CycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangeKind(str, Enum):
    baseline = "baseline"
    unchanged = "unchanged"
    risk_increased = "risk_increased"
    risk_decreased = "risk_decreased"
    state_changed = "state_changed"


class CycleAlert(CycleModel):
    alert_id: str
    severity: Literal["medium", "high", "critical"]
    kind: str
    message: str


class CycleState(CycleModel):
    fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    current_version: str
    autonomy: AutonomyLevel
    drift_index: int = Field(ge=0, le=100)
    coverage_percent: int = Field(ge=0, le=100)
    alert_fingerprints: list[str]


class MonitorCyclePayload(CycleModel):
    schema_version: Literal["0.1"] = CYCLE_SCHEMA_VERSION
    cycle_id: str
    system_id: str
    ran_at: str
    ingestion: IngestionSummary
    report_id: str
    report_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    previous_state: CycleState | None
    current_state: CycleState
    state_changed: bool
    change_kind: ChangeKind
    emitted_alerts: list[CycleAlert]
    report_written: bool
    output_paths: dict[str, str]


class CycleSignature(CycleModel):
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    signed_payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    value_base64: str
    public_key_pem: str


class SignedMonitorCycleReceipt(CycleModel):
    payload: MonitorCyclePayload
    signature: CycleSignature


class CycleVerification(CycleModel):
    valid: bool
    key_matches: bool
    digest_matches: bool
    signature_valid: bool
    problems: list[str]


class MonitorCycleResult(CycleModel):
    receipt: SignedMonitorCycleReceipt
    audit_inserted: bool
    cycle_registered: bool


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_cycle_keypair(
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> str:
    private_path = Path(private_key_path)
    public_path = Path(public_key_path)
    if private_path.exists() or public_path.exists():
        raise FileExistsError("cycle key generation never overwrites an existing key")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return _public_key_id(public_key)


def _load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("cycle private key must be Ed25519")
    return key


def _load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("cycle public key must be Ed25519")
    return key


def _state_from_report(report: LongitudinalReport) -> CycleState:
    alert_fingerprints = sorted(alert.alert_id for alert in report.current_alerts)
    state_payload = {
        "system_id": report.system_id,
        "current_version": report.current_version,
        "autonomy": report.current_autonomy.value,
        "drift_index": report.current_drift_index,
        "coverage_percent": report.current_coverage_percent,
        "alert_fingerprints": alert_fingerprints,
    }
    fingerprint = hashlib.sha256(_canonical_bytes(state_payload)).hexdigest()
    return CycleState(
        fingerprint=fingerprint,
        current_version=report.current_version,
        autonomy=report.current_autonomy,
        drift_index=report.current_drift_index,
        coverage_percent=report.current_coverage_percent,
        alert_fingerprints=alert_fingerprints,
    )


def _cycle_state_from_stored(state: MonitorState | None) -> CycleState | None:
    if state is None:
        return None
    return CycleState(
        fingerprint=state.state_fingerprint,
        current_version=state.current_version,
        autonomy=AutonomyLevel(state.autonomy),
        drift_index=state.drift_index,
        coverage_percent=state.coverage_percent,
        alert_fingerprints=state.alert_fingerprints,
    )


def _classify_change(previous: CycleState | None, current: CycleState) -> ChangeKind:
    if previous is None:
        return ChangeKind.baseline
    if previous.fingerprint == current.fingerprint:
        return ChangeKind.unchanged
    worsened = (
        _AUTONOMY_ORDER[current.autonomy.value] > _AUTONOMY_ORDER[previous.autonomy.value]
        or current.drift_index > previous.drift_index
        or current.coverage_percent < previous.coverage_percent
    )
    improved = (
        _AUTONOMY_ORDER[current.autonomy.value] < _AUTONOMY_ORDER[previous.autonomy.value]
        or current.drift_index < previous.drift_index
        or current.coverage_percent > previous.coverage_percent
    )
    if worsened and not improved:
        return ChangeKind.risk_increased
    if improved and not worsened:
        return ChangeKind.risk_decreased
    return ChangeKind.state_changed


def _emitted_alerts(
    previous: CycleState | None,
    current: CycleState,
    report: LongitudinalReport,
    change_kind: ChangeKind,
) -> list[CycleAlert]:
    if previous is None or change_kind is ChangeKind.unchanged:
        return []
    severity: Literal["medium", "high", "critical"]
    if change_kind is ChangeKind.risk_increased:
        severity = (
            "critical"
            if current.autonomy is AutonomyLevel.QUARANTINE
            else "high"
        )
    else:
        severity = "medium"
    change_message = (
        f"Readiness state changed from {previous.autonomy.value}/"
        f"{previous.drift_index} to {current.autonomy.value}/{current.drift_index}."
    )
    change_id = hashlib.sha256(
        f"{report.system_id}|{previous.fingerprint}|{current.fingerprint}".encode()
    ).hexdigest()[:20]
    emitted = [
        CycleAlert(
            alert_id=f"CHG-{change_id.upper()}",
            severity=severity,
            kind=change_kind.value,
            message=change_message,
        )
    ]
    previous_alerts = set(previous.alert_fingerprints)
    emitted.extend(
        CycleAlert(
            alert_id=alert.alert_id,
            severity=alert.severity,
            kind=alert.kind,
            message=alert.message,
        )
        for alert in report.current_alerts
        if alert.alert_id not in previous_alerts
    )
    return emitted


def sign_cycle_payload(
    payload: MonitorCyclePayload,
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> SignedMonitorCycleReceipt:
    private_key = _load_private_key(private_key_path)
    public_key = _load_public_key(public_key_path)
    if private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) != public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ):
        raise ValueError("cycle private and public keys do not match")
    encoded = _canonical_bytes(payload.model_dump(mode="json"))
    signature = private_key.sign(encoded)
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return SignedMonitorCycleReceipt(
        payload=payload,
        signature=CycleSignature(
            key_id=_public_key_id(public_key),
            signed_payload_sha256=hashlib.sha256(encoded).hexdigest(),
            value_base64=base64.b64encode(signature).decode("ascii"),
            public_key_pem=public_pem,
        ),
    )


def verify_cycle_receipt(receipt: SignedMonitorCycleReceipt) -> CycleVerification:
    problems: list[str] = []
    encoded = _canonical_bytes(receipt.payload.model_dump(mode="json"))
    digest = hashlib.sha256(encoded).hexdigest()
    digest_matches = digest == receipt.signature.signed_payload_sha256
    if not digest_matches:
        problems.append("signed payload digest does not match")
    try:
        key = serialization.load_pem_public_key(receipt.signature.public_key_pem.encode())
    except (ValueError, TypeError):
        key = None
    key_matches = isinstance(key, Ed25519PublicKey) and (
        receipt.signature.key_id == _public_key_id(key)
    )
    if not key_matches:
        problems.append("embedded public key does not match key_id")
    signature_valid = False
    if isinstance(key, Ed25519PublicKey):
        try:
            signature = base64.b64decode(receipt.signature.value_base64, validate=True)
            key.verify(signature, encoded)
            signature_valid = True
        except (ValueError, InvalidSignature):
            signature_valid = False
    if not signature_valid:
        problems.append("Ed25519 cycle signature is invalid")
    return CycleVerification(
        valid=digest_matches and key_matches and signature_valid,
        key_matches=key_matches,
        digest_matches=digest_matches,
        signature_valid=signature_valid,
        problems=problems,
    )


def run_monitor_cycle(
    *,
    store: AIActLongitudinalStore,
    profile: SystemProfile,
    obligation_map: ObligationMap,
    evaluation_paths: list[str | Path],
    output_dir: str | Path,
    audit_ledger_path: str | Path,
    private_key_path: str | Path,
    public_key_path: str | Path,
    ran_at: str | None = None,
) -> MonitorCycleResult:
    timestamp = ran_at or datetime.now(timezone.utc).isoformat()
    parse_timestamp(timestamp)
    evaluations = load_evaluations(evaluation_paths)
    ingestion = store.ingest(
        profile,
        obligation_map,
        evaluations,
        ingested_at=timestamp,
    )
    snapshot = store.load_snapshot(profile.system_id)
    report = build_longitudinal_report(
        snapshot.profile,
        snapshot.obligation_map,
        snapshot.evaluations,
        generated_at=timestamp,
    )
    current = _state_from_report(report)
    cycle_seed = _canonical_bytes(
        {
            "system_id": profile.system_id,
            "ran_at": timestamp,
            "ingestion_id": ingestion.ingestion_id,
            "state_fingerprint": current.fingerprint,
        }
    )
    cycle_id = f"CYCLE-{hashlib.sha256(cycle_seed).hexdigest()[:24].upper()}"
    output_path = Path(output_dir)
    immutable_receipt_path = output_path / "cycles" / f"{cycle_id}.json"
    latest_receipt_path = output_path / "cycle_receipt.json"

    from autopilot.audit import AuditLedger, record_ai_act_monitor_cycle

    existing_receipt_json = store.load_monitor_cycle_receipt(cycle_id)
    if existing_receipt_json is not None:
        existing_receipt = SignedMonitorCycleReceipt.model_validate_json(
            existing_receipt_json
        )
        if not verify_cycle_receipt(existing_receipt).valid:
            raise ValueError("stored cycle receipt failed signature verification")
        audit_result = record_ai_act_monitor_cycle(
            AuditLedger(audit_ledger_path), existing_receipt
        )
        output_path.mkdir(parents=True, exist_ok=True)
        immutable_receipt_path.parent.mkdir(parents=True, exist_ok=True)
        rendered_existing = existing_receipt.model_dump_json(indent=2) + "\n"
        if immutable_receipt_path.exists():
            if immutable_receipt_path.read_text(encoding="utf-8") != rendered_existing:
                raise ValueError("immutable cycle receipt path contains different content")
        else:
            immutable_receipt_path.write_text(rendered_existing, encoding="utf-8")
        latest_receipt_path.write_text(rendered_existing, encoding="utf-8")
        return MonitorCycleResult(
            receipt=existing_receipt,
            audit_inserted=audit_result.inserted,
            cycle_registered=False,
        )

    previous = _cycle_state_from_stored(store.get_monitor_state(profile.system_id))
    change_kind = _classify_change(previous, current)
    state_changed = change_kind is not ChangeKind.unchanged
    report_written = (
        state_changed
        or ingestion.evaluations_inserted > 0
        or ingestion.profile_changed
        or ingestion.obligation_map_changed
        or not (output_path / "index.html").is_file()
    )
    outputs = write_report_bundle(report, output_path) if report_written else {}
    emitted_alerts = _emitted_alerts(previous, current, report, change_kind)
    immutable_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    outputs["cycle_receipt"] = str(immutable_receipt_path)
    if emitted_alerts:
        outputs["alerts"] = str(output_path / "alerts" / f"{cycle_id}.json")
    report_encoded = _canonical_bytes(report.model_dump(mode="json"))
    payload = MonitorCyclePayload(
        cycle_id=cycle_id,
        system_id=profile.system_id,
        ran_at=timestamp,
        ingestion=ingestion,
        report_id=report.report_id,
        report_sha256=hashlib.sha256(report_encoded).hexdigest(),
        previous_state=previous,
        current_state=current,
        state_changed=state_changed,
        change_kind=change_kind,
        emitted_alerts=emitted_alerts,
        report_written=report_written,
        output_paths=outputs,
    )
    receipt = sign_cycle_payload(payload, private_key_path, public_key_path)
    verification = verify_cycle_receipt(receipt)
    if not verification.valid:
        raise ValueError("generated cycle receipt failed signature verification")
    output_path.mkdir(parents=True, exist_ok=True)
    rendered_receipt = receipt.model_dump_json(indent=2) + "\n"
    immutable_receipt_path.write_text(rendered_receipt, encoding="utf-8")
    latest_receipt_path.write_text(rendered_receipt, encoding="utf-8")
    if emitted_alerts:
        alert_path = Path(outputs["alerts"])
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        alert_path.write_text(
            json.dumps(
                [alert.model_dump(mode="json") for alert in emitted_alerts],
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    audit_result = record_ai_act_monitor_cycle(AuditLedger(audit_ledger_path), receipt)
    registration = store.register_monitor_cycle(
        cycle_id=cycle_id,
        system_id=profile.system_id,
        ingestion_id=ingestion.ingestion_id,
        ran_at=timestamp,
        state_changed=state_changed,
        change_kind=change_kind.value,
        receipt_json=receipt.model_dump_json(),
    )
    store.save_monitor_state(
        MonitorState(
            system_id=profile.system_id,
            state_fingerprint=current.fingerprint,
            current_version=current.current_version,
            autonomy=current.autonomy.value,
            drift_index=current.drift_index,
            coverage_percent=current.coverage_percent,
            alert_fingerprints=current.alert_fingerprints,
            updated_at=timestamp,
        )
    )
    return MonitorCycleResult(
        receipt=receipt,
        audit_inserted=audit_result.inserted,
        cycle_registered=registration.inserted,
    )
