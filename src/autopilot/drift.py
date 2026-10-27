"""Behavioural drift detector v0.1 (System 1 — OP-2/OP-3 of the design packet).

The packet designs OP-2 (build/version a signed baseline) and OP-3 (compare a
window against the live baseline; emit a drift signal — or nothing — to the
ledger). OP-1 (production telemetry) and OP-4 (signed trajectories from events)
are not built, so v0.1 is explicit about its source: the windows are the
``MonitoringCycle`` records emitted by the monitoring loop
(``monitoring_loop.py``). This detects drift of the *evaluation-state stream*,
not of production behaviour; when OP-1/OP-4 land, the window source changes and
these mechanics do not.

Guarantees, per the packet's invariants:

- **I-1 (single direction):** this module has no path to autonomy. It does not
  import the enforcement adapter, does not build snapshots and carries no
  autonomy field; its only outputs are a ``DriftSignal`` and a
  ``drift_signal`` ledger event.
- **I-3 (drift ≠ failure):** a signal fires and is recorded; the deterministic
  evaluator remains the only classifier of failure.
- **Fail-closed:** the detector never compares against an unverified profile —
  unsigned, tampered, foreign-key or superseded baselines raise ``ValueError``.
  An uncertain window (gap, duplicate, contract mismatch) is declared
  ``indeterminate`` and recorded as such; it is never silently treated as clear.
- **T-04:** the baseline is versioned, anchored by a sha256 digest of its
  complete learning window and signed by the baseline authority; rebuilding
  over an existing baseline requires a named human approval or is refused.
- **Window completeness (known v0.1 limit):** windows are validated by unique
  ids, ordering, single-contract and fingerprint chaining. A gap *inside* a run
  of identical fingerprints is invisible to the chain (each cycle only carries
  the previous fingerprint), so declared per-window coverage — OP-1 territory —
  remains unbuilt; ``window_sha256`` freezes exactly the cycles the baseline saw.

The signing primitives are the guard's shared Ed25519 scheme (the same one the
ledger and the trajectory envelopes use), imported from ``enforcement.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

# Shared Ed25519 primitives (same scheme as the ledger, trajectories and
# snapshots). They are internal to the package on purpose: one signing recipe.
from autopilot.enforcement import (
    GuardSignature,
    _embedded_key_matches_trust_key,
    _sign_guard_model,
    _verify_signed_receipt,
)
from autopilot.monitoring_loop import MonitoringCycle, load_previous_cycle

if TYPE_CHECKING:  # pragma: no cover - audit imports this module (observation record)
    from autopilot.audit import AuditLedger

BASELINE_SCHEMA_VERSION = "0.1"
SIGNAL_SCHEMA_VERSION = "0.1"
REFUSAL_SCHEMA_VERSION = "0.1"
BASELINE_LABEL = "baseline"

# Defaults are declared tolerances (OP-3), not hidden constants: every signal
# carries the tolerances it was measured against.
DEFAULT_MAX_STATUS_SHIFT = 0.34
DEFAULT_MAX_AUTONOMY_SHIFT = 0.34
DEFAULT_MAX_CHANGE_SHIFT = 0.34


class DriftStatus(str, Enum):
    """Outcome of one comparison (I-3: only ``drift`` triggers deeper work)."""

    clear = "clear"
    drift = "drift"
    indeterminate = "indeterminate"


class DriftTolerances(BaseModel):
    """Declared tolerances for one comparison (OP-3 precondition)."""

    model_config = ConfigDict(extra="forbid")

    max_status_shift: float = Field(default=DEFAULT_MAX_STATUS_SHIFT, ge=0.0, le=1.0)
    max_autonomy_shift: float = Field(default=DEFAULT_MAX_AUTONOMY_SHIFT, ge=0.0, le=1.0)
    max_change_shift: float = Field(default=DEFAULT_MAX_CHANGE_SHIFT, ge=0.0, le=1.0)
    # A category never seen in the learning window is drift by declaration;
    # set False to tolerate unseen categories explicitly.
    flag_new_categories: bool = True


class BaselineProfile(BaseModel):
    """The signed payload: a frozen learning window and its distributions."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = BASELINE_SCHEMA_VERSION
    baseline_id: str
    baseline_version: int = Field(ge=1)
    produced_by: str
    issued_at: str
    supersedes_baseline_id: str | None = None
    approved_by: str | None = None
    contract_id: str
    contract_version: str
    trajectory_ids: list[str]
    window_cycle_ids: list[str]
    window_cycles: int = Field(ge=1)
    window_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status_counts: dict[str, int]
    autonomy_counts: dict[str, int]
    change_counts: dict[str, int]
    status_rates: dict[str, float]
    autonomy_rates: dict[str, float]
    change_rates: dict[str, float]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class SignedBaseline(BaseModel):
    """Ed25519-signed envelope around one versioned baseline."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = BASELINE_SCHEMA_VERSION
    baseline_id: str
    payload: BaselineProfile
    signature: GuardSignature


class BaselineVerification(BaseModel):
    """Result of verifying a signed baseline envelope."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    key_matches: bool
    digest_matches: bool
    signature_valid: bool
    trusted: bool
    problems: list[str]


class DistributionShift(BaseModel):
    """Total-variation distance of one distribution against the baseline."""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    distance: float
    tolerance: float
    exceeded: bool


class DriftSignal(BaseModel):
    """One OP-3 observation record: what was compared and what was seen.

    Deliberately carries no autonomy level, no block_release and no snapshot
    reference: it can only fire and be recorded (I-1, I-3).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = SIGNAL_SCHEMA_VERSION
    kind: Literal["drift_signal"] = "drift_signal"
    signal_id: str
    produced_by: str
    issued_at: str
    status: DriftStatus
    baseline_id: str
    baseline_version: int
    baseline_fingerprint: str
    contract_id: str
    window_cycle_ids: list[str]
    window_cycles: int
    window_sha256: str | None
    shifts: list[DistributionShift]
    new_categories: list[str]
    reasons: list[str]
    tolerances: DriftTolerances


DriftRefusalReason = Literal[
    "baseline_unverified",
    "baseline_superseded",
    "baseline_not_accepted",
    "window_not_accepted",
    "signal_not_accepted",
    "signal_not_drift",
    "signal_already_answered",
    "coalescence_already_declared",
    "signal_not_recorded",
    "contract_invalid",
    "contract_mismatch",
    "continuity_broken",
    "previous_cycle_invalid",
    "cost_control_unreadable",
    "run_refused",
]


class DriftRefusal(BaseModel):
    """One declared fail-closed refusal of the drift path (observation record).

    Every refusal the pipeline makes over a verified object — a signed
    baseline (stage ``check``) or a recorded signal (stage ``respond``) — is
    declared here before it propagates, so the ledger carries the decisions
    that produced no signal, no run and no snapshot, not only the ones that
    did. Failures that happen before an object exists (an input file that does
    not load) are declared by the boundary that reports them. No authority:
    there is no autonomy field, by construction (I-1).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = REFUSAL_SCHEMA_VERSION
    kind: Literal["drift_refusal"] = "drift_refusal"
    refusal_id: str
    produced_by: str
    issued_at: str
    stage: Literal["check", "respond"]
    reason_code: DriftRefusalReason
    reason: str = Field(min_length=1, max_length=4000)
    signal_id: str | None = None
    baseline_id: str | None = None
    baseline_version: int | None = None
    contract_id: str | None = None


class DriftRefusalError(ValueError):
    """Fail-closed refusal raised after the refusal was declared in the ledger."""

    def __init__(self, message: str, *, refusal: DriftRefusal | None) -> None:
        super().__init__(message)
        self.refusal = refusal


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _atomic_write(path: Path, document: str) -> str:
    """Write ``document`` to ``path`` atomically (temp file + ``os.replace``)."""

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
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _parse_moment(issued_at: str | None, field_name: str = "issued_at") -> datetime:
    if issued_at is None:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return moment


def _cycle_moment(cycle: MonitoringCycle) -> datetime:
    try:
        return datetime.fromisoformat(cycle.issued_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"cycle {cycle.cycle_id} has an unparseable issued_at") from exc


def _window_digest(window: Sequence[MonitoringCycle]) -> str:
    """Anchor digest of the learning window: identity fields only, no clocks."""

    body = [
        {
            "cycle_id": cycle.cycle_id,
            "fingerprint": cycle.fingerprint,
            "previous_fingerprint": cycle.previous_fingerprint,
            "issued_at": cycle.issued_at,
            "status": cycle.evaluation_status,
            "recommended_autonomy": cycle.recommended_autonomy,
            "block_release": cycle.block_release,
        }
        for cycle in window
    ]
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _window_problems(window: Sequence[MonitoringCycle]) -> list[str]:
    """Structural problems of a candidate window (gaps, duplicates, mixes)."""

    problems: list[str] = []
    if not window:
        return ["window is empty"]
    seen: set[str] = set()
    previous_moment: datetime | None = None
    for index, cycle in enumerate(window):
        if cycle.cycle_id in seen:
            problems.append(f"cycle {cycle.cycle_id} appears more than once")
        seen.add(cycle.cycle_id)
        moment = _cycle_moment(cycle)
        if previous_moment is not None and moment < previous_moment:
            problems.append(f"cycle {cycle.cycle_id} is not ordered after the previous cycle")
        previous_moment = moment
        if index > 0 and cycle.previous_fingerprint != window[index - 1].fingerprint:
            problems.append(
                f"window is not contiguous: cycle {cycle.cycle_id} does not link to the "
                "previous cycle fingerprint"
            )
    contract_ids = {cycle.contract_id for cycle in window}
    if len(contract_ids) > 1:
        problems.append("window mixes contract ids: " + ", ".join(sorted(contract_ids)))
    contract_versions = {cycle.contract_version for cycle in window}
    if len(contract_versions) > 1:
        problems.append("window mixes contract versions: " + ", ".join(sorted(contract_versions)))
    return problems


def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _rates(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {}
    return {key: round(value / total, 6) for key, value in counts.items()}


def _total_variation(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    return round(0.5 * sum(abs(left.get(key, 0.0) - right.get(key, 0.0)) for key in keys), 6)


def _window_distributions(window: Sequence[MonitoringCycle]) -> tuple[
    dict[str, int], dict[str, int], dict[str, int]
]:
    status_counts = _counts([cycle.evaluation_status for cycle in window])
    autonomy_counts = _counts([cycle.recommended_autonomy for cycle in window])
    change_counts = _counts([cycle.change.value for cycle in window])
    return status_counts, autonomy_counts, change_counts


def _baseline_fingerprint(
    *,
    contract_id: str,
    contract_version: str,
    window_sha256: str,
    status_counts: dict[str, int],
    autonomy_counts: dict[str, int],
    change_counts: dict[str, int],
) -> str:
    body = {
        "contract_id": contract_id,
        "contract_version": contract_version,
        "window_sha256": window_sha256,
        "status_counts": status_counts,
        "autonomy_counts": autonomy_counts,
        "change_counts": change_counts,
    }
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


# ---------------------------------------------------------------------------
# OP-2 — build, sign and version the baseline
# ---------------------------------------------------------------------------


def build_baseline(
    window: Sequence[MonitoringCycle],
    *,
    authority_private_key: str | Path,
    authority_public_key: str | Path,
    produced_by: str = "integrity-drift-baseline",
    issued_at: str | None = None,
    previous: SignedBaseline | None = None,
    approved_by: str | None = None,
) -> SignedBaseline:
    """Build a signed, versioned baseline from a complete learning window.

    OP-2 preconditions enforced here: the window must be non-empty, ordered,
    contiguous and single-contract (``ValueError`` otherwise — a partial
    window cannot anchor a profile). Re-baselining over ``previous`` requires
    a named ``approved_by`` (T-04) and bumps ``baseline_version``.
    """

    problems = _window_problems(window)
    if problems:
        raise ValueError("baseline requires a complete learning window: " + "; ".join(problems))
    moment = _parse_moment(issued_at)
    if previous is not None:
        if not approved_by or not approved_by.strip():
            raise ValueError(
                "re-baseline over an existing baseline requires explicit human approval "
                "(approved_by), per threat T-04"
            )
        if moment <= max(_cycle_moment(cycle) for cycle in window):
            raise ValueError("re-baseline issued_at must be after the learning window")
        version = previous.payload.baseline_version + 1
        supersedes = previous.payload.baseline_id
    else:
        version = 1
        supersedes = None
    contract_id = window[0].contract_id
    contract_version = window[0].contract_version
    window_sha256 = _window_digest(window)
    status_counts, autonomy_counts, change_counts = _window_distributions(window)
    fingerprint = _baseline_fingerprint(
        contract_id=contract_id,
        contract_version=contract_version,
        window_sha256=window_sha256,
        status_counts=status_counts,
        autonomy_counts=autonomy_counts,
        change_counts=change_counts,
    )
    baseline_id = "BASELINE-" + hashlib.sha256(
        _canonical_bytes(
            {
                "contract_id": contract_id,
                "contract_version": contract_version,
                "window_sha256": window_sha256,
                "baseline_version": version,
                "issued_at": moment.isoformat(),
                "produced_by": produced_by,
            }
        )
    ).hexdigest()[:24].upper()
    payload = BaselineProfile(
        baseline_id=baseline_id,
        baseline_version=version,
        produced_by=produced_by,
        issued_at=moment.isoformat(),
        supersedes_baseline_id=supersedes,
        approved_by=approved_by if approved_by else None,
        contract_id=contract_id,
        contract_version=contract_version,
        trajectory_ids=sorted({cycle.trajectory_id for cycle in window}),
        window_cycle_ids=[cycle.cycle_id for cycle in window],
        window_cycles=len(window),
        window_sha256=window_sha256,
        status_counts=status_counts,
        autonomy_counts=autonomy_counts,
        change_counts=change_counts,
        status_rates=_rates(status_counts),
        autonomy_rates=_rates(autonomy_counts),
        change_rates=_rates(change_counts),
        fingerprint=fingerprint,
    )
    return SignedBaseline(
        baseline_id=baseline_id,
        payload=payload,
        signature=_sign_guard_model(payload, authority_private_key, authority_public_key),
    )


def verify_baseline(
    baseline: SignedBaseline,
    trusted_public_key_path: str | Path | None = None,
) -> BaselineVerification:
    """Verify a signed baseline: digest, key binding, signature, trust key.

    Without ``trusted_public_key_path`` the embedded key is accepted on
    self-consistency alone and ``trusted`` stays ``False`` — the detector
    itself is only ever called with a configured trust key.
    """

    core = _verify_signed_receipt(
        baseline.payload.model_dump(mode="json"), baseline.signature, BASELINE_LABEL
    )
    problems = list(core.problems)
    identity_ok = baseline.baseline_id == baseline.payload.baseline_id
    if not identity_ok:
        problems.append("envelope baseline_id does not match the signed payload")
    trusted = False
    if trusted_public_key_path is not None:
        trusted = _embedded_key_matches_trust_key(
            baseline.signature.public_key_pem, trusted_public_key_path
        )
        if not trusted:
            problems.append("baseline signing key is not the configured trust key")
    return BaselineVerification(
        valid=core.valid and identity_ok and (trusted or trusted_public_key_path is None),
        key_matches=core.key_matches,
        digest_matches=core.digest_matches,
        signature_valid=core.signature_valid,
        trusted=trusted,
        problems=problems,
    )


def load_signed_baseline(
    path: str | Path,
    trusted_public_key_path: str | Path,
) -> SignedBaseline:
    """Load and verify a signed baseline from disk (fail-closed).

    Any structural, identity, cryptographic or trust problem raises
    ``ValueError``: the caller must refuse to compare, never proceed.
    """

    resolved = Path(path)
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{resolved} not found: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"{resolved} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or "payload" not in document or "signature" not in document:
        raise ValueError(
            f"{resolved} is not a signed baseline envelope; an unsigned baseline is refused"
        )
    try:
        baseline = SignedBaseline.model_validate(document)
    except Exception as exc:
        raise ValueError(f"{resolved} is not a valid signed baseline: {exc}") from exc
    verification = verify_baseline(baseline, trusted_public_key_path)
    if not verification.valid:
        raise ValueError(
            f"{resolved} signed baseline is not trusted: {'; '.join(verification.problems)}"
        )
    return baseline


def write_baseline(baseline: SignedBaseline, path: str | Path) -> str:
    """Write a signed baseline atomically; returns the document sha256."""

    document = json.dumps(baseline.model_dump(mode="json"), indent=2) + "\n"
    return _atomic_write(Path(path), document)


def load_window_from_dir(directory: str | Path) -> list[MonitoringCycle]:
    """Load a window from ``cycle-*.json`` records, ordered by ``issued_at``."""

    resolved = Path(directory)
    files = sorted(resolved.glob("cycle-*.json"))
    if not files:
        raise ValueError(f"{resolved} contains no cycle-*.json records")
    cycles = [load_previous_cycle(path) for path in files]
    cycles.sort(key=_cycle_moment)
    return cycles


def _refusal_id(
    *,
    stage: str,
    reason_code: str,
    reason: str,
    produced_by: str,
    issued_at: str,
    signal_id: str | None,
    baseline_id: str | None,
) -> str:
    return "REFUSED-" + hashlib.sha256(
        _canonical_bytes(
            {
                "stage": stage,
                "reason_code": reason_code,
                "reason": reason,
                "produced_by": produced_by,
                "issued_at": issued_at,
                "signal_id": signal_id,
                "baseline_id": baseline_id,
            }
        )
    ).hexdigest()[:24].upper()


def record_drift_refusal(
    ledger: AuditLedger | None,
    *,
    stage: Literal["check", "respond"],
    reason_code: DriftRefusalReason,
    reason: str,
    produced_by: str,
    issued_at: str | None = None,
    signal: DriftSignal | None = None,
    baseline: SignedBaseline | None = None,
    contract_id: str | None = None,
) -> DriftRefusal | None:
    """Declare one fail-closed refusal in the ledger (observation, no authority).

    Returns the declared record, or ``None`` when no ledger was supplied: the
    caller still refuses, there is simply no durable evidence to append to.
    """

    if ledger is None:
        return None
    moment = _parse_moment(issued_at).isoformat()
    profile = baseline.payload if baseline is not None else None
    signal_id = signal.signal_id if signal is not None else None
    refusal = DriftRefusal(
        refusal_id=_refusal_id(
            stage=stage,
            reason_code=reason_code,
            reason=reason,
            produced_by=produced_by,
            issued_at=moment,
            signal_id=signal_id,
            baseline_id=profile.baseline_id if profile is not None else None,
        ),
        produced_by=produced_by,
        issued_at=moment,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        signal_id=signal_id,
        baseline_id=profile.baseline_id if profile is not None else None,
        baseline_version=profile.baseline_version if profile is not None else None,
        contract_id=(
            contract_id
            or (signal.contract_id if signal is not None else None)
            or (profile.contract_id if profile is not None else None)
        ),
    )
    from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

    ledger.append(
        AuditEventType.drift_refusal,
        refusal.refusal_id,
        moment,
        refusal.model_dump(mode="json"),
    )
    return refusal


def refuse_drift(
    ledger: AuditLedger | None,
    *,
    stage: Literal["check", "respond"],
    reason_code: DriftRefusalReason,
    reason: str,
    produced_by: str,
    issued_at: str | None = None,
    signal: DriftSignal | None = None,
    baseline: SignedBaseline | None = None,
    contract_id: str | None = None,
) -> NoReturn:
    """Declare a fail-closed refusal in the ledger and raise it (fail-closed)."""

    refusal = record_drift_refusal(
        ledger,
        stage=stage,
        reason_code=reason_code,
        reason=reason,
        produced_by=produced_by,
        issued_at=issued_at,
        signal=signal,
        baseline=baseline,
        contract_id=contract_id,
    )
    raise DriftRefusalError(reason, refusal=refusal)


# ---------------------------------------------------------------------------
# OP-3 — compare a window against the baseline and record the signal
# ---------------------------------------------------------------------------


def detect_drift(
    baseline: SignedBaseline,
    window: Sequence[MonitoringCycle],
    *,
    trust_key_path: str | Path,
    tolerances: DriftTolerances | None = None,
    produced_by: str = "integrity-drift-detector",
    issued_at: str | None = None,
    latest_baseline_id: str | None = None,
    ledger: AuditLedger | None = None,
) -> DriftSignal:
    """Compare ``window`` against the verified baseline (OP-3).

    Returns a :class:`DriftSignal` and, when the status is ``drift`` or
    ``indeterminate``, appends exactly one ``drift_signal`` event to ``ledger``.
    ``clear`` is deliberately not recorded ("signal or nothing"): the ledger
    carries signals, not a heartbeat of silence.

    Refusals (``DriftRefusalError``): unverified or superseded baselines,
    declared in the ledger as ``drift_refusal`` records when one was supplied.
    Uncertain windows are not refused — they are declared ``indeterminate``,
    recorded, and never presented as clear.
    """

    declared = tolerances or DriftTolerances()
    verification = verify_baseline(baseline, trust_key_path)
    if not verification.valid:
        refuse_drift(
            ledger,
            stage="check",
            reason_code="baseline_unverified",
            reason=(
                "refusing to compare against an unverified baseline: "
                + "; ".join(verification.problems)
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            baseline=baseline,
        )
    payload = baseline.payload
    if latest_baseline_id is not None and payload.baseline_id != latest_baseline_id:
        refuse_drift(
            ledger,
            stage="check",
            reason_code="baseline_superseded",
            reason=(
                f"baseline {payload.baseline_id} is not the latest ({latest_baseline_id}); "
                "reuse of a superseded baseline is refused"
            ),
            produced_by=produced_by,
            issued_at=issued_at,
            baseline=baseline,
        )
    moment = _parse_moment(issued_at)
    problems = _window_problems(window)
    if window:
        contracts = {cycle.contract_id for cycle in window}
        if contracts != {payload.contract_id}:
            problems.append(
                f"window contract {', '.join(sorted(contracts))} does not match "
                f"baseline contract {payload.contract_id}"
            )
        versions = {cycle.contract_version for cycle in window}
        if versions != {payload.contract_version}:
            problems.append(
                f"window contract version {', '.join(sorted(versions))} does not match "
                f"baseline version {payload.contract_version}"
            )
    shifts: list[DistributionShift] = []
    new_categories: list[str] = []
    reasons: list[str] = list(problems)
    if problems:
        status = DriftStatus.indeterminate
    else:
        status_counts, autonomy_counts, change_counts = _window_distributions(window)
        run_rates = _rates(status_counts)
        autonomy_rates = _rates(autonomy_counts)
        change_rates = _rates(change_counts)
        for dimension, rates, baseline_rates, tolerance in (
            ("status", run_rates, payload.status_rates, declared.max_status_shift),
            ("autonomy", autonomy_rates, payload.autonomy_rates, declared.max_autonomy_shift),
            ("change", change_rates, payload.change_rates, declared.max_change_shift),
        ):
            distance = _total_variation(rates, baseline_rates)
            exceeded = distance > tolerance
            shifts.append(
                DistributionShift(
                    dimension=dimension, distance=distance, tolerance=tolerance, exceeded=exceeded
                )
            )
            if exceeded:
                reasons.append(
                    f"{dimension} distribution shifted by {distance:.3f} "
                    f"(tolerance {tolerance:.3f})"
                )
            if declared.flag_new_categories:
                for key in sorted(rates):
                    if key not in baseline_rates:
                        new_categories.append(f"{dimension}:{key}")
        if new_categories:
            reasons.append(
                "new categories observed in window: " + ", ".join(new_categories)
            )
        status = DriftStatus.drift if reasons else DriftStatus.clear
    signal_id = "DRIFT-" + hashlib.sha256(
        _canonical_bytes(
            {
                "baseline_id": payload.baseline_id,
                "baseline_fingerprint": payload.fingerprint,
                "window_sha256": _window_digest(window) if window else None,
                "cycle_ids": [cycle.cycle_id for cycle in window],
                "produced_by": produced_by,
                "issued_at": moment.isoformat(),
            }
        )
    ).hexdigest()[:24].upper()
    signal = DriftSignal(
        signal_id=signal_id,
        produced_by=produced_by,
        issued_at=moment.isoformat(),
        status=status,
        baseline_id=payload.baseline_id,
        baseline_version=payload.baseline_version,
        baseline_fingerprint=payload.fingerprint,
        contract_id=payload.contract_id,
        window_cycle_ids=[cycle.cycle_id for cycle in window],
        window_cycles=len(window),
        window_sha256=_window_digest(window) if window else None,
        shifts=shifts,
        new_categories=new_categories,
        reasons=reasons,
        tolerances=declared,
    )
    if ledger is not None and signal.status is not DriftStatus.clear:
        from autopilot.audit import AuditEventType  # imported lazily: audit imports this module

        ledger.append(
            AuditEventType.drift_signal,
            signal.signal_id,
            moment.isoformat(),
            signal.model_dump(mode="json"),
        )
    return signal
