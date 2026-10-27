"""Drift-triggered deep evaluation v0.1 (System 1 — OP-3 → OP-5 wiring).

The design packet leaves one wire unplugged: the drift signal (OP-3) only
fires and is recorded; nothing consumes it. This module is that consumer —
and nothing more:

    recorded drift signal → run the monitoring loop over the window's signed
    trajectory → deterministic evaluation → fresh signed snapshot with TTL →
    ledger response record linking signal and cycle.

Guarantees:

- **Only a recorded drift signal triggers a run.** ``clear`` is not a trigger;
  ``indeterminate`` is not a trigger (it is a declaration for human review);
  and a signal that is not present in the append-only ledger with the same
  payload is refused — the causal chain lives in durable evidence, not in a
  JSON file someone hands over.
- **One run per signal, and a declared budget per contract (P-07).** A second
  response for the same signal is refused; a signal that is the same episode
  as an already recorded run is *coalesced* into it (no run spent); and the
  declared cost-control policy (by default 3 recorded deep runs per sliding
  30-day window, per contract) caps what is left. A signal the budget cannot
  afford is refused with a declared ``drift_budget_refusal`` and
  ``DriftBudgetExhausted`` is raised after the declaration — fail-closed:
  no fresh snapshot, so the previous snapshot remains the only authority
  until its TTL expires (I-2). Coalescence is checked before the budget on
  purpose: folding a duplicate signal must not consume a run.
- **The engine decides; the response only reports.** The run is
  :func:`autopilot.monitoring_loop.run_monitoring_cycle`: sign trajectory →
  verify against the trust key → deterministic contract evaluation → signed
  snapshot with TTL. The autonomy values in the response are a copy of the
  engine's output; the authority is the signed snapshot, which can only
  tighten. This module does not import the enforcement adapter and cannot
  relax anything (invariant I-1).
- **Continuity with the triggering window.** When a run happens and the
  previous cycle is supplied, it must be the last cycle of the signal's
  window; coherence beats convenience.
- **Fail-closed, and declared:** wrong status, unrecorded signal, duplicate
  response, contract mismatch, broken continuity, unreadable cost-control
  state or a refused engine run raise after the refusal has been declared in
  the append-only ledger as a ``drift_refusal`` record (observation, no
  authority), so the evidence carries the decisions that produced no run —
  not only the ones that did. Cost-control refusals keep their own records
  (``drift_coalescence`` / ``drift_budget_refusal``).

v0.1 honesty: the "window's signed trajectory" is the source trajectory of the
latest emission (the artifact on disk). A full replay of every event in the
window needs OP-1/OP-4 telemetry, which is not built.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from autopilot.contracts import DSLValidationError, load_contract
from autopilot.drift import (
    DriftSignal,
    DriftStatus,
    _canonical_bytes,
    _parse_moment,
    refuse_drift,
)
from autopilot.drift_budget import (
    BudgetPolicy,
    DriftBudgetExhausted,
    DriftCoalescence,
    budget_state,
    build_coalescence,
    build_refusal,
    coalescence_target,
)
from autopilot.monitoring_loop import (
    DEFAULT_INTERVAL_SECONDS,
    EVALUATION_SNAPSHOT_TTL_SECONDS,
    CycleChange,
    MonitoringCycle,
    run_monitoring_cycle,
)

if TYPE_CHECKING:  # pragma: no cover - audit imports this module (observation record)
    from autopilot.audit import AuditLedger

RESPONSE_SCHEMA_VERSION = "0.1"


class DriftResponse(BaseModel):
    """Observation record linking a drift signal to its triggered evaluation run.

    The autonomy fields are the deterministic engine's reported output, carried
    authoritatively by the signed snapshot the loop emits; this record itself
    has no authority and is validated for structure, not for a signature.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = RESPONSE_SCHEMA_VERSION
    kind: Literal["drift_response"] = "drift_response"
    response_id: str
    produced_by: str
    issued_at: str
    signal_id: str
    baseline_id: str
    baseline_version: int
    contract_id: str
    contract_version: str
    trajectory_id: str
    window_cycle_ids: list[str]
    triggered_cycle_id: str
    change: CycleChange
    evaluation_status: str
    recommended_autonomy: str
    block_release: bool
    snapshot_id: str
    snapshot_expires_at: str
    fingerprint: str


def _existing_response(ledger: AuditLedger, signal_id: str) -> str | None:
    """Return the id of a recorded response for ``signal_id``, if any."""

    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    for event in ledger.events():
        if event.event_type is not AuditEventType.drift_response:
            continue
        if isinstance(event.payload, dict) and event.payload.get("signal_id") == signal_id:
            return event.subject_id
    return None


def _existing_coalescence(ledger: AuditLedger, signal_id: str) -> str | None:
    """Return the response a recorded coalescence folds ``signal_id`` into, if any."""

    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    for event in ledger.events():
        if event.event_type is not AuditEventType.drift_coalescence:
            continue
        payload = event.payload
        if isinstance(payload, dict) and payload.get("signal_id") == signal_id:
            response_id = payload.get("response_id")
            if isinstance(response_id, str) and response_id:
                return response_id
            return event.subject_id
    return None


def _recorded_signal(ledger: AuditLedger, signal: DriftSignal) -> bool:
    """True when this exact signal (id and payload) is in the ledger."""

    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    for event in ledger.events():
        if event.event_type is not AuditEventType.drift_signal:
            continue
        if event.subject_id != signal.signal_id:
            continue
        try:
            return DriftSignal.model_validate(event.payload) == signal
        except Exception:
            return False
    return False


def run_drift_response(
    signal: DriftSignal,
    *,
    ledger: AuditLedger,
    trajectory_path: str | Path,
    contract_path: str | Path,
    producer_private_key: str | Path,
    producer_public_key: str | Path,
    trust_key_path: str | Path,
    output_dir: str | Path,
    sensor_trust_key_path: str | Path | None = None,
    produced_by: str = "integrity-drift-response",
    issued_at: str | None = None,
    previous: MonitoringCycle | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    snapshot_ttl_seconds: float = EVALUATION_SNAPSHOT_TTL_SECONDS,
    budget: BudgetPolicy | None = None,
) -> DriftResponse | DriftCoalescence:
    """Trigger one deterministic evaluation run from a recorded drift signal.

    Runs the full evidence path over the window's signed trajectory, records
    the loop's cycle and then one ``drift_response`` event tying the signal to
    the run. The engine's recommendation is reported verbatim; this function
    never chooses an autonomy level.

    Cost control (P-07) is applied before anything is emitted, in order: a
    signal that is the same episode as an already recorded response is folded
    into it (``DriftCoalescence``, no run); otherwise the declared ``budget``
    decides, and a spent window refuses the signal — the refusal is recorded
    and ``DriftBudgetExhausted`` is raised after the declaration.
    """

    policy = budget if budget is not None else BudgetPolicy()
    if signal.status is not DriftStatus.drift:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="signal_not_drift",
            reason=(
                "only a recorded drift signal triggers an evaluation; this signal is "
                f"{signal.status.value}"
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            signal=signal,
        )
    existing = _existing_response(ledger, signal.signal_id)
    if existing is not None:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="signal_already_answered",
            reason=(
                f"signal {signal.signal_id} already has a recorded response ({existing}); "
                "refusing a duplicate run"
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            signal=signal,
        )
    if not _recorded_signal(ledger, signal):
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="signal_not_recorded",
            reason=(
                f"refusing to act on signal {signal.signal_id}: it is not recorded in this "
                "ledger with the same payload"
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            signal=signal,
        )
    try:
        document = load_contract(contract_path)
    except DSLValidationError as exc:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="contract_invalid",
            reason=f"contract is not valid: {exc}",
            produced_by=produced_by,
            issued_at=issued_at,
            signal=signal,
        )
    if document.contract.id != signal.contract_id:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="contract_mismatch",
            reason=(
                f"contract {document.contract.id} does not match the signal's contract "
                f"{signal.contract_id}; refusing to evaluate a different contract"
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            signal=signal,
        )
    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    moment = _parse_moment(issued_at)
    issued_iso = moment.isoformat()
    try:
        target = coalescence_target(
            ledger,
            signal=signal,
            contract_id=document.contract.id,
            contract_version=document.contract.version,
        )
    except ValueError as exc:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="cost_control_unreadable",
            reason=str(exc),
            produced_by=produced_by,
            issued_at=issued_iso,
            signal=signal,
        )
    if target is not None:
        recorded = _existing_coalescence(ledger, signal.signal_id)
        if recorded is not None:
            refuse_drift(
                ledger,
                stage="respond",
                reason_code="coalescence_already_declared",
                reason=(
                    f"signal {signal.signal_id} was already coalesced into response "
                    f"{recorded}; refusing a duplicate declaration"
                ),
                produced_by=produced_by,
                issued_at=issued_iso,
                signal=signal,
            )
        coalescence = build_coalescence(
            signal=signal, target=target, produced_by=produced_by, issued_at=issued_iso
        )
        ledger.append(
            AuditEventType.drift_coalescence,
            coalescence.coalescence_id,
            issued_iso,
            coalescence.model_dump(mode="json"),
        )
        return coalescence

    try:
        state = budget_state(
            ledger, contract_id=document.contract.id, now=moment, policy=policy
        )
    except ValueError as exc:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="cost_control_unreadable",
            reason=str(exc),
            produced_by=produced_by,
            issued_at=issued_iso,
            signal=signal,
        )
    if state.exhausted:
        refusal = build_refusal(
            signal=signal,
            produced_by=produced_by,
            issued_at=issued_iso,
            policy=policy,
            state=state,
        )
        ledger.append(
            AuditEventType.drift_budget_refusal,
            refusal.refusal_id,
            issued_iso,
            refusal.model_dump(mode="json"),
        )
        release = (
            f"; the window releases a slot at {refusal.earliest_release_at}"
            if refusal.earliest_release_at
            else ""
        )
        raise DriftBudgetExhausted(f"budget exhausted: {refusal.reason}{release}", refusal=refusal)

    if previous is not None and signal.window_cycle_ids:
        if previous.cycle_id != signal.window_cycle_ids[-1]:
            refuse_drift(
                ledger,
                stage="respond",
                reason_code="continuity_broken",
                reason=(
                    f"previous cycle {previous.cycle_id} is not the last cycle of the "
                    f"signal window ({signal.window_cycle_ids[-1]}); the response must "
                    "continue the window that triggered it"
                ),
                produced_by=produced_by,
                issued_at=issued_iso,
                signal=signal,
            )

    try:
        cycle = run_monitoring_cycle(
            trajectory_path=trajectory_path,
            contract_path=contract_path,
            producer_private_key=producer_private_key,
            producer_public_key=producer_public_key,
            trust_key_path=trust_key_path,
            output_dir=output_dir,
            sensor_trust_key_path=sensor_trust_key_path,
            produced_by=produced_by,
            interval_seconds=interval_seconds,
            snapshot_ttl_seconds=snapshot_ttl_seconds,
            issued_at=issued_iso,
            previous=previous,
            ledger=ledger,
        )
    except (OSError, ValueError) as exc:
        refuse_drift(
            ledger,
            stage="respond",
            reason_code="run_refused",
            reason=str(exc),
            produced_by=produced_by,
            issued_at=issued_iso,
            signal=signal,
        )
    response_id = "RESPONSE-" + hashlib.sha256(
        _canonical_bytes(
            {
                "signal_id": signal.signal_id,
                "triggered_cycle_id": cycle.cycle_id,
                "produced_by": produced_by,
                "issued_at": cycle.issued_at,
            }
        )
    ).hexdigest()[:24].upper()
    response = DriftResponse(
        response_id=response_id,
        produced_by=produced_by,
        issued_at=cycle.issued_at,
        signal_id=signal.signal_id,
        baseline_id=signal.baseline_id,
        baseline_version=signal.baseline_version,
        contract_id=cycle.contract_id,
        contract_version=cycle.contract_version,
        trajectory_id=cycle.trajectory_id,
        window_cycle_ids=list(signal.window_cycle_ids),
        triggered_cycle_id=cycle.cycle_id,
        change=cycle.change,
        evaluation_status=cycle.evaluation_status,
        recommended_autonomy=cycle.recommended_autonomy,
        block_release=cycle.block_release,
        snapshot_id=cycle.snapshot_id,
        snapshot_expires_at=cycle.snapshot_expires_at,
        fingerprint=cycle.fingerprint,
    )
    ledger.append(
        AuditEventType.drift_response,
        response.response_id,
        cycle.issued_at,
        response.model_dump(mode="json"),
    )
    return response
