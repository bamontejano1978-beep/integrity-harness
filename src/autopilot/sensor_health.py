"""One-line sensor health for the scheduled monitoring loop (System 1 v0.1).

Between ticks an operator needs three facts, not a dashboard: is the guard
judging against a fresh signed snapshot, is there a signal nobody answered,
and did any recovery end without a run. This module answers them from what is
already on disk and in the ledger — read-only, no writes, no authority.

The three questions map to the three failure modes the loop can hide:

* **Snapshot age** — an absent, expired or untrusted ``guard-snapshot.json``
  means the guard is (or is about to be) in quarantine: availability costs,
  never assurance.
* **Pending signals** — a recorded ``drift_signal`` with no respond-stage
  record. ``recover_pending`` answers it on the next tick, so it is
  ``degraded``, not a refusal; the line names the oldest one so a backlog is
  visible before it becomes routine.
* **Recovery gaps** — signals a refusal answered instead of a run (budget
  spent, window this node never saw, engine run refused). No fresh snapshot
  came out of them, so an operator has to look.

Exit convention of the CLI: ``0`` ok, ``1`` degraded, ``2`` critical —
matching ``incidents``/``diagnose`` (findings → 1) and the refusal family
(unusable input → 2).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from autopilot.drift import _parse_moment
from autopilot.drift_watch import declined_signals, pending_signals
from autopilot.enforcement import SignedEvaluationSnapshot, verify_evaluation_snapshot
from autopilot.monitoring_loop import SNAPSHOT_FILENAME, load_cycle_history

if TYPE_CHECKING:  # pragma: no cover - annotation only, keeps the import light
    from datetime import datetime

    from autopilot.audit import AuditLedger

SENSOR_HEALTH_SCHEMA_VERSION = "0.1"
# A snapshot with less than a quarter of its TTL left is flagged while there is
# still room to re-emit it: the next scheduled tick may come too late.
AGING_REMAINING_FRACTION = 0.25


class SensorHealthStatus(str, Enum):
    """How the sensor side looks right now, in operator terms."""

    ok = "ok"
    degraded = "degraded"
    critical = "critical"


class SensorHealthReport(BaseModel):
    """Read-only health of the sensor side, rendered as one line."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = SENSOR_HEALTH_SCHEMA_VERSION
    kind: Literal["sensor_health"] = "sensor_health"
    checked_at: str
    status: SensorHealthStatus
    snapshot_id: str | None = None
    snapshot_issued_at: str | None = None
    snapshot_expires_at: str | None = None
    snapshot_age_seconds: float | None = None
    snapshot_ttl_left_seconds: float | None = None
    snapshot_verified: bool | None = None
    pending_signals: int = 0
    oldest_pending_signal_id: str | None = None
    oldest_pending_age_seconds: float | None = None
    recovery_gaps: int = 0
    gap_signal_ids: list[str] = Field(default_factory=list)
    cycles_seen: int = 0
    contract_id: str | None = None
    contract_version: str | None = None
    reasons: list[str] = Field(default_factory=list)
    detail: str = ""

    def line(self) -> str:
        """Render the report as a single operator-readable line."""

        if self.snapshot_id is None:
            snapshot = "snapshot missing"
        else:
            snapshot = (
                f"snapshot {self.snapshot_id} age={_seconds(self.snapshot_age_seconds)} "
                f"ttl_left={_seconds(self.snapshot_ttl_left_seconds)}"
            )
        pending = f"pending={self.pending_signals}"
        if self.oldest_pending_signal_id is not None:
            pending += (
                f" (oldest {self.oldest_pending_signal_id}, "
                f"{_seconds(self.oldest_pending_age_seconds)})"
            )
        parts = [
            snapshot,
            pending,
            f"gaps={self.recovery_gaps}",
            f"cycles={self.cycles_seen}",
        ]
        if self.contract_id is not None:
            parts.append(f"contract {self.contract_id} v{self.contract_version}")
        if self.reasons:
            parts.append("reasons=" + ",".join(self.reasons))
        return f"SENSOR HEALTH: {self.status.value}; " + "; ".join(parts)


def _seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}s"


def _age(moment: "datetime", stamp: str) -> float:
    return (moment - _parse_moment(stamp)).total_seconds()


def build_sensor_health(
    output_dir: str | Path,
    *,
    ledger: "AuditLedger",
    checked_at: str | None = None,
    trust_key_path: str | Path | None = None,
) -> SensorHealthReport:
    """Read the sensor side and report it (never writes, never decides).

    ``trust_key_path`` anchors the snapshot signature the way the guard does.
    Without it the signature is checked for self-consistency and freshness only
    and ``snapshot_verified`` stays ``None``: an unanchored key is not an
    identity, so the report does not pretend to have verified one.
    """

    moment = _parse_moment(checked_at)
    stage = Path(output_dir)
    history = load_cycle_history(stage)
    last = history[-1] if history else None

    reasons: list[str] = []
    detail: list[str] = []
    snapshot_id = issued = expires = None
    age = ttl_left = None
    verified: bool | None = None
    snapshot_path = stage / SNAPSHOT_FILENAME
    if not snapshot_path.is_file():
        reasons.append("snapshot_missing")
        detail.append("no guard snapshot on disk: the guard quarantines")
    else:
        try:
            snapshot = SignedEvaluationSnapshot.model_validate_json(
                snapshot_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise ValueError(f"{snapshot_path} is not a readable guard snapshot: {exc}") from exc
        payload = snapshot.payload
        snapshot_id = payload.snapshot_id
        issued = payload.issued_at
        expires = payload.expires_at
        age = _age(moment, issued)
        ttl_left = -_age(moment, expires)
        verification = verify_evaluation_snapshot(snapshot, trust_key_path, now=moment)
        if trust_key_path is not None:
            verified = verification.valid
        if ttl_left <= 0:
            reasons.append("snapshot_expired")
            detail.append(f"the guard snapshot expired {_seconds(-ttl_left)} ago")
        else:
            if trust_key_path is not None and not verification.valid:
                reasons.append("snapshot_not_trusted")
                detail.append(
                    "the guard snapshot does not verify against the trust key: "
                    + "; ".join(verification.problems)
                )
            ttl = _age(_parse_moment(expires), issued)
            if ttl > 0 and ttl_left <= ttl * AGING_REMAINING_FRACTION:
                reasons.append("snapshot_aging")
                detail.append(f"only {_seconds(ttl_left)} of the snapshot TTL remain")

    pending = pending_signals(ledger)
    gaps = declined_signals(ledger)
    oldest_id = None
    oldest_age = None
    if pending:
        reasons.append("pending_signals")
        oldest = min(pending, key=lambda signal: _parse_moment(signal.issued_at))
        oldest_id = oldest.signal_id
        oldest_age = _age(moment, oldest.issued_at)
        detail.append(
            f"{len(pending)} recorded signal(s) without an answer; the next tick answers one"
        )
    if gaps:
        reasons.append("recovery_gaps")
        detail.append(
            f"{len(gaps)} signal(s) were answered by a refusal, so no fresh snapshot came out"
        )

    if {"snapshot_missing", "snapshot_expired", "snapshot_not_trusted"} & set(reasons):
        status = SensorHealthStatus.critical
    elif pending or gaps or "snapshot_aging" in reasons:
        status = SensorHealthStatus.degraded
    else:
        status = SensorHealthStatus.ok
        detail.append("fresh signed snapshot; no pending signal and no recovery gap")

    return SensorHealthReport(
        checked_at=moment.isoformat(),
        status=status,
        snapshot_id=snapshot_id,
        snapshot_issued_at=issued,
        snapshot_expires_at=expires,
        snapshot_age_seconds=age,
        snapshot_ttl_left_seconds=ttl_left,
        snapshot_verified=verified,
        pending_signals=len(pending),
        oldest_pending_signal_id=oldest_id,
        oldest_pending_age_seconds=oldest_age,
        recovery_gaps=len(gaps),
        gap_signal_ids=[signal.signal_id for signal in gaps],
        cycles_seen=len(history),
        contract_id=last.contract_id if last is not None else None,
        contract_version=last.contract_version if last is not None else None,
        reasons=reasons,
        detail="; ".join(detail),
    )


__all__ = [
    "AGING_REMAINING_FRACTION",
    "SensorHealthReport",
    "SensorHealthStatus",
    "build_sensor_health",
]
