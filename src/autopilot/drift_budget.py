"""Run budget and signal coalescence for drift-triggered evaluations (P-07).

System 1 design packet, §1.5 and threat T-09: the deep evaluation consumes
agreed budget. A stream of drift signals (an avalanche per T-09, or a
legitimate release read as a change per T-05) must not buy one full run per
signal. This module owns the two declared cost-control rules and the records
that make their effect auditable:

- **Coalescence.** A drift signal is coalesced into an already recorded
  response when both describe the same episode: same baseline id and version,
  same contract id and version, and windows that share at least one evaluation
  cycle. A coalesced signal spends no run; the declaration names the response
  that already covers the episode and when its snapshot expires.
- **Budget.** At most ``max_runs`` recorded deep runs per sliding
  ``window_seconds`` window, counted per contract over the ``drift_response``
  records in the append-only ledger. The default (3 runs / 30 days) mirrors
  the commercial cadence claim; a different policy is an explicit input, and
  every refusal record carries the policy that produced it.
- **Fail-closed, never silent (I-2).** A signal that neither coalesces nor
  fits in the budget is refused with a ``drift_budget_refusal`` record: no
  run, no fresh snapshot, and the previous snapshot remains the guard's only
  authority until its TTL expires. The refusal is declared, not swallowed.

Boundaries (v0.1, declared):

- The budget counts **drift-triggered runs** (``drift_response`` records) for
  one contract; scheduled loop emissions and other event types do not enter
  the count.
- Coalescence matches episode identity, not the full signal payload: two
  signals over the same baseline, contract and overlapping window are one
  episode. A disjoint window opens a new episode and is subject to budget.
- The rules read ledger records and take an explicit ``now``; they never read
  or write autonomy. The engine's signed snapshot stays the only authority
  (I-1), and this module does not import the enforcement adapter.
- A malformed ``drift_response`` record in the ledger makes both rules refuse
  (``ValueError``): corrupt cost-control evidence is not guessed around.

Recording is done by the caller (``drift_response.run_drift_response``): these
records are observation events with no authority, validated for structure,
exactly like the loop cycle and the signal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from autopilot.drift import DriftSignal, _canonical_bytes

if TYPE_CHECKING:  # pragma: no cover - audit imports this module's records
    from autopilot.audit import AuditLedger

COALESCENCE_SCHEMA_VERSION = "0.1"
REFUSAL_SCHEMA_VERSION = "0.1"

DEFAULT_BUDGET_MAX_RUNS = 3
DEFAULT_BUDGET_WINDOW_SECONDS = 30 * 24 * 60 * 60


class DriftCoalescence(BaseModel):
    """The declared outcome of a signal folded into a recorded run.

    It spends no run and emits no snapshot; it records that the episode the
    signal describes is already covered by ``response_id``, with the shared
    cycles as the declared justification. No autonomy field exists here.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = COALESCENCE_SCHEMA_VERSION
    kind: Literal["drift_coalescence"] = "drift_coalescence"
    coalescence_id: str
    produced_by: str
    issued_at: str
    signal_id: str
    baseline_id: str
    baseline_version: int
    contract_id: str
    contract_version: str
    response_id: str
    triggered_cycle_id: str
    window_cycle_ids: list[str]
    shared_cycle_ids: list[str]
    snapshot_id: str
    snapshot_expires_at: str


class DriftBudgetRefusal(BaseModel):
    """The declared outcome of a signal the budget cannot afford.

    Fail-closed by construction: no run and no fresh snapshot happen, and the
    previous snapshot stays the guard's only authority until its TTL expires.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = REFUSAL_SCHEMA_VERSION
    kind: Literal["drift_budget_refusal"] = "drift_budget_refusal"
    refusal_id: str
    produced_by: str
    issued_at: str
    signal_id: str
    baseline_id: str
    baseline_version: int
    contract_id: str
    budget_max_runs: int
    budget_window_seconds: float
    runs_in_window: int
    earliest_release_at: str | None
    reason: str


class DriftBudgetExhausted(ValueError):
    """Fail-closed refusal raised after the refusal was declared in the ledger."""

    def __init__(self, message: str, *, refusal: DriftBudgetRefusal) -> None:
        super().__init__(message)
        self.refusal = refusal


@dataclass(frozen=True)
class BudgetPolicy:
    """Declared cost-control policy: runs per sliding window, per contract."""

    max_runs: int = DEFAULT_BUDGET_MAX_RUNS
    window_seconds: float = DEFAULT_BUDGET_WINDOW_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.max_runs, bool) or self.max_runs < 1:
            raise ValueError("budget max_runs must be an integer of at least 1")
        if self.window_seconds <= 0:
            raise ValueError("budget window_seconds must be positive")


@dataclass(frozen=True)
class BudgetState:
    """The budget as read from the ledger at one moment, for one contract."""

    contract_id: str
    max_runs: int
    window_seconds: float
    runs_in_window: int
    earliest_release_at: str | None

    @property
    def exhausted(self) -> bool:
        return self.runs_in_window >= self.max_runs


@dataclass(frozen=True)
class CoalescenceTarget:
    """The recorded response a signal is the same episode as."""

    response_id: str
    triggered_cycle_id: str
    contract_version: str
    window_cycle_ids: tuple[str, ...]
    shared_cycle_ids: tuple[str, ...]
    snapshot_id: str
    snapshot_expires_at: str


@dataclass(frozen=True)
class _RecordedRun:
    """Typed view of one recorded ``drift_response`` used by both rules."""

    response_id: str
    issued_at: datetime
    baseline_id: str
    baseline_version: int
    contract_id: str
    contract_version: str
    triggered_cycle_id: str
    window_cycle_ids: tuple[str, ...]
    snapshot_id: str
    snapshot_expires_at: str


def _recorded_moment(value: object, *, where: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{where} has no issued_at; refusing to read cost-control state")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{where} has an unparseable issued_at; refusing to read cost-control state"
        ) from exc
    if moment.tzinfo is None:
        raise ValueError(f"{where} has a naive issued_at; refusing to read cost-control state")
    return moment


def _required(payload: dict[str, Any], field: str, *, where: str, kind: type) -> Any:
    value = payload.get(field)
    if kind is str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{where} is malformed ({field}); refusing to act on it")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} is malformed ({field}); refusing to act on it")
    elif kind is list:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{where} is malformed ({field}); refusing to act on it")
    else:  # pragma: no cover - internal guard against a wrong kind argument
        raise ValueError(f"unsupported field kind for {field}")
    return value


def _recorded_runs(ledger: AuditLedger) -> list[_RecordedRun]:
    """Read every recorded run, refusing to guess around a malformed record."""

    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    runs: list[_RecordedRun] = []
    for event in ledger.events():
        if event.event_type is not AuditEventType.drift_response:
            continue
        where = f"recorded drift_response {event.subject_id}"
        payload = event.payload
        if not isinstance(payload, dict):
            raise ValueError(f"{where} is malformed; refusing to act on it")
        runs.append(
            _RecordedRun(
                response_id=_required(payload, "response_id", where=where, kind=str),
                issued_at=_recorded_moment(payload.get("issued_at"), where=where),
                baseline_id=_required(payload, "baseline_id", where=where, kind=str),
                baseline_version=_required(payload, "baseline_version", where=where, kind=int),
                contract_id=_required(payload, "contract_id", where=where, kind=str),
                contract_version=_required(payload, "contract_version", where=where, kind=str),
                triggered_cycle_id=_required(
                    payload, "triggered_cycle_id", where=where, kind=str
                ),
                window_cycle_ids=tuple(
                    _required(payload, "window_cycle_ids", where=where, kind=list)
                ),
                snapshot_id=_required(payload, "snapshot_id", where=where, kind=str),
                snapshot_expires_at=_required(
                    payload, "snapshot_expires_at", where=where, kind=str
                ),
            )
        )
    return runs


def budget_state(
    ledger: AuditLedger,
    *,
    contract_id: str,
    now: datetime,
    policy: BudgetPolicy,
) -> BudgetState:
    """Count recorded deep runs for ``contract_id`` inside the sliding window.

    Only ``drift_response`` records count: the budget protects the deep-run
    path, not the scheduled loop. A run issued after ``now`` is not counted
    (it cannot have consumed the window yet); a run older than the window has
    been released. ``earliest_release_at`` is when the oldest counted run
    leaves the window, so a refusal can declare when a retry may fit.
    """

    if now.tzinfo is None:
        raise ValueError("budget 'now' must include a timezone")
    window_start = now - timedelta(seconds=policy.window_seconds)
    counted = [
        run.issued_at
        for run in _recorded_runs(ledger)
        if run.contract_id == contract_id and window_start <= run.issued_at <= now
    ]
    earliest_release = (
        min(counted) + timedelta(seconds=policy.window_seconds) if counted else None
    )
    return BudgetState(
        contract_id=contract_id,
        max_runs=policy.max_runs,
        window_seconds=policy.window_seconds,
        runs_in_window=len(counted),
        earliest_release_at=earliest_release.isoformat() if earliest_release else None,
    )


def coalescence_target(
    ledger: AuditLedger,
    *,
    signal: DriftSignal,
    contract_id: str,
    contract_version: str,
) -> CoalescenceTarget | None:
    """The most recent recorded response the signal is the same episode as.

    Identity is ``(baseline_id, baseline_version, contract_id,
    contract_version)`` plus a non-empty window overlap; among all matching
    responses the latest by ``(issued_at, response_id)`` wins, so the choice
    is deterministic. Returns ``None`` when the signal opens a new episode.
    """

    candidates: list[tuple[datetime, _RecordedRun, tuple[str, ...]]] = []
    for run in _recorded_runs(ledger):
        if run.baseline_id != signal.baseline_id:
            continue
        if run.baseline_version != signal.baseline_version:
            continue
        if run.contract_id != contract_id or run.contract_version != contract_version:
            continue
        recorded_cycles = set(run.window_cycle_ids)
        shared = tuple(
            cycle_id for cycle_id in signal.window_cycle_ids if cycle_id in recorded_cycles
        )
        if shared:
            candidates.append((run.issued_at, run, shared))
    if not candidates:
        return None
    _, run, shared = max(candidates, key=lambda item: (item[0], item[1].response_id))
    return CoalescenceTarget(
        response_id=run.response_id,
        triggered_cycle_id=run.triggered_cycle_id,
        contract_version=run.contract_version,
        window_cycle_ids=run.window_cycle_ids,
        shared_cycle_ids=shared,
        snapshot_id=run.snapshot_id,
        snapshot_expires_at=run.snapshot_expires_at,
    )


def _decision_id(prefix: str, body: dict[str, object]) -> str:
    return prefix + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:24].upper()


def build_coalescence(
    *,
    signal: DriftSignal,
    target: CoalescenceTarget,
    produced_by: str,
    issued_at: str,
) -> DriftCoalescence:
    """Assemble the declaration for a signal folded into a recorded run."""

    coalescence_id = _decision_id(
        "COALESCE-",
        {
            "signal_id": signal.signal_id,
            "response_id": target.response_id,
            "produced_by": produced_by,
            "issued_at": issued_at,
        },
    )
    return DriftCoalescence(
        coalescence_id=coalescence_id,
        produced_by=produced_by,
        issued_at=issued_at,
        signal_id=signal.signal_id,
        baseline_id=signal.baseline_id,
        baseline_version=signal.baseline_version,
        contract_id=signal.contract_id,
        contract_version=target.contract_version,
        response_id=target.response_id,
        triggered_cycle_id=target.triggered_cycle_id,
        window_cycle_ids=list(signal.window_cycle_ids),
        shared_cycle_ids=list(target.shared_cycle_ids),
        snapshot_id=target.snapshot_id,
        snapshot_expires_at=target.snapshot_expires_at,
    )


def build_refusal(
    *,
    signal: DriftSignal,
    produced_by: str,
    issued_at: str,
    policy: BudgetPolicy,
    state: BudgetState,
) -> DriftBudgetRefusal:
    """Assemble the fail-closed declaration for a signal the budget cannot afford."""

    refusal_id = _decision_id(
        "REFUSE-",
        {
            "signal_id": signal.signal_id,
            "produced_by": produced_by,
            "issued_at": issued_at,
            "budget_max_runs": policy.max_runs,
            "budget_window_seconds": policy.window_seconds,
        },
    )
    reason = (
        f"contract {signal.contract_id} already holds {state.runs_in_window} of "
        f"{policy.max_runs} recorded deep runs in the last {policy.window_seconds:g}s window; "
        f"refusing signal {signal.signal_id}: no fresh snapshot, the previous snapshot "
        "remains the only authority until it expires"
    )
    return DriftBudgetRefusal(
        refusal_id=refusal_id,
        produced_by=produced_by,
        issued_at=issued_at,
        signal_id=signal.signal_id,
        baseline_id=signal.baseline_id,
        baseline_version=signal.baseline_version,
        contract_id=signal.contract_id,
        budget_max_runs=policy.max_runs,
        budget_window_seconds=policy.window_seconds,
        runs_in_window=state.runs_in_window,
        earliest_release_at=state.earliest_release_at,
        reason=reason,
    )
