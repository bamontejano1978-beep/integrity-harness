"""Automatic drift watch over the A2 cycle history (System 1 OP-2/OP-3 wiring).

The monitoring loop appends one ``MonitoringCycle`` per scheduled emission to
``cycle-history.jsonl``. This module chains that history to the two drift
pieces the design packet names:

* **OP-2** — anchor a signed, versioned baseline on the *first* learning
  window of the history. It is written once; re-baselining keeps requiring a
  named human approval (threat T-04), never a scheduler.
* **OP-3** — compare a bounded trailing window of cycles against that
  baseline and, when the evaluation-state stream shifts, record exactly one
  ``drift_signal`` in the ledger.

Automation is deliberately narrow: a five-minute scheduler must not turn the
ledger into a stream of identical signals while behaviour is unchanged. A
window already compared against the same baseline — recorded in
``drift-watch-state.json`` *and* re-checked in the ledger — is not compared
again, so the state file is an optimisation and a lost or stale state file
cannot duplicate a signal.

Closing the circuit (``respond_to_watch``): a recorded ``drift`` signal can
trigger the deep evaluation through :func:`autopilot.drift_response.run_drift_response`
without an operator in between — and only through it, so coalescence, the
declared budget and the ledger replay guard keep applying unchanged. When the
answer is a run, the loop re-emits the signed snapshot the guard reads.

**A crash is not a lost signal.** If the process died between recording the
signal and answering it, ``recover_pending`` finds it on the next tick and
sends it down the same path — same ledger replay guard, same coalescence, same
budget. Only signals with no respond-stage record at all are pending, so a
declined answer is never retried.

**The detector never observes its own reaction.** A cycle produced by a
drift response is a reaction to what was observed, not a new observation;
feeding it back into the window would make the watch re-signal the same
episode on every tick. Response cycles are excluded from the comparison
history, identified from the ledger (``drift_response.triggered_cycle_id``)
so the exclusion survives a lost state file and never depends on a label.
The remaining chain stays contiguous: a response re-evaluates exactly the
trajectory that triggered it, so its evaluation fingerprint equals the
trigger's and the next scheduled cycle links to it. Should a response ever
carry a different fingerprint, the window fails the contiguity check and is
refused fail-closed instead of being compared silently.

No authority: this module never builds a snapshot, never imports the
enforcement adapter and carries no autonomy field (invariant I-1). A drift
signal is an observation; the signed snapshot remains the only authority.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from autopilot.drift import (
    DriftSignal,
    DriftStatus,
    DriftTolerances,
    _window_digest,
    build_baseline,
    detect_drift,
    load_signed_baseline,
    refuse_drift,
    write_baseline,
)
from autopilot.monitoring_loop import (
    DEFAULT_INTERVAL_SECONDS,
    EVALUATION_SNAPSHOT_TTL_SECONDS,
    CycleChange,
    MonitoringCycle,
    load_cycle_history,
)

if TYPE_CHECKING:  # pragma: no cover - audit imports the drift modules
    from autopilot.audit import AuditLedger
    from autopilot.drift_budget import BudgetPolicy, DriftCoalescence
    from autopilot.drift_response import DriftResponse

DRIFT_WATCH_SCHEMA_VERSION = "0.1"
DRIFT_WATCH_STATE_FILENAME = "drift-watch-state.json"
DEFAULT_BASELINE_FILENAME = "drift-baseline.json"
# Declared defaults, not hidden constants: three cycles anchor the baseline and
# three cycles are compared on each tick. Both are overridable per invocation.
DEFAULT_LEARNING_CYCLES = 3
DEFAULT_WINDOW_CYCLES = 3


class DriftWatchOutcome(str, Enum):
    """What one watch tick decided, including the cases that decided nothing."""

    insufficient_window = "insufficient_window"
    baseline_created = "baseline_created"
    clear = "clear"
    drift = "drift"
    indeterminate = "indeterminate"
    unchanged_window = "unchanged_window"


class DriftWatchState(BaseModel):
    """Last comparison performed, so a repeated tick does not repeat the signal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = DRIFT_WATCH_SCHEMA_VERSION
    kind: Literal["drift_watch_state"] = "drift_watch_state"
    baseline_id: str
    baseline_version: int
    last_window_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    last_status: DriftStatus
    last_signal_id: str | None = None
    checked_at: str


class DriftWatchReport(BaseModel):
    """Result of one automatic drift-watch tick (observation record only)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = DRIFT_WATCH_SCHEMA_VERSION
    kind: Literal["drift_watch"] = "drift_watch"
    checked_at: str
    outcome: DriftWatchOutcome
    cycles_seen: int
    baseline_id: str | None = None
    baseline_version: int | None = None
    baseline_created: bool = False
    window_cycle_ids: list[str] = Field(default_factory=list)
    signal: DriftSignal | None = None
    excluded_response_cycles: int = 0
    detail: str = ""


def _moment(issued_at: str | None) -> datetime:
    if issued_at is None:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("issued_at must include a timezone")
    return moment


def _atomic_write(path: Path, document: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_drift_watch_state(history_dir: str | Path) -> DriftWatchState | None:
    """Load the last comparison record, or ``None`` when no tick has run yet."""

    path = Path(history_dir) / DRIFT_WATCH_STATE_FILENAME
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not readable drift-watch state: {exc}") from exc
    try:
        return DriftWatchState.model_validate(document)
    except Exception as exc:
        raise ValueError(f"{path} is not a valid drift-watch state record: {exc}") from exc


def _write_state(
    history_dir: Path,
    *,
    baseline_id: str,
    baseline_version: int,
    window_sha256: str,
    status: DriftStatus,
    signal_id: str | None,
    checked_at: str,
) -> None:
    state = DriftWatchState(
        baseline_id=baseline_id,
        baseline_version=baseline_version,
        last_window_sha256=window_sha256,
        last_status=status,
        last_signal_id=signal_id,
        checked_at=checked_at,
    )
    _atomic_write(
        history_dir / DRIFT_WATCH_STATE_FILENAME,
        state.model_dump_json(indent=2) + "\n",
    )


def _has_recorded_signal(
    ledger: "AuditLedger", *, baseline_id: str, window_sha256: str
) -> bool:
    """True when this exact window was already compared and recorded against the baseline."""

    from autopilot.audit import AuditEventType  # imported lazily: audit imports the drift modules

    for event in ledger.events():
        if event.event_type is not AuditEventType.drift_signal:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        if (
            payload.get("baseline_id") == baseline_id
            and payload.get("window_sha256") == window_sha256
        ):
            return True
    return False


def _response_cycle_ids(ledger: "AuditLedger | None") -> set[str]:
    """Cycle ids produced by a drift response: reactions, not observations.

    The ledger is the source of truth on purpose: a label (``produced_by``)
    can be chosen freely by the caller, while the recorded
    ``triggered_cycle_id`` of an answered signal is evidence.
    """

    if ledger is None:
        return set()
    from autopilot.audit import AuditEventType  # imported lazily: audit imports the drift modules

    cycle_ids: set[str] = set()
    for event in ledger.events():
        if event.event_type is not AuditEventType.drift_response:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        triggered = payload.get("triggered_cycle_id")
        if isinstance(triggered, str):
            cycle_ids.add(triggered)
    return cycle_ids


def run_drift_watch(
    history_dir: str | Path,
    *,
    authority_private_key: str | Path,
    authority_public_key: str | Path,
    trust_key_path: str | Path | None = None,
    baseline_path: str | Path | None = None,
    learning_cycles: int = DEFAULT_LEARNING_CYCLES,
    window_cycles: int = DEFAULT_WINDOW_CYCLES,
    tolerances: DriftTolerances | None = None,
    latest_baseline_id: str | None = None,
    produced_by: str = "integrity-drift-watch",
    issued_at: str | None = None,
    ledger: "AuditLedger | None" = None,
) -> DriftWatchReport:
    """Chain the cycle history to OP-2/OP-3: anchor once, then compare each new window.

    Refusals keep the drift path's fail-closed behaviour: an unverified or
    foreign-key baseline raises ``DriftRefusalError`` after the refusal is
    recorded in the ledger. Nothing here relaxes or tightens autonomy.
    """

    if learning_cycles < 1:
        raise ValueError("learning_cycles must be at least 1")
    if window_cycles < 1:
        raise ValueError("window_cycles must be at least 1")
    moment = _moment(issued_at)
    stage = Path(history_dir)
    trust = Path(trust_key_path) if trust_key_path is not None else Path(authority_public_key)

    recorded: list[MonitoringCycle] = load_cycle_history(stage)
    reactions = _response_cycle_ids(ledger)
    history: list[MonitoringCycle] = [
        cycle for cycle in recorded if cycle.cycle_id not in reactions
    ]
    excluded_response_cycles = len(recorded) - len(history)
    # The head of a chain is classified ``change=baseline`` because it has no
    # predecessor: that is a scheduling fact, not a behavioural class. Feeding it
    # to the distributions would make the very first comparison report a new
    # category and a drift that nothing observed, so the watch treats the chain
    # head as context and starts from the first cycle that has a predecessor.
    start = 0
    while start < len(history) and history[start].change is CycleChange.baseline:
        start += 1
    cycles = history[start:]
    if len(cycles) < learning_cycles:
        return DriftWatchReport(
            checked_at=moment.isoformat(),
            outcome=DriftWatchOutcome.insufficient_window,
            cycles_seen=len(history),
            excluded_response_cycles=excluded_response_cycles,
            detail=(
                f"{len(cycles)} comparable cycle(s) recorded ({len(history)} total); "
                f"{learning_cycles} are needed to anchor the baseline"
            ),
        )

    baseline_file = (
        Path(baseline_path) if baseline_path is not None else stage / DEFAULT_BASELINE_FILENAME
    )
    baseline_created = False
    if baseline_file.exists():
        try:
            baseline = load_signed_baseline(baseline_file, trust)
        except (OSError, ValueError) as exc:
            # A baseline the detector cannot trust is a declared refusal, never a
            # comparison: the detector would otherwise report ordinariness against
            # a profile nobody verified.
            refuse_drift(
                ledger,
                stage="check",
                reason_code="baseline_not_accepted",
                reason=str(exc),
                produced_by=produced_by,
                issued_at=moment.isoformat(),
            )
    else:
        baseline = build_baseline(
            cycles[:learning_cycles],
            authority_private_key=authority_private_key,
            authority_public_key=authority_public_key,
            produced_by=produced_by,
            issued_at=moment.isoformat(),
        )
        write_baseline(baseline, baseline_file)
        baseline_created = True

    profile = baseline.payload
    if len(cycles) <= learning_cycles:
        # The learning window is still the whole history: comparing it against
        # itself would only report a tautology. The next emission is the first
        # new behaviour worth judging.
        _write_state(
            stage,
            baseline_id=profile.baseline_id,
            baseline_version=profile.baseline_version,
            window_sha256=_window_digest(cycles),
            status=DriftStatus.clear,
            signal_id=None,
            checked_at=moment.isoformat(),
        )
        return DriftWatchReport(
            checked_at=moment.isoformat(),
            # A pre-existing baseline over a history still inside its learning
            # window is not a creation: declare the short window instead.
            outcome=(
                DriftWatchOutcome.baseline_created
                if baseline_created
                else DriftWatchOutcome.insufficient_window
            ),
            cycles_seen=len(history),
            baseline_id=profile.baseline_id,
            baseline_version=profile.baseline_version,
            baseline_created=baseline_created,
            window_cycle_ids=[cycle.cycle_id for cycle in cycles],
            excluded_response_cycles=excluded_response_cycles,
            detail=(
                f"baseline anchored on {learning_cycles} cycle(s); "
                "the next emission starts the comparison window"
                if baseline_created
                else (
                    f"the history still holds only the {len(cycles)} learning cycle(s); "
                    "the next emission starts the comparison window"
                )
            ),
        )

    window = cycles[-window_cycles:]
    window_sha256 = _window_digest(window)
    state = load_drift_watch_state(stage)
    if (
        state is not None
        and state.baseline_id == profile.baseline_id
        and state.last_window_sha256 == window_sha256
    ):
        return DriftWatchReport(
            checked_at=moment.isoformat(),
            outcome=DriftWatchOutcome.unchanged_window,
            cycles_seen=len(history),
            baseline_id=profile.baseline_id,
            baseline_version=profile.baseline_version,
            baseline_created=baseline_created,
            window_cycle_ids=[cycle.cycle_id for cycle in window],
            excluded_response_cycles=excluded_response_cycles,
            detail="this window was already compared against the same baseline",
        )

    if ledger is not None and _has_recorded_signal(
        ledger, baseline_id=profile.baseline_id, window_sha256=window_sha256
    ):
        _write_state(
            stage,
            baseline_id=profile.baseline_id,
            baseline_version=profile.baseline_version,
            window_sha256=window_sha256,
            status=DriftStatus.drift,
            signal_id=None,
            checked_at=moment.isoformat(),
        )
        return DriftWatchReport(
            checked_at=moment.isoformat(),
            outcome=DriftWatchOutcome.unchanged_window,
            cycles_seen=len(history),
            baseline_id=profile.baseline_id,
            baseline_version=profile.baseline_version,
            baseline_created=baseline_created,
            window_cycle_ids=[cycle.cycle_id for cycle in window],
            excluded_response_cycles=excluded_response_cycles,
            detail="this window already has a recorded signal in the ledger",
        )

    signal = detect_drift(
        baseline,
        window,
        trust_key_path=trust,
        tolerances=tolerances,
        produced_by=produced_by,
        issued_at=moment.isoformat(),
        latest_baseline_id=latest_baseline_id,
        ledger=ledger,
    )
    _write_state(
        stage,
        baseline_id=profile.baseline_id,
        baseline_version=profile.baseline_version,
        window_sha256=window_sha256,
        status=signal.status,
        signal_id=signal.signal_id if signal.status is not DriftStatus.clear else None,
        checked_at=moment.isoformat(),
    )
    return DriftWatchReport(
        checked_at=moment.isoformat(),
        outcome=DriftWatchOutcome(signal.status.value),
        cycles_seen=len(history),
        baseline_id=profile.baseline_id,
        baseline_version=profile.baseline_version,
        baseline_created=baseline_created,
        window_cycle_ids=[cycle.cycle_id for cycle in window],
        signal=signal,
        excluded_response_cycles=excluded_response_cycles,
        detail=(
            "no signal recorded: the window is inside the declared tolerances"
            if signal.status is DriftStatus.clear
            else "; ".join(signal.reasons)
        ),
    )


def respond_to_watch(
    report: DriftWatchReport,
    *,
    history_dir: str | Path,
    ledger: "AuditLedger",
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    contract_path: str | Path | None = None,
    trust_key_path: str | Path | None = None,
    sensor_trust_key_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    produced_by: str = "integrity-drift-response",
    issued_at: str | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    snapshot_ttl_seconds: float = EVALUATION_SNAPSHOT_TTL_SECONDS,
    budget: "BudgetPolicy | None" = None,
) -> "DriftResponse | DriftCoalescence | None":
    """Close the circuit: a recorded ``drift`` tick triggers the deep run.

    Pure composition, no new policy. Everything that decides whether a run
    happens stays where it already lives: this function only hands the
    signal, the window's signed trajectory and its last cycle to
    :func:`autopilot.drift_response.run_drift_response`, which re-checks that
    the signal is recorded verbatim, folds the same episode into the existing
    response (no run spent) and refuses fail-closed when the declared budget
    is spent — after declaring the refusal.

    Returns ``None`` when the tick did not record a ``drift`` signal: a clear
    window, an unchanged window, an anchored baseline or an insufficient
    history are all answered with "no run", not with an evaluation.

    The contract comes from the triggering cycle (``contract_source``) unless
    one is passed explicitly, so the run evaluates the contract that produced
    the signal. The output directory is the history directory unless given:
    that is where the guard reads ``guard-snapshot.json``, so a run always
    re-emits the snapshot in place.
    """

    if report.outcome is not DriftWatchOutcome.drift or report.signal is None:
        return None
    if not report.window_cycle_ids:
        raise ValueError("a drift report without a compared window cannot be answered")
    return _answer_signal(
        report.signal,
        history_dir=history_dir,
        ledger=ledger,
        producer_private_key=producer_private_key,
        producer_public_key=producer_public_key,
        contract_path=contract_path,
        trust_key_path=trust_key_path,
        sensor_trust_key_path=sensor_trust_key_path,
        output_dir=output_dir,
        produced_by=produced_by,
        issued_at=issued_at,
        interval_seconds=interval_seconds,
        snapshot_ttl_seconds=snapshot_ttl_seconds,
        budget=budget,
    )


def _answer_signal(
    signal: DriftSignal,
    *,
    history_dir: str | Path,
    ledger: "AuditLedger",
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    contract_path: str | Path | None,
    trust_key_path: str | Path | None,
    sensor_trust_key_path: str | Path | None,
    output_dir: str | Path | None,
    produced_by: str,
    issued_at: str | None,
    interval_seconds: float,
    snapshot_ttl_seconds: float,
    budget: "BudgetPolicy | None",
) -> "DriftResponse | DriftCoalescence":
    """Answer one recorded signal by continuing the window that triggered it."""

    from autopilot.drift_response import run_drift_response

    if not signal.window_cycle_ids:
        raise ValueError(f"signal {signal.signal_id} carries no window to answer")
    stage = Path(history_dir)
    reactions = _response_cycle_ids(ledger)
    trigger = None
    for cycle in load_cycle_history(stage):
        if cycle.cycle_id in reactions:
            continue
        if cycle.cycle_id == signal.window_cycle_ids[-1]:
            trigger = cycle
    if trigger is None:
        # Declared, not silent: the signal points at a window this node never
        # observed (a ledger shared with another output directory, a pruned
        # history). Recording the refusal also stops it from being retried as
        # pending on every tick.
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="continuity_broken",
            reason=(
                f"the signal's window ends at cycle {signal.window_cycle_ids[-1]}, which is "
                f"not in the history at {stage}; refusing to answer a window this node "
                "never observed"
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            signal=signal,
        )

    return run_drift_response(
        signal,
        ledger=ledger,
        trajectory_path=Path(trigger.trajectory_source),
        contract_path=contract_path if contract_path is not None else trigger.contract_source,
        producer_private_key=producer_private_key,
        producer_public_key=producer_public_key,
        trust_key_path=trust_key_path if trust_key_path is not None else producer_public_key,
        sensor_trust_key_path=sensor_trust_key_path,
        output_dir=output_dir if output_dir is not None else stage,
        produced_by=produced_by,
        issued_at=issued_at,
        previous=trigger,
        interval_seconds=interval_seconds,
        snapshot_ttl_seconds=snapshot_ttl_seconds,
        budget=budget,
    )


def _signal_records(
    ledger: "AuditLedger",
) -> tuple[list[DriftSignal], set[str], set[str]]:
    """Read the ledger once and classify every recorded signal.

    Returns ``(signals, covered, declined)``: signals in the order recorded,
    ids answered by a run or folded into one, and ids a refusal answered.
    """

    from autopilot.audit import AuditEventType  # imported lazily: audit imports the drift modules

    recorded: list[DriftSignal] = []
    covered: set[str] = set()
    declined: set[str] = set()
    for event in ledger.events():
        payload = event.payload if isinstance(event.payload, dict) else {}
        signal_id = payload.get("signal_id")
        if event.event_type is AuditEventType.drift_signal:
            try:
                signal = DriftSignal.model_validate(payload)
            except Exception as exc:
                raise ValueError(
                    f"ledger holds an unreadable drift signal {event.subject_id}: {exc}"
                ) from exc
            recorded.append(signal)
        elif event.event_type in (
            AuditEventType.drift_response,
            AuditEventType.drift_coalescence,
        ):
            if isinstance(signal_id, str):
                covered.add(signal_id)
        elif event.event_type is AuditEventType.drift_budget_refusal:
            if isinstance(signal_id, str):
                declined.add(signal_id)
        elif event.event_type is AuditEventType.drift_refusal:
            if payload.get("stage") == "respond" and isinstance(signal_id, str):
                declined.add(signal_id)
    return recorded, covered, declined


def pending_signals(ledger: "AuditLedger") -> list[DriftSignal]:
    """Recorded ``drift`` signals that no respond-stage record has answered.

    The gap this closes is the one between the watch appending ``drift_signal``
    and the response appending its outcome: a process killed in between left a
    real signal that nothing ever answered, and no later tick looked back at it.

    A signal counts as answered by *any* respond-stage record, including the
    ones that declined it: ``drift_response`` (a run happened), ``drift_coalescence``
    (the episode was already covered), ``drift_budget_refusal`` (the declared
    cost control said no) and ``drift_refusal`` with ``stage=respond`` (the run
    was refused for a named reason). Retrying a declared answer would turn a
    decision into a loop, so only signals with none of those are pending —
    which is exactly the crash window, and nothing else.

    ``clear`` and ``indeterminate`` signals are never pending: only ``drift``
    triggers a run, and an indeterminate declaration is for human review.
    """

    recorded, covered, declined = _signal_records(ledger)
    return [
        signal
        for signal in recorded
        if signal.status is DriftStatus.drift
        and signal.signal_id not in covered
        and signal.signal_id not in declined
    ]


def declined_signals(ledger: "AuditLedger") -> list[DriftSignal]:
    """Recorded ``drift`` signals a refusal answered instead of a run.

    These are the recovery *gaps*: the circuit was supposed to close with a
    fresh snapshot and closed with a declared decision instead (budget spent,
    window this node never saw, engine run refused). The episode stays covered
    only by whatever snapshot was already valid, so an operator has to look —
    which is exactly why the health line counts them.
    """

    recorded, covered, declined = _signal_records(ledger)
    return [
        signal
        for signal in recorded
        if signal.status is DriftStatus.drift
        and signal.signal_id in declined
        and signal.signal_id not in covered
    ]


def recover_pending(
    *,
    history_dir: str | Path,
    ledger: "AuditLedger",
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    contract_path: str | Path | None = None,
    trust_key_path: str | Path | None = None,
    sensor_trust_key_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    produced_by: str = "integrity-drift-response",
    issued_at: str | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    snapshot_ttl_seconds: float = EVALUATION_SNAPSHOT_TTL_SECONDS,
    budget: "BudgetPolicy | None" = None,
    limit: int = 1,
) -> list["DriftResponse | DriftCoalescence"]:
    """Answer signals a previous tick recorded but never got to answer.

    Runs before the tick emits anything: an unanswered signal is evidence that
    the guard is still judging against an old snapshot, so it is settled before
    more evidence is piled on top.

    ``limit`` (one by default) bounds the work per tick: a backlog is answered
    one tick at a time, and the budget/coalescence rules still decide each one.
    A refusal propagates after being declared and the signal stops being
    pending, so a transient failure is not retried in a loop — it becomes an
    operator decision with a ``drift_refusal`` record behind it.
    """

    if limit < 1:
        raise ValueError("limit must be at least 1")
    answers: list["DriftResponse | DriftCoalescence"] = []
    for signal in pending_signals(ledger)[:limit]:
        answers.append(
            _answer_signal(
                signal,
                history_dir=history_dir,
                ledger=ledger,
                producer_private_key=producer_private_key,
                producer_public_key=producer_public_key,
                contract_path=contract_path,
                trust_key_path=trust_key_path,
                sensor_trust_key_path=sensor_trust_key_path,
                output_dir=output_dir,
                produced_by=produced_by,
                issued_at=issued_at,
                interval_seconds=interval_seconds,
                snapshot_ttl_seconds=snapshot_ttl_seconds,
                budget=budget,
            )
        )
    return answers
