"""Append-only, hash-chained audit ledger for the remediation workflow."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from autopilot.ai_act_cycle import SignedMonitorCycleReceipt, verify_cycle_receipt
from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.enforcement import (
    SignedApprovalReceipt,
    SignedApprovalRevocationReceipt,
    SignedGuardDecisionReceipt,
    verify_guard_decision,
    verify_tool_approval,
    verify_tool_approval_revocation,
)
from autopilot.contracts.loader import pydantic_issues
from autopilot.monitoring_loop import MonitoringCycle, MonitorRefusal
from autopilot.system1_sensor import System1SensorRefusal, System1TrajectoryEmission
from autopilot.drift import DriftRefusal, DriftSignal
from autopilot.drift_budget import DriftBudgetRefusal, DriftCoalescence
from autopilot.drift_response import DriftResponse
from autopilot.contracts.models import SNAKE_NAME_PATTERN
from autopilot.diagnostics import ChangeCard
from autopilot.incidents import Incident
from autopilot.release_review import (
    ReleaseReview,
    ReviewVerification,
    verify_release_review_pem,
)
from autopilot.contracts.models import IDENTIFIER_PATTERN, SEMVER_PATTERN
from autopilot.sandbox import SandboxReport
from autopilot.trajectory import parse_timestamp

AUDIT_SCHEMA_VERSION = "1"
GENESIS_HASH = "0" * 64
LEDGER_BUNDLE_KIND = "audit_ledger_bundle"
LEDGER_BUNDLE_VERSION = "0.1"

# Portable bundle emitted by `audit-export`. The verifier that reads it lives in
# `web/assets/js/ledger-verifier.js` (browser and Node): it only recomputes
# SHA-256 over bytes, so the bundle keeps every stored value verbatim instead of
# reformatting it. See docs/ledger_bundle_v0.1.md for the exact contract.


class AuditEventType(str, Enum):
    incident_detected = "incident_detected"
    change_card_generated = "change_card_generated"
    sandbox_evaluated = "sandbox_evaluated"
    release_review_signed = "release_review_signed"
    signing_key_revoked = "signing_key_revoked"
    release_review_revoked = "release_review_revoked"
    release_denied = "release_denied"
    # B2 carry-over (D) — mirrors release_denied for chain-superseded approvals.
    # Idempotent via UNIQUE(event_type, subject_id) at the (release_chain_override,
    # review_id) pair so retries cannot double-append.
    release_chain_override = "release_chain_override"
    ai_act_monitor_cycle = "ai_act_monitor_cycle"
    guard_decision_recorded = "guard_decision_recorded"
    tool_approval_granted = "tool_approval_granted"
    tool_approval_revoked = "tool_approval_revoked"
    # Continuous monitoring loop (System 1 v0.1): one unsigned observation
    # record per scheduled emission. It carries no authority - autonomy
    # changes only through signed evaluation snapshots - so the record is
    # validated for structure, not for a signature.
    monitoring_loop_cycle = "monitoring_loop_cycle"
    # Behavioural drift signal (System 1 OP-3): an observation record with no
    # authority. Like the loop's cycle, it is validated for structure, not for
    # a signature; autonomy never changes through it (invariant I-1).
    drift_signal = "drift_signal"
    # Drift-triggered deep evaluation (System 1 OP-3 → OP-5): ties a recorded
    # drift signal to the deterministic run it fired. Observation record: the
    # authority remains the signed snapshot the run emitted (invariant I-1).
    drift_response = "drift_response"
    # Cost-control declarations (System 1 P-07, threat T-09): a signal folded
    # into an already recorded run, or refused because the declared budget
    # window is spent. Observation records, no authority: I-1 untouched and
    # the refusal is fail-closed until a fresh signed snapshot exists.
    drift_coalescence = "drift_coalescence"
    drift_budget_refusal = "drift_budget_refusal"
    # Fail-closed refusals of the drift path (System 1): the check or the
    # response declined to act on a verified baseline or a recorded signal —
    # replay, wrong status, mismatch, continuity break, unreadable cost-control
    # state, refused run. Observation records, no authority: the ledger carries
    # the decisions that produced no run, not only the ones that did.
    drift_refusal = "drift_refusal"
    # Fail-closed refusals of the monitoring loop (System 1): a cycle the sensor
    # declined to emit — a trajectory it cannot sign, an envelope it cannot
    # verify, a contract it cannot evaluate, a snapshot it cannot hand the
    # guard. Observation records, no authority: the ledger carries the cycles
    # that emitted no snapshot, not only the ones that did.
    monitoring_refusal = "monitoring_refusal"
    # System 1 A2: signed trajectory emitted from a complete harness window.
    # Receipt is cryptographically verified before append; it has no autonomy authority.
    system1_trajectory_emitted = "system1_trajectory_emitted"
    # System 1 A2: declared producer refusal when a harness window cannot be trusted.
    system1_sensor_refusal = "system1_sensor_refusal"


class KeyRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    reason: Annotated[str, Field(min_length=20, max_length=2000)]
    reference: Annotated[str, Field(min_length=3, max_length=255)]
    revoked_at: str

    @field_validator("revoked_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class ReviewRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    reason: Annotated[str, Field(min_length=20, max_length=2000)]
    reference: Annotated[str, Field(min_length=3, max_length=255)]
    revoked_at: str

    @field_validator("revoked_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class ReleaseDenial(BaseModel):
    """Audit-logged record of an envelope auto-veto against one review_id.

    ``subject_id`` for the underlying ``release_denied`` event is the
    review_id, so repeated denies against the same signed payload are
    idempotent (the existing ``UNIQUE(event_type, subject_id)`` constraint
    on ``audit_events`` returns ``inserted=False`` for the second insert).
    The carrier actor is ``"system_autopilot"`` so a human can never
    clash with the autonomous envelope-checker handle.
    """

    model_config = ConfigDict(extra="forbid")

    review_id: str
    contract_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    contract_version: Annotated[str, Field(pattern=SEMVER_PATTERN)]
    scope_clause_ids: list[
        Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    ] = Field(min_length=1)
    target_clause_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    current_usage: Annotated[int, Field(ge=0)]
    risk_budget: Annotated[int, Field(ge=1, le=10_000)]
    expiry: str
    denied_at: str
    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)] = "system_autopilot"
    reason: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    reference: Annotated[str, Field(min_length=3, max_length=255)]

    @field_validator("expiry")
    @classmethod
    def validate_expiry(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @field_validator("denied_at")
    @classmethod
    def validate_denied_at(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class ReleaseChainOverride(BaseModel):
    """Audit-logged record of a chain-signed release that bypassed an
    envelope auto-veto thanks to a cryptographically verified B2
    chain signature.

    Symmetrical to :class:`ReleaseDenial`: the envelope snapshot fields
    are identical so downstream verifiers can correlate the bypass with
    the contract / clause / budget it overrode. ``subject_id`` for the
    underlying ``release_chain_override`` event is also the ``review_id``,
    making ``UNIQUE(event_type, subject_id)`` on ``audit_events`` the
    idempotency lever; retries return ``inserted=False`` for the second
    insert.

    The carrier actor is ``"system_orchestrator"`` — deliberately distinct
    from :class:`ReleaseDenial`'s ``"system_autopilot"`` — to mark this
    event as the orchestrator's deliberate bypass decision rather than
    an autonomous auto-veto. ``override_reason`` carries the same chain-
    override-prefixed invariants that ``EnvelopeEnforcementResult.problems``
    exposes (``"chain_override: "`` prefix), so the trail can answer
    "why was this release approved without ``release_denied``".

    ``chain_actor_count`` records the size of the delegation chain that
    authorised the override (root → leaf, ≥2 by the B2 contract).
    """

    model_config = ConfigDict(extra="forbid")

    review_id: str
    contract_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    contract_version: Annotated[str, Field(pattern=SEMVER_PATTERN)]
    scope_clause_ids: list[
        Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    ] = Field(min_length=1)
    target_clause_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    current_usage: Annotated[int, Field(ge=0)]
    risk_budget: Annotated[int, Field(ge=1, le=10_000)]
    expiry: str
    override_at: str
    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)] = "system_orchestrator"
    override_reason: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    reference: Annotated[str, Field(min_length=3, max_length=255)]
    chain_actor_count: Annotated[int, Field(ge=2)] | None = None

    @field_validator("expiry")
    @classmethod
    def validate_expiry(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @field_validator("override_at")
    @classmethod
    def validate_override_at(cls, value: str) -> str:
        parse_timestamp(value)
        return value


class RecordedReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: ReleaseReview
    public_key_pem: str
    verification_at_recording: ReviewVerification


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    event_id: str
    event_type: AuditEventType
    subject_id: str
    occurred_at: str
    recorded_at: str
    payload: dict
    payload_sha256: str
    previous_hash: str
    event_hash: str


class AuditAppendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inserted: bool
    event: AuditEvent


class AuditVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    event_count: int = Field(ge=0)
    head_hash: str
    chain_valid: bool
    payloads_valid: bool
    references_valid: bool
    signatures_valid: bool
    revoked_key_ids: list[str]
    revoked_review_ids: list[str]
    active_staged_review_ids: list[str]
    problems: list[str]


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event_hash(
    sequence: int,
    event_type: str,
    subject_id: str,
    occurred_at: str,
    recorded_at: str,
    payload_sha256: str,
    previous_hash: str,
) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "sequence": sequence,
                "event_type": event_type,
                "subject_id": subject_id,
                "occurred_at": occurred_at,
                "recorded_at": recorded_at,
                "payload_sha256": payload_sha256,
                "previous_hash": previous_hash,
            }
        )
    )


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        sequence=row["sequence"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        subject_id=row["subject_id"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        payload=json.loads(row["payload_json"]),
        payload_sha256=row["payload_sha256"],
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
    )


class AuditLedger:
    """SQLite ledger whose event rows cannot be updated or deleted."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events(
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    UNIQUE(event_type, subject_id)
                );
                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_metadata_no_update
                BEFORE UPDATE ON audit_metadata BEGIN
                    SELECT RAISE(ABORT, 'audit metadata is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_metadata_no_delete
                BEFORE DELETE ON audit_metadata BEGIN
                    SELECT RAISE(ABORT, 'audit metadata is immutable');
                END;
                """
            )
            row = connection.execute(
                "SELECT value FROM audit_metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO audit_metadata(key,value) VALUES('schema_version',?)",
                    (AUDIT_SCHEMA_VERSION,),
                )
            elif row["value"] != AUDIT_SCHEMA_VERSION:
                raise RuntimeError("unsupported audit schema version: " + row["value"])

    def append(
        self,
        event_type: AuditEventType,
        subject_id: str,
        occurred_at: str,
        payload: dict,
    ) -> AuditAppendResult:
        parse_timestamp(occurred_at)
        payload_json = _canonical_json(payload)
        payload_sha256 = _sha256_text(payload_json)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM audit_events WHERE event_type=? AND subject_id=?",
                (event_type.value, subject_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise DSLValidationError(
                        [
                            ValidationIssue(
                                "audit_event",
                                "subject already exists with a different payload",
                            )
                        ]
                    )
                connection.commit()
                return AuditAppendResult(inserted=False, event=_row_to_event(existing))

            last = connection.execute(
                "SELECT sequence,event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = 1 if last is None else int(last["sequence"]) + 1
            previous_hash = GENESIS_HASH if last is None else last["event_hash"]
            recorded_at = datetime.now(timezone.utc).isoformat()
            digest = _event_hash(
                sequence,
                event_type.value,
                subject_id,
                occurred_at,
                recorded_at,
                payload_sha256,
                previous_hash,
            )
            event_id = f"AUD-{digest[:24].upper()}"
            connection.execute(
                """
                INSERT INTO audit_events(
                    sequence,event_id,event_type,subject_id,occurred_at,recorded_at,
                    payload_json,payload_sha256,previous_hash,event_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sequence,
                    event_id,
                    event_type.value,
                    subject_id,
                    occurred_at,
                    recorded_at,
                    payload_json,
                    payload_sha256,
                    previous_hash,
                    digest,
                ),
            )
            row = connection.execute(
                "SELECT * FROM audit_events WHERE sequence=?", (sequence,)
            ).fetchone()
            connection.commit()
            return AuditAppendResult(inserted=True, event=_row_to_event(row))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def events(self) -> list[AuditEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence"
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def raw_events(self) -> list[dict[str, object]]:
        """Return the stored rows verbatim, `payload_json` included and unparsed.

        The bundle exporter needs the exact bytes that were hashed at append
        time: re-serialising the parsed payload would make the export depend on
        the exporter's own JSON writer.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence"
            ).fetchall()
        return [dict(row) for row in rows]

    def find(self, event_type: AuditEventType, subject_id: str) -> AuditEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM audit_events WHERE event_type=? AND subject_id=?",
                (event_type.value, subject_id),
            ).fetchone()
        return _row_to_event(row) if row is not None else None


def export_ledger_bundle(
    ledger: AuditLedger,
    *,
    exported_at: str | None = None,
) -> dict:
    """Export a verified ledger as a self-contained, offline-verifiable bundle.

    Fail-closed by construction: a ledger that does not verify is not exported,
    because a bundle nobody can check is worse than no bundle. The bundle carries
    the stored values verbatim (``payload_json`` included) plus the operator's
    own CLI verification, so a third party can recompute every hash with a bare
    SHA-256 implementation and compare it with what the operator claimed.
    """
    verification = verify_audit_ledger(ledger)
    if not verification.valid:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "ledger",
                    "refusing to export an invalid ledger: "
                    + "; ".join(verification.problems[:3] or ["verification failed"]),
                )
            ]
        )
    rows = ledger.raw_events()
    if not rows:
        raise DSLValidationError(
            [ValidationIssue("ledger", "has no events to export")]
        )
    stamp = exported_at or datetime.now(timezone.utc).isoformat()
    parse_timestamp(stamp)
    try:
        ledger_sha256 = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    except OSError:  # pragma: no cover - the ledger exists, we just verified it
        ledger_sha256 = None
    return {
        "kind": LEDGER_BUNDLE_KIND,
        "bundle_version": LEDGER_BUNDLE_VERSION,
        "exported_at": stamp,
        "generator": "autopilot audit-export",
        "source": {
            "ledger_file": ledger.path.name,
            "ledger_sha256": ledger_sha256,
            "schema_version": AUDIT_SCHEMA_VERSION,
        },
        "ledger": {
            "hash_algorithm": "sha256",
            "event_hash_fields": [
                "sequence",
                "event_type",
                "subject_id",
                "occurred_at",
                "recorded_at",
                "payload_sha256",
                "previous_hash",
            ],
            "genesis_hash": GENESIS_HASH,
            "event_count": verification.event_count,
            "head_hash": verification.head_hash,
            "events": [
                {
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "subject_id": row["subject_id"],
                    "occurred_at": row["occurred_at"],
                    "recorded_at": row["recorded_at"],
                    "payload_json": row["payload_json"],
                    "payload_sha256": row["payload_sha256"],
                    "previous_hash": row["previous_hash"],
                    "event_hash": row["event_hash"],
                }
                for row in rows
            ],
        },
        "operator_verification": verification.model_dump(mode="json"),
    }


def record_change_card(ledger: AuditLedger, card: ChangeCard) -> list[AuditAppendResult]:
    incident_result = ledger.append(
        AuditEventType.incident_detected,
        card.incident.incident_id,
        card.incident.detected_at,
        card.incident.model_dump(mode="json"),
    )
    card_result = ledger.append(
        AuditEventType.change_card_generated,
        card.card_id,
        card.generated_at,
        card.model_dump(mode="json"),
    )
    return [incident_result, card_result]


def record_sandbox_report(
    ledger: AuditLedger,
    report: SandboxReport,
) -> AuditAppendResult:
    card_event = ledger.find(AuditEventType.change_card_generated, report.card_id)
    if card_event is None:
        raise DSLValidationError(
            [ValidationIssue("sandbox_report.card_id", "change card is not in the ledger")]
        )
    card = ChangeCard.model_validate(card_event.payload)
    candidate = next(
        (
            item
            for item in card.candidates
            if item.candidate_id == report.selected_candidate.candidate_id
        ),
        None,
    )
    if candidate is None or candidate != report.selected_candidate:
        raise DSLValidationError(
            [ValidationIssue("sandbox_report.selected_candidate", "does not match the change card")]
        )
    return ledger.append(
        AuditEventType.sandbox_evaluated,
        report.report_id,
        report.generated_at,
        report.model_dump(mode="json"),
    )


def record_release_denied(
    ledger: AuditLedger,
    denial: ReleaseDenial,
) -> AuditAppendResult:
    """Append a ``release_denied`` event to the audit ledger.

    Idempotency: a second call with the same ``denial.review_id`` and
    payload is recorded as ``inserted=False`` (existing in ledger) so a
    retry during a denial burst cannot double-append.
    """

    return ledger.append(
        AuditEventType.release_denied,
        denial.review_id,
        denial.denied_at,
        denial.model_dump(mode="json"),
    )


def record_release_chain_override(
    ledger: AuditLedger,
    override: ReleaseChainOverride,
) -> AuditAppendResult:
    """Append a ``release_chain_override`` event to the audit ledger.

    Symmetric to :func:`record_release_denied`. Idempotency: a second call
    with the same ``override.review_id`` and payload is recorded as
    ``inserted=False`` (existing in ledger) so a retry during an override
    burst cannot double-append. The distinct ``event_type`` guarantees the
    override event and any earlier ``release_denied`` event for the same
    review_id are not conflated.
    """

    return ledger.append(
        AuditEventType.release_chain_override,
        override.review_id,
        override.override_at,
        override.model_dump(mode="json"),
    )


def record_ai_act_monitor_cycle(
    ledger: AuditLedger,
    receipt: SignedMonitorCycleReceipt,
) -> AuditAppendResult:
    """Verify and append one signed longitudinal-monitor cycle receipt."""

    verification = verify_cycle_receipt(receipt)
    if not verification.valid:
        raise DSLValidationError(
            [ValidationIssue("ai_act_monitor_cycle.signature", "cycle signature is invalid")]
        )
    return ledger.append(
        AuditEventType.ai_act_monitor_cycle,
        receipt.payload.cycle_id,
        receipt.payload.ran_at,
        receipt.model_dump(mode="json"),
    )


def record_guard_decision(
    ledger: AuditLedger,
    receipt: SignedGuardDecisionReceipt,
) -> AuditAppendResult:
    """Verify and append one signed guard-decision receipt.

    Idempotency: ``subject_id`` is the receipt's ``decision_id`` (a digest of
    the screening inputs), so re-screening the same evaluation + policy at the
    same timestamp returns ``inserted=False`` instead of double-appending.
    A tampered signature is rejected before it can enter the ledger.
    """

    verification = verify_guard_decision(receipt)
    if not verification.valid:
        raise DSLValidationError(
            [ValidationIssue("guard_decision.signature", "guard signature is invalid")]
        )
    return ledger.append(
        AuditEventType.guard_decision_recorded,
        receipt.payload.decision_id,
        receipt.payload.recorded_at,
        receipt.model_dump(mode="json"),
    )


def record_tool_approval(
    ledger: AuditLedger,
    receipt: SignedApprovalReceipt,
) -> AuditAppendResult:
    """Verify and append one signed human-approval receipt.

    Idempotent by ``approval_id``; a tampered signature never enters the
    ledger. Ids are ``APPROVAL-<digest>`` and the append is attributable to
    ``payload.granted_by``.
    """

    verification = verify_tool_approval(receipt)
    if not verification.valid:
        raise DSLValidationError(
            [ValidationIssue("tool_approval.signature", "approval signature is invalid")]
        )
    return ledger.append(
        AuditEventType.tool_approval_granted,
        receipt.payload.approval_id,
        receipt.payload.granted_at,
        receipt.model_dump(mode="json"),
    )


def record_tool_approval_revocation(
    ledger: AuditLedger,
    receipt: SignedApprovalRevocationReceipt,
) -> AuditAppendResult:
    """Verify and append one signed approval-revocation receipt.

    Idempotent by ``revocation_id``; the referenced ``approval_id`` must
    already exist in the ledger (reference integrity is enforced here and
    re-checked by ``verify_audit_ledger``).
    """

    verification = verify_tool_approval_revocation(receipt)
    if not verification.valid:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "tool_approval_revocation.signature",
                    "approval revocation signature is invalid",
                )
            ]
        )
    known = {
        event.subject_id
        for event in ledger.events()
        if event.event_type is AuditEventType.tool_approval_granted
    }
    if receipt.payload.approval_id not in known:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "tool_approval_revocation.approval_id",
                    f"unknown approval_id: {receipt.payload.approval_id}",
                )
            ]
    )
    return ledger.append(
        AuditEventType.tool_approval_revoked,
        receipt.payload.revocation_id,
        receipt.payload.revoked_at,
        receipt.model_dump(mode="json"),
    )


def record_release_review(
    ledger: AuditLedger,
    review: ReleaseReview,
    public_key_path: str | Path,
) -> AuditAppendResult:
    try:
        public_key_pem = Path(public_key_path).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError) as exc:
        raise DSLValidationError(
            [ValidationIssue("public_key", "cannot read a valid UTF-8 public key")]
        ) from exc
    verification = verify_release_review_pem(review, public_key_pem)
    if not verification.valid:
        raise DSLValidationError(
            [ValidationIssue("release_review.signature", "review signature is invalid")]
        )
    report_id = review.sandbox_report.report_id
    report_event = ledger.find(AuditEventType.sandbox_evaluated, report_id)
    if report_event is None:
        raise DSLValidationError(
            [ValidationIssue("release_review.sandbox_report", "report is not in the ledger")]
        )
    if report_event.payload != review.sandbox_report.model_dump(mode="json"):
        raise DSLValidationError(
            [ValidationIssue("release_review.sandbox_report", "does not match ledger evidence")]
        )
    if ledger.find(AuditEventType.signing_key_revoked, review.signature.key_id):
        raise DSLValidationError(
            [ValidationIssue("release_review.signature.key_id", "signing key is revoked")]
        )
    recorded = RecordedReview(
        review=review,
        public_key_pem=public_key_pem,
        verification_at_recording=verification,
    )
    return ledger.append(
        AuditEventType.release_review_signed,
        review.review_id,
        review.decided_at,
        recorded.model_dump(mode="json"),
    )


def revoke_signing_key(ledger: AuditLedger, revocation: KeyRevocation) -> AuditAppendResult:
    known = any(
        event.event_type is AuditEventType.release_review_signed
        and RecordedReview.model_validate(event.payload).review.signature.key_id
        == revocation.key_id
        for event in ledger.events()
    )
    if not known:
        raise DSLValidationError(
            [ValidationIssue("key_id", "has no signed review in the ledger")]
        )
    return ledger.append(
        AuditEventType.signing_key_revoked,
        revocation.key_id,
        revocation.revoked_at,
        revocation.model_dump(mode="json"),
    )


def revoke_release_review(
    ledger: AuditLedger,
    revocation: ReviewRevocation,
) -> AuditAppendResult:
    if ledger.find(AuditEventType.release_review_signed, revocation.review_id) is None:
        raise DSLValidationError(
            [ValidationIssue("review_id", "is not in the ledger")]
        )
    return ledger.append(
        AuditEventType.release_review_revoked,
        revocation.review_id,
        revocation.revoked_at,
        revocation.model_dump(mode="json"),
    )


def record_system1_trajectory_emission(
    ledger: AuditLedger,
    emission: System1TrajectoryEmission,
) -> AuditAppendResult:
    """Verify the signed canonical trajectory before recording its emission."""

    if not emission.verification_valid:
        raise DSLValidationError(
            [ValidationIssue("system1_trajectory_emission.signature", "trajectory signature or binding is invalid")]
        )
    return ledger.append(
        AuditEventType.system1_trajectory_emitted,
        emission.emission_id,
        emission.emitted_at,
        emission.model_dump(mode="json"),
    )


def _validate_payload(event: AuditEvent) -> BaseModel:
    models = {
        AuditEventType.incident_detected: Incident,
        AuditEventType.change_card_generated: ChangeCard,
        AuditEventType.sandbox_evaluated: SandboxReport,
        AuditEventType.release_review_signed: RecordedReview,
        AuditEventType.signing_key_revoked: KeyRevocation,
        AuditEventType.release_review_revoked: ReviewRevocation,
        AuditEventType.release_denied: ReleaseDenial,
        AuditEventType.release_chain_override: ReleaseChainOverride,
        AuditEventType.ai_act_monitor_cycle: SignedMonitorCycleReceipt,
        AuditEventType.guard_decision_recorded: SignedGuardDecisionReceipt,
        AuditEventType.tool_approval_granted: SignedApprovalReceipt,
        AuditEventType.tool_approval_revoked: SignedApprovalRevocationReceipt,
        AuditEventType.monitoring_loop_cycle: MonitoringCycle,
        AuditEventType.drift_signal: DriftSignal,
        AuditEventType.drift_response: DriftResponse,
        AuditEventType.drift_coalescence: DriftCoalescence,
        AuditEventType.drift_budget_refusal: DriftBudgetRefusal,
        AuditEventType.drift_refusal: DriftRefusal,
        AuditEventType.monitoring_refusal: MonitorRefusal,
        AuditEventType.system1_trajectory_emitted: System1TrajectoryEmission,
        AuditEventType.system1_sensor_refusal: System1SensorRefusal,
    }
    return models[event.event_type].model_validate(event.payload)


def verify_audit_ledger(ledger: AuditLedger) -> AuditVerification:
    events = ledger.events()
    problems: list[str] = []
    chain_valid = True
    payloads_valid = True
    references_valid = True
    signatures_valid = True
    previous = GENESIS_HASH
    incidents: set[str] = set()
    cards: set[str] = set()
    reports: set[str] = set()
    reviews: dict[str, RecordedReview] = {}
    revoked_keys: set[str] = set()
    revoked_reviews: set[str] = set()
    approvals: set[str] = set()
    drift_responses: set[str] = set()

    for expected_sequence, event in enumerate(events, start=1):
        payload_json = _canonical_json(event.payload)
        payload_digest = _sha256_text(payload_json)
        expected_hash = _event_hash(
            event.sequence,
            event.event_type.value,
            event.subject_id,
            event.occurred_at,
            event.recorded_at,
            payload_digest,
            previous,
        )
        if (
            event.sequence != expected_sequence
            or event.previous_hash != previous
            or event.payload_sha256 != payload_digest
            or event.event_hash != expected_hash
            or event.event_id != f"AUD-{expected_hash[:24].upper()}"
        ):
            chain_valid = False
            problems.append(f"event {event.sequence}: hash-chain mismatch")
        previous = event.event_hash

        try:
            artifact = _validate_payload(event)
        except ValidationError:
            payloads_valid = False
            problems.append(f"event {event.sequence}: invalid typed payload")
            continue

        if isinstance(artifact, Incident):
            incidents.add(artifact.incident_id)
        elif isinstance(artifact, ChangeCard):
            if artifact.incident.incident_id not in incidents:
                references_valid = False
                problems.append(f"event {event.sequence}: missing incident dependency")
            cards.add(artifact.card_id)
        elif isinstance(artifact, SandboxReport):
            if artifact.card_id not in cards:
                references_valid = False
                problems.append(f"event {event.sequence}: missing card dependency")
            reports.add(artifact.report_id)
        elif isinstance(artifact, RecordedReview):
            if artifact.review.sandbox_report.report_id not in reports:
                references_valid = False
                problems.append(f"event {event.sequence}: missing sandbox dependency")
            try:
                verification = verify_release_review_pem(
                    artifact.review, artifact.public_key_pem
                )
            except DSLValidationError:
                verification = None
            if verification is None or not verification.valid:
                signatures_valid = False
                problems.append(f"event {event.sequence}: invalid review signature")
            reviews[artifact.review.review_id] = artifact
        elif isinstance(artifact, KeyRevocation):
            revoked_keys.add(artifact.key_id)
        elif isinstance(artifact, ReviewRevocation):
            if artifact.review_id not in reviews:
                references_valid = False
                problems.append(f"event {event.sequence}: missing review dependency")
            revoked_reviews.add(artifact.review_id)
        elif isinstance(artifact, SignedMonitorCycleReceipt):
            cycle_verification = verify_cycle_receipt(artifact)
            if not cycle_verification.valid:
                signatures_valid = False
                problems.append(f"event {event.sequence}: invalid monitor-cycle signature")
        elif isinstance(artifact, SignedGuardDecisionReceipt):
            guard_verification = verify_guard_decision(artifact)
            if not guard_verification.valid:
                signatures_valid = False
                problems.append(f"event {event.sequence}: invalid guard signature")
        elif isinstance(artifact, SignedApprovalReceipt):
            approval_verification = verify_tool_approval(artifact)
            if not approval_verification.valid:
                signatures_valid = False
                problems.append(f"event {event.sequence}: invalid approval signature")
            approvals.add(artifact.payload.approval_id)
        elif isinstance(artifact, MonitoringCycle):
            pass  # observation record: structural validation above is the check
        elif isinstance(artifact, DriftSignal):
            pass  # observation record: structural validation above is the check
        elif isinstance(artifact, DriftResponse):
            drift_responses.add(artifact.response_id)
        elif isinstance(artifact, DriftCoalescence):
            if artifact.response_id not in drift_responses:
                references_valid = False
                problems.append(
                    f"event {expected_sequence}: missing drift response dependency"
                )
        elif isinstance(artifact, DriftRefusal):
            pass  # observation record: a refusal declares that nothing acted
        elif isinstance(artifact, MonitorRefusal):
            pass  # observation record: a cycle refusal declares nothing was emitted
        elif isinstance(artifact, System1TrajectoryEmission):
            if not artifact.verification_valid:
                signatures_valid = False
                problems.append(f"event {event.sequence}: emitted System 1 trajectory is not verified")
        elif isinstance(artifact, System1SensorRefusal):
            pass  # observation record: source event window was refused
        elif isinstance(artifact, DriftBudgetRefusal):
            pass  # observation record: structural validation above is the check
        elif isinstance(artifact, SignedApprovalRevocationReceipt):
            if artifact.payload.approval_id not in approvals:
                references_valid = False
                problems.append(
                    f"event {event.sequence}: missing approval dependency"
                )
            revocation_verification = verify_tool_approval_revocation(artifact)
            if not revocation_verification.valid:
                signatures_valid = False
                problems.append(
                    f"event {event.sequence}: invalid approval-revocation signature"
                )

    active = sorted(
        review_id
        for review_id, recorded in reviews.items()
        if recorded.review.staged_release_authorized
        and review_id not in revoked_reviews
        and recorded.review.signature.key_id not in revoked_keys
    )
    valid = all([chain_valid, payloads_valid, references_valid, signatures_valid])
    return AuditVerification(
        valid=valid,
        event_count=len(events),
        head_hash=previous,
        chain_valid=chain_valid,
        payloads_valid=payloads_valid,
        references_valid=references_valid,
        signatures_valid=signatures_valid,
        revoked_key_ids=sorted(revoked_keys),
        revoked_review_ids=sorted(revoked_reviews),
        active_staged_review_ids=active,
        problems=problems,
    )


def load_audit_artifact(path: str | Path) -> ChangeCard | SandboxReport | ReleaseReview:
    resolved = Path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DSLValidationError([ValidationIssue("document", "is not valid UTF-8 JSON")]) from exc
    try:
        if "card_id" in raw and "incident" in raw:
            return ChangeCard.model_validate(raw)
        if "report_id" in raw and "runs" in raw:
            return SandboxReport.model_validate(raw)
        if "review_id" in raw and "signature" in raw:
            return ReleaseReview.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc
    raise DSLValidationError([ValidationIssue("document", "unsupported audit artifact")])
