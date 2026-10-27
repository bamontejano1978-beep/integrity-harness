"""Incident detection and idempotent SQLite registration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.models import (
    AutonomyLevel,
    ContractDocument,
    RecoveryClause,
    Severity,
)
from autopilot.evaluator import (
    ClauseStatus,
    ContractEvaluation,
    EvidenceRef,
    EvaluatorInfo,
)

DETECTOR_NAME = "contract_failure_incident_detector"
DETECTOR_VERSION = "0.1.0"
REGISTRY_SCHEMA_VERSION = "1"


class IncidentKind(str, Enum):
    behavioral_violation = "behavioral_violation"
    recovery_blocked = "recovery_blocked"


class IncidentStatus(str, Enum):
    open = "open"


class Incident(BaseModel):
    """Immutable incident created from one failed clause result."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    incident_id: str
    fingerprint: str
    kind: IncidentKind
    status: IncidentStatus = IncidentStatus.open
    detected_at: str
    contract_id: str
    contract_version: str
    clause_id: str
    clause_type: str
    clause_description: str
    severity: Severity
    trajectory_id: str
    candidate_version: str
    reason: str
    evidence: list[EvidenceRef]
    recommended_autonomy: AutonomyLevel
    block_release: bool
    source_evaluator: EvaluatorInfo
    detector_name: str = DETECTOR_NAME
    detector_version: str = DETECTOR_VERSION


class IncidentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str
    contract_version: str
    trajectory_id: str
    candidate_version: str
    detected_at: str
    incident_count: int = Field(ge=0)
    incidents: list[Incident]


class RegistrationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_path: str
    attempted: int = Field(ge=0)
    inserted: int = Field(ge=0)
    duplicates: int = Field(ge=0)


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def detect_incidents(
    document: ContractDocument,
    evaluation: ContractEvaluation,
) -> IncidentBatch:
    """Create one deterministic incident per failed clause result."""

    issues: list[ValidationIssue] = []
    if evaluation.contract_id != document.contract.id:
        issues.append(ValidationIssue("evaluation.contract_id", "does not match the contract id"))
    if evaluation.contract_version != document.contract.version:
        issues.append(
            ValidationIssue("evaluation.contract_version", "does not match the contract version")
        )

    clause_by_id = {clause.id: clause for clause in document.clauses}
    for index, result in enumerate(evaluation.clause_results):
        if result.clause_id not in clause_by_id:
            issues.append(
                ValidationIssue(
                    f"evaluation.clause_results[{index}].clause_id",
                    "does not exist in the contract",
                )
            )
        if result.contract_id != evaluation.contract_id:
            issues.append(
                ValidationIssue(
                    f"evaluation.clause_results[{index}].contract_id",
                    "does not match the evaluation contract id",
                )
            )
        if result.contract_version != evaluation.contract_version:
            issues.append(
                ValidationIssue(
                    f"evaluation.clause_results[{index}].contract_version",
                    "does not match the evaluation contract version",
                )
            )
    if issues:
        raise DSLValidationError(issues)

    incidents: list[Incident] = []
    for result in evaluation.clause_results:
        if result.status is not ClauseStatus.failed:
            continue

        clause = clause_by_id[result.clause_id]
        kind = (
            IncidentKind.recovery_blocked
            if isinstance(clause, RecoveryClause)
            else IncidentKind.behavioral_violation
        )
        fingerprint = _canonical_hash(
            {
                "contract_id": document.contract.id,
                "clause_id": clause.id,
                "kind": kind.value,
            }
        )
        incident_digest = _canonical_hash(
            {
                "fingerprint": fingerprint,
                "contract_version": document.contract.version,
                "trajectory_id": evaluation.trajectory_id,
                "candidate_version": evaluation.candidate_version,
                "detected_at": evaluation.evaluated_at,
                "evidence_refs": sorted(evidence.ref for evidence in result.evidence),
            }
        )
        incidents.append(
            Incident(
                incident_id=f"INC-{incident_digest[:24].upper()}",
                fingerprint=fingerprint,
                kind=kind,
                detected_at=evaluation.evaluated_at,
                contract_id=document.contract.id,
                contract_version=document.contract.version,
                clause_id=clause.id,
                clause_type=clause.type,
                clause_description=clause.description,
                severity=clause.severity,
                trajectory_id=evaluation.trajectory_id,
                candidate_version=evaluation.candidate_version,
                reason=result.reason,
                evidence=result.evidence,
                recommended_autonomy=result.recommended_autonomy,
                block_release=result.block_release,
                source_evaluator=result.evaluator,
            )
        )

    incidents.sort(key=lambda incident: (incident.clause_id, incident.incident_id))
    return IncidentBatch(
        contract_id=evaluation.contract_id,
        contract_version=evaluation.contract_version,
        trajectory_id=evaluation.trajectory_id,
        candidate_version=evaluation.candidate_version,
        detected_at=evaluation.evaluated_at,
        incident_count=len(incidents),
        incidents=incidents,
    )


class IncidentRegistry:
    """Small local SQLite incident store with idempotent inserts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS registry_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    contract_version TEXT NOT NULL,
                    clause_id TEXT NOT NULL,
                    clause_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    trajectory_id TEXT NOT NULL,
                    candidate_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
                    ON incidents(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_incidents_status_detected
                    ON incidents(status, detected_at DESC);
                """
            )
            row = connection.execute(
                "SELECT value FROM registry_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO registry_metadata(key, value) VALUES('schema_version', ?)",
                    (REGISTRY_SCHEMA_VERSION,),
                )
            elif row["value"] != REGISTRY_SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported incident registry schema version: " + row["value"]
                )

    def register(self, batch: IncidentBatch) -> RegistrationSummary:
        """Insert unseen incidents and report exact duplicate suppression."""

        inserted = 0
        registered_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            for incident in batch.incidents:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO incidents(
                        incident_id, fingerprint, detected_at, registered_at,
                        contract_id, contract_version, clause_id, clause_type,
                        severity, trajectory_id, candidate_version, status, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident.incident_id,
                        incident.fingerprint,
                        incident.detected_at,
                        registered_at,
                        incident.contract_id,
                        incident.contract_version,
                        incident.clause_id,
                        incident.clause_type,
                        incident.severity.value,
                        incident.trajectory_id,
                        incident.candidate_version,
                        incident.status.value,
                        incident.model_dump_json(),
                    ),
                )
                inserted += cursor.rowcount

        attempted = len(batch.incidents)
        return RegistrationSummary(
            registry_path=str(self.path),
            attempted=attempted,
            inserted=inserted,
            duplicates=attempted - inserted,
        )

    def list_incidents(
        self,
        *,
        status: IncidentStatus | None = None,
        limit: int = 100,
    ) -> list[Incident]:
        """Read incidents in reverse detection order."""

        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        query = "SELECT payload_json FROM incidents"
        parameters: tuple = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status.value,)
        query += " ORDER BY detected_at DESC, incident_id ASC LIMIT ?"
        parameters += (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Incident.model_validate_json(row["payload_json"]) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM incidents").fetchone()
        return int(row["total"])

