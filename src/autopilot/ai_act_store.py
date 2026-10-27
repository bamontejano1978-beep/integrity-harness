"""Persistent, idempotent store for longitudinal AI Act readiness evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

from autopilot.ai_act_monitor import (
    ImportedEvaluation,
    ObligationMap,
    SystemProfile,
)

STORE_SCHEMA_VERSION = "2"


class StoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestionSummary(StoreModel):
    ingestion_id: str
    database_path: str
    system_id: str
    profile_changed: bool
    obligation_map_changed: bool
    evaluations_attempted: int = Field(ge=0)
    evaluations_inserted: int = Field(ge=0)
    evaluation_duplicates: int = Field(ge=0)


class StoredSystemSummary(StoreModel):
    system_id: str
    name: str
    provider: str
    current_version: str
    evaluation_count: int = Field(ge=0)
    last_evaluated_at: str | None
    updated_at: str


class StoreSnapshot(StoreModel):
    profile: SystemProfile
    obligation_map: ObligationMap
    evaluations: list[ImportedEvaluation]


class MonitorState(StoreModel):
    system_id: str
    state_fingerprint: str
    current_version: str
    autonomy: str
    drift_index: int = Field(ge=0, le=100)
    coverage_percent: int = Field(ge=0, le=100)
    alert_fingerprints: list[str]
    updated_at: str


class CycleRegistration(StoreModel):
    cycle_id: str
    inserted: bool


def _canonical_json(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    payload = model.model_dump(mode="json", exclude=exclude or set())
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AIActLongitudinalStore:
    """SQLite store for system snapshots and immutable evaluation evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_profiles (
                    system_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    current_version TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS obligation_maps (
                    system_id TEXT PRIMARY KEY,
                    map_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(system_id) REFERENCES system_profiles(system_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    system_id TEXT NOT NULL,
                    candidate_version TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    trajectory_id TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recommended_autonomy TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    FOREIGN KEY(system_id) REFERENCES system_profiles(system_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_ai_act_evaluations_system_time
                    ON evaluations(system_id, evaluated_at, evaluation_id);
                CREATE INDEX IF NOT EXISTS idx_ai_act_evaluations_version
                    ON evaluations(system_id, candidate_version, evaluated_at);

                CREATE TABLE IF NOT EXISTS ingestion_events (
                    ingestion_id TEXT PRIMARY KEY,
                    system_id TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    attempted INTEGER NOT NULL,
                    inserted INTEGER NOT NULL,
                    duplicates INTEGER NOT NULL,
                    profile_changed INTEGER NOT NULL,
                    obligation_map_changed INTEGER NOT NULL,
                    FOREIGN KEY(system_id) REFERENCES system_profiles(system_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS monitor_states (
                    system_id TEXT PRIMARY KEY,
                    state_fingerprint TEXT NOT NULL,
                    current_version TEXT NOT NULL,
                    autonomy TEXT NOT NULL,
                    drift_index INTEGER NOT NULL,
                    coverage_percent INTEGER NOT NULL,
                    alert_fingerprints_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(system_id) REFERENCES system_profiles(system_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS monitor_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    system_id TEXT NOT NULL,
                    ingestion_id TEXT NOT NULL,
                    ran_at TEXT NOT NULL,
                    state_changed INTEGER NOT NULL,
                    change_kind TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    FOREIGN KEY(system_id) REFERENCES system_profiles(system_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_monitor_cycles_system_time
                    ON monitor_cycles(system_id, ran_at, cycle_id);
                """
            )
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO store_metadata(key, value) VALUES('schema_version', ?)",
                    (STORE_SCHEMA_VERSION,),
                )
            elif row["value"] == "1":
                connection.execute(
                    "UPDATE store_metadata SET value = ? WHERE key = 'schema_version'",
                    (STORE_SCHEMA_VERSION,),
                )
            elif row["value"] != STORE_SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported AI Act store schema version: " + row["value"]
                )

    def ingest(
        self,
        profile: SystemProfile,
        obligation_map: ObligationMap,
        evaluations: list[ImportedEvaluation],
        *,
        ingested_at: str | None = None,
    ) -> IngestionSummary:
        """Upsert declared snapshots and append unseen evaluation evidence."""

        timestamp = ingested_at or datetime.now(timezone.utc).isoformat()
        profile_json = _canonical_json(profile)
        profile_hash = _digest(profile_json)
        map_json = _canonical_json(obligation_map)
        map_hash = _digest(map_json)
        current_version = max(
            profile.versions,
            key=lambda item: datetime.fromisoformat(item.released_at.replace("Z", "+00:00")),
        ).version

        inserted = 0
        evaluation_ids: list[str] = []
        with self._connect() as connection:
            existing_profile = connection.execute(
                "SELECT profile_hash FROM system_profiles WHERE system_id = ?",
                (profile.system_id,),
            ).fetchone()
            profile_changed = (
                existing_profile is None or existing_profile["profile_hash"] != profile_hash
            )
            connection.execute(
                """
                INSERT INTO system_profiles(
                    system_id, name, provider, current_version,
                    profile_hash, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(system_id) DO UPDATE SET
                    name = excluded.name,
                    provider = excluded.provider,
                    current_version = excluded.current_version,
                    profile_hash = excluded.profile_hash,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.system_id,
                    profile.name,
                    profile.provider,
                    current_version,
                    profile_hash,
                    profile_json,
                    timestamp,
                ),
            )

            existing_map = connection.execute(
                "SELECT map_hash FROM obligation_maps WHERE system_id = ?",
                (profile.system_id,),
            ).fetchone()
            map_changed = existing_map is None or existing_map["map_hash"] != map_hash
            connection.execute(
                """
                INSERT INTO obligation_maps(system_id, map_hash, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(system_id) DO UPDATE SET
                    map_hash = excluded.map_hash,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (profile.system_id, map_hash, map_json, timestamp),
            )

            for evaluation in evaluations:
                evidence_json = _canonical_json(evaluation, exclude={"source_path"})
                evaluation_id = f"EVAL-{_digest(evidence_json)[:32].upper()}"
                evaluation_ids.append(evaluation_id)
                payload = json.loads(evidence_json)
                payload["source_path"] = evaluation.source_path
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO evaluations(
                        evaluation_id, system_id, candidate_version, contract_id,
                        trajectory_id, evaluated_at, status, recommended_autonomy,
                        source_path, payload_json, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation_id,
                        profile.system_id,
                        evaluation.candidate_version,
                        evaluation.contract_id,
                        evaluation.trajectory_id,
                        evaluation.evaluated_at,
                        evaluation.status,
                        evaluation.recommended_autonomy.value,
                        evaluation.source_path,
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ),
                        timestamp,
                    ),
                )
                inserted += cursor.rowcount

            attempted = len(evaluations)
            duplicates = attempted - inserted
            event_seed = json.dumps(
                {
                    "system_id": profile.system_id,
                    "ingested_at": timestamp,
                    "profile_hash": profile_hash,
                    "map_hash": map_hash,
                    "evaluation_ids": sorted(evaluation_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            ingestion_id = f"ING-{_digest(event_seed)[:24].upper()}"
            connection.execute(
                """
                INSERT OR IGNORE INTO ingestion_events(
                    ingestion_id, system_id, ingested_at, attempted, inserted,
                    duplicates, profile_changed, obligation_map_changed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingestion_id,
                    profile.system_id,
                    timestamp,
                    attempted,
                    inserted,
                    duplicates,
                    int(profile_changed),
                    int(map_changed),
                ),
            )

            stored_ingestion = connection.execute(
                "SELECT * FROM ingestion_events WHERE ingestion_id = ?",
                (ingestion_id,),
            ).fetchone()

        return IngestionSummary(
            ingestion_id=ingestion_id,
            database_path=str(self.path),
            system_id=profile.system_id,
            profile_changed=bool(stored_ingestion["profile_changed"]),
            obligation_map_changed=bool(stored_ingestion["obligation_map_changed"]),
            evaluations_attempted=stored_ingestion["attempted"],
            evaluations_inserted=stored_ingestion["inserted"],
            evaluation_duplicates=stored_ingestion["duplicates"],
        )

    def load_snapshot(self, system_id: str) -> StoreSnapshot:
        with self._connect() as connection:
            profile_row = connection.execute(
                "SELECT payload_json FROM system_profiles WHERE system_id = ?",
                (system_id,),
            ).fetchone()
            map_row = connection.execute(
                "SELECT payload_json FROM obligation_maps WHERE system_id = ?",
                (system_id,),
            ).fetchone()
            evaluation_rows = connection.execute(
                """
                SELECT payload_json FROM evaluations
                WHERE system_id = ?
                ORDER BY evaluated_at, evaluation_id
                """,
                (system_id,),
            ).fetchall()
        if profile_row is None:
            raise KeyError(f"unknown system_id: {system_id}")
        if map_row is None:
            raise KeyError(f"no obligation map stored for system_id: {system_id}")
        return StoreSnapshot(
            profile=SystemProfile.model_validate_json(profile_row["payload_json"]),
            obligation_map=ObligationMap.model_validate_json(map_row["payload_json"]),
            evaluations=[
                ImportedEvaluation.model_validate_json(row["payload_json"])
                for row in evaluation_rows
            ],
        )

    def list_systems(self) -> list[StoredSystemSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    profile.system_id,
                    profile.name,
                    profile.provider,
                    profile.current_version,
                    profile.updated_at,
                    COUNT(evaluation.evaluation_id) AS evaluation_count,
                    MAX(evaluation.evaluated_at) AS last_evaluated_at
                FROM system_profiles AS profile
                LEFT JOIN evaluations AS evaluation
                    ON evaluation.system_id = profile.system_id
                GROUP BY profile.system_id
                ORDER BY profile.name, profile.system_id
                """
            ).fetchall()
        return [
            StoredSystemSummary(
                system_id=row["system_id"],
                name=row["name"],
                provider=row["provider"],
                current_version=row["current_version"],
                evaluation_count=row["evaluation_count"],
                last_evaluated_at=row["last_evaluated_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def count_ingestions(self, system_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM ingestion_events WHERE system_id = ?",
                (system_id,),
            ).fetchone()
        return int(row["total"])

    def get_monitor_state(self, system_id: str) -> MonitorState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_states WHERE system_id = ?",
                (system_id,),
            ).fetchone()
        if row is None:
            return None
        return MonitorState(
            system_id=row["system_id"],
            state_fingerprint=row["state_fingerprint"],
            current_version=row["current_version"],
            autonomy=row["autonomy"],
            drift_index=row["drift_index"],
            coverage_percent=row["coverage_percent"],
            alert_fingerprints=json.loads(row["alert_fingerprints_json"]),
            updated_at=row["updated_at"],
        )

    def save_monitor_state(self, state: MonitorState) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monitor_states(
                    system_id, state_fingerprint, current_version, autonomy,
                    drift_index, coverage_percent, alert_fingerprints_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(system_id) DO UPDATE SET
                    state_fingerprint = excluded.state_fingerprint,
                    current_version = excluded.current_version,
                    autonomy = excluded.autonomy,
                    drift_index = excluded.drift_index,
                    coverage_percent = excluded.coverage_percent,
                    alert_fingerprints_json = excluded.alert_fingerprints_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.system_id,
                    state.state_fingerprint,
                    state.current_version,
                    state.autonomy,
                    state.drift_index,
                    state.coverage_percent,
                    json.dumps(state.alert_fingerprints, separators=(",", ":")),
                    state.updated_at,
                ),
            )

    def register_monitor_cycle(
        self,
        *,
        cycle_id: str,
        system_id: str,
        ingestion_id: str,
        ran_at: str,
        state_changed: bool,
        change_kind: str,
        receipt_json: str,
    ) -> CycleRegistration:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO monitor_cycles(
                    cycle_id, system_id, ingestion_id, ran_at,
                    state_changed, change_kind, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    system_id,
                    ingestion_id,
                    ran_at,
                    int(state_changed),
                    change_kind,
                    receipt_json,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT receipt_json FROM monitor_cycles WHERE cycle_id = ?",
                    (cycle_id,),
                ).fetchone()
                if row["receipt_json"] != receipt_json:
                    raise ValueError("cycle_id already exists with a different receipt")
        return CycleRegistration(cycle_id=cycle_id, inserted=bool(cursor.rowcount))

    def count_monitor_cycles(self, system_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM monitor_cycles WHERE system_id = ?",
                (system_id,),
            ).fetchone()
        return int(row["total"])

    def load_monitor_cycle_receipt(self, cycle_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM monitor_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
        return row["receipt_json"] if row is not None else None
