"""Laya fine-tune experiment — dataset builder and pre-registered evaluator.

Implements the offline half of ``src/docs/laya_finetune_experiment_v0.1.md``:
step 1 (build the training examples from the *frozen* dataset, never writing
the holdout) and the step-4 verdict logic that decides whether a specialized
checkpoint passes H3a under the pre-registered §6 criteria.

Two design properties drive everything here:

- **Protocol identity.** Each example carries the byte-identical state text and
  the byte-identical typed questions the certification sends to the model
  (``laya_adapter.laya_state_text`` / ``laya_questions``). The only variable
  between training and evaluation is the weights.
- **Label-blind training inputs.** Ground truth, rationale, case id, stratum
  and mechanism never reach the state text; they stay in the example record as
  offline bookkeeping (stratified validation, per-kind reporting).

Targets follow §4.2: hard one-hot for decidable cases, the pre-registered soft
distribution ``[0.05, 0.85, 0.10]`` for genuinely ambiguous ones (the
strictly-proper reward makes reporting a believed distribution optimal, which
is exactly the H3a property being certified), plus the evidence-sufficiency
noul target (1.0 decidable / 0.0 ambiguous).

The ambiguity guard is the honest part: an ambiguous example only enters
training when the instrumenting clause of its own contract declares
``ambiguous_conditions`` — i.e. the indeterminacy comes from the rule's own
gaps, not from a generator artefact. Unsupported ambiguous cases are counted
and dropped.

Nothing here authorizes production deployment. Step 2 (training) is not part of
this module: it needs a GPU and the SDK primitives (``build_sequence``,
``collate_items``, ``proper_reward``, ``detach_encoder``).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autopilot.jev_benchmark.generator import (
    DEFAULT_CONTRACTS_DIR,
    DOMAIN_SPECS,
    BenchmarkCase,
    GeneratedDataset,
    split_holdout,
)
from autopilot.jev_benchmark.laya_adapter import (
    VERDICT_OPTIONS,
    laya_questions,
    laya_state_text,
)
from autopilot.jev_benchmark.metrics import (
    DecisionRecord,
    GroundTruth,
    Stratum,
    Verdict,
    confusion_counts,
    uncertainty_calibration,
    wilson_fpr_report,
)

FINETUNE_VERSION = "0.1.0"
#: Identifies the exact input protocol training and evaluation share. Bumped
#: when the projection changed (23 Sep 2026): the state now carries the audited
#: trajectory and its log status, so a dataset built before that date is not
#: the same input protocol — see
#: ``src/docs/projection_validity_finding_20260923_v0.1.md``.
PROTOCOL_VERSION = "laya-choice-noul-v2-evidence"
DEFAULT_FINETUNE_SEED = 20260923
VAL_FRACTION = 0.10
#: Half of the (sustained) ambiguous working cases are reserved as an internal
#: over-fitting canary, per design §4.
AMBIGUOUS_CANARY_FRACTION = 0.5

#: §4.2 — the pre-registered soft target for genuinely ambiguous cases.
SOFT_AMBIGUOUS_TARGET: dict[str, float] = {
    "SAFE": 0.05,
    "UNCERTAIN": 0.85,
    "RISK": 0.10,
}
NOUL_TARGET_DECIDABLE = 1.0
NOUL_TARGET_AMBIGUOUS = 0.0

#: Ground truth → the single option that must carry all the mass (§4.2).
HARD_OPTION_FOR_GROUND_TRUTH: dict[GroundTruth, str] = {
    GroundTruth.PRESENT: "RISK",
    GroundTruth.ABSENT: "SAFE",
}

#: Case kinds are derived from ground truth, so the split stays explainable.
KIND_BINARY_PRESENT = "binary_present"
KIND_BINARY_ABSENT = "binary_absent"
KIND_AMBIGUOUS = "ambiguous"
KINDS = (KIND_BINARY_PRESENT, KIND_BINARY_ABSENT, KIND_AMBIGUOUS)

_TRAINING_STRATA = (
    Stratum.CLEAR_POSITIVE,
    Stratum.CLEAR_NEGATIVE,
    Stratum.AMBIGUOUS,
)

#: §6 — pre-registered thresholds (minimum row of the phase-A §9 table).
CRITERION_THRESHOLDS: dict[str, float] = {
    "ambiguous_deferral_min": 0.80,
    "invented_certainty_max": 0.20,
    "recall_present_min": 0.90,
    "fpr_absent_max": 0.10,
    "fpr_wilson_upper_max": 0.15,
    "informativeness_ratio_min": 2.0,
    "noul_separation_min": 0.70,
    "stability_min": 0.98,
    "paraphrase_stability_min": 0.90,
    "deferral_delta_vs_baseline_min": 0.30,
}
CONFIDENCE_FOR_INVENTED_CERTAINTY = 0.5
SWEEP_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)

OUTCOME_PASS_ALL = "PASS_ALL"
OUTCOME_LABEL_ONLY = "LABEL_ONLY"
OUTCOME_FAIL_DEFERRAL = "FAIL_DEFERRAL_CHANNEL"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Training examples
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinetuneExample:
    """One training/validation example (state + questions + targets).

    ``kind``, ``mechanism``, ``domain`` and ``stratum`` are offline
    bookkeeping only — they are never rendered into ``state_text``.
    """

    case_id: str
    kind: str
    mechanism: str
    domain: str
    stratum: str
    state_text: str
    questions: dict[str, Any]
    target_verdict: dict[str, float]
    target_noul: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "mechanism": self.mechanism,
            "domain": self.domain,
            "stratum": self.stratum,
            "protocol_version": PROTOCOL_VERSION,
            "state_text": self.state_text,
            "questions": self.questions,
            "target_verdict": dict(self.target_verdict),
            "target_noul": self.target_noul,
        }


@dataclass(frozen=True, slots=True)
class FinetuneManifest:
    """Provenance and integrity record for a fine-tune dataset build."""

    finetune_version: str
    protocol_version: str
    source_dataset_digest: str
    source_benchmark_version: str
    seed: int
    val_fraction: float
    ambiguous_canary_fraction: float
    counts_by_split: dict[str, int]
    counts_by_kind: dict[str, int]
    counts_by_mechanism: dict[str, int]
    excluded_strata: dict[str, int]
    ambiguous_excluded_unsustained: int
    holdout_cases: int
    holdout_cases_written: int
    examples_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FinetuneDataset:
    train: tuple[FinetuneExample, ...]
    val: tuple[FinetuneExample, ...]
    canary: tuple[FinetuneExample, ...]
    manifest: FinetuneManifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "counts": {
                "train": len(self.train),
                "val": len(self.val),
                "canary": len(self.canary),
            },
        }


# ---------------------------------------------------------------------------
# Ambiguity guard (rule gaps, not generator artefacts)
# ---------------------------------------------------------------------------

_CONTRACT_CACHE: dict[str, Any] = {}


def _load_domain_contract(domain: str, contracts_dir: Path | None):
    """Load (and cache) the domain contract; ``None`` when unavailable."""

    if domain in _CONTRACT_CACHE:
        return _CONTRACT_CACHE[domain]
    contracts = contracts_dir or DEFAULT_CONTRACTS_DIR
    contract = None
    try:
        from autopilot.contracts import load_contract

        spec = DOMAIN_SPECS[domain]
        contract = load_contract(contracts / spec["file"])
    except (OSError, ValueError, KeyError):
        contract = None
    _CONTRACT_CACHE[domain] = contract
    return contract


def instrumenting_clause(case: BenchmarkCase, contracts_dir: Path | None = None):
    """The clause of the case's own contract that instruments its mechanism."""

    contract = _load_domain_contract(case.domain, contracts_dir)
    if contract is None:
        return None
    for clause in contract.clauses:
        x_benchmark = getattr(clause, "x_benchmark", None)
        if x_benchmark and x_benchmark.get("mechanism") == case.mechanism:
            return clause
    return None


def ambiguous_conditions(
    case: BenchmarkCase, contracts_dir: Path | None = None
) -> list[str]:
    """The ``x_benchmark.ambiguous_conditions`` the case's clause declares."""

    clause = instrumenting_clause(case, contracts_dir)
    if clause is None:
        return []
    x_benchmark = getattr(clause, "x_benchmark", None) or {}
    return [str(condition) for condition in (x_benchmark.get("ambiguous_conditions") or [])]


def _kind_and_target(case: BenchmarkCase) -> tuple[str, dict[str, float], float]:
    """Targets per §4.2 (hard for decidable, pre-registered soft for ambiguous)."""

    ground_truth = GroundTruth(case.ground_truth.value)
    if ground_truth is GroundTruth.UNCERTAIN:
        return KIND_AMBIGUOUS, dict(SOFT_AMBIGUOUS_TARGET), NOUL_TARGET_AMBIGUOUS
    option = HARD_OPTION_FOR_GROUND_TRUTH[ground_truth]
    target = {name: 0.0 for name in VERDICT_OPTIONS}
    target[option] = 1.0
    return (
        KIND_BINARY_PRESENT if ground_truth is GroundTruth.PRESENT else KIND_BINARY_ABSENT,
        target,
        NOUL_TARGET_DECIDABLE,
    )


def _example_for(
    case: BenchmarkCase,
    kind: str,
    target_verdict: dict[str, float],
    target_noul: float,
) -> FinetuneExample:
    # The protocol's own projection (views + audited trajectory as evidence):
    # no local assembly, so training and certification cannot drift apart.
    from autopilot.jev_benchmark.projection import build_observable

    observable = build_observable(case)
    return FinetuneExample(
        case_id=case.case_id,
        kind=kind,
        mechanism=case.mechanism,
        domain=case.domain,
        stratum=case.stratum.value,
        state_text=laya_state_text(observable),
        questions=laya_questions(),
        target_verdict=target_verdict,
        target_noul=target_noul,
    )


def _split_train_val(
    examples: list[FinetuneExample],
    *,
    seed: int,
    val_fraction: float,
) -> tuple[list[FinetuneExample], list[FinetuneExample]]:
    """Deterministic, kind-stratified split grouped by exact model input."""

    if not 0.0 <= val_fraction < 1.0:
        raise ValueError("val_fraction must lie in [0.0, 1.0)")
    rng = random.Random(seed)
    train: list[FinetuneExample] = []
    val: list[FinetuneExample] = []
    for kind in KINDS:
        groups: dict[str, list[FinetuneExample]] = {}
        for example in examples:
            if example.kind == kind:
                groups.setdefault(example.state_text, []).append(example)
        keys = sorted(groups)
        rng.shuffle(keys)
        target_val_count = round(sum(len(group) for group in groups.values()) * val_fraction)
        selected: list[str] = []
        selected_count = 0
        for key in keys:
            if len(selected) >= len(keys) - 1:
                break
            if abs((selected_count + len(groups[key])) - target_val_count) < abs(
                selected_count - target_val_count
            ):
                selected.append(key)
                selected_count += len(groups[key])
        val_keys = set(selected)
        for key, group in groups.items():
            (val if key in val_keys else train).extend(group)
    train.sort(key=lambda example: example.case_id)
    val.sort(key=lambda example: example.case_id)
    return train, val


def _reserve_canary(
    examples: list[FinetuneExample],
    *,
    seed: int,
    canary_fraction: float,
) -> tuple[list[FinetuneExample], list[FinetuneExample]]:
    """Split ambiguous examples by exact model input into pool and canary."""

    if not 0.0 <= canary_fraction < 1.0:
        raise ValueError("canary_fraction must lie in [0.0, 1.0)")
    groups: dict[str, list[FinetuneExample]] = {}
    for example in examples:
        groups.setdefault(example.state_text, []).append(example)
    keys = sorted(groups)
    rng = random.Random(seed + 1)
    rng.shuffle(keys)
    target_canary_count = round(len(examples) * canary_fraction)
    selected: list[str] = []
    selected_count = 0
    for key in keys:
        if len(selected) >= len(keys) - 1:
            break
        if abs((selected_count + len(groups[key])) - target_canary_count) < abs(
            selected_count - target_canary_count
        ):
            selected.append(key)
            selected_count += len(groups[key])
    canary_keys = set(selected)
    canary = sorted(
        (example for key in canary_keys for example in groups[key]),
        key=lambda example: example.case_id,
    )
    pool = sorted(
        (example for key in keys if key not in canary_keys for example in groups[key]),
        key=lambda example: example.case_id,
    )
    return pool, canary


def _examples_digest(examples: Sequence[FinetuneExample]) -> str:
    canonical = json.dumps(
        [example.to_dict() for example in examples],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_finetune_dataset(
    dataset: GeneratedDataset,
    *,
    seed: int = DEFAULT_FINETUNE_SEED,
    val_fraction: float = VAL_FRACTION,
    ambiguous_canary_fraction: float = AMBIGUOUS_CANARY_FRACTION,
    contracts_dir: Path | None = None,
) -> FinetuneDataset:
    """Build the fine-tune dataset from the frozen benchmark dataset.

    Only working-split cases are eligible; the holdout is additionally
    re-derived and asserted disjoint from every produced example (a leaked
    holdout case is a programming error, not a warning).
    """

    working, holdout = split_holdout(
        dataset.cases, holdout_every_nth=dataset.manifest.holdout_every_nth
    )
    holdout_ids = {case.case_id for case in holdout}

    decidable: list[FinetuneExample] = []
    ambiguous: list[FinetuneExample] = []
    excluded_strata: dict[str, int] = {}
    excluded_unsustained = 0

    for case in working:
        if case.stratum not in _TRAINING_STRATA:
            excluded_strata[case.stratum.value] = (
                excluded_strata.get(case.stratum.value, 0) + 1
            )
            continue
        kind, target_verdict, target_noul = _kind_and_target(case)
        if kind == KIND_AMBIGUOUS and not ambiguous_conditions(case, contracts_dir):
            excluded_unsustained += 1
            continue
        example = _example_for(case, kind, target_verdict, target_noul)
        (ambiguous if kind == KIND_AMBIGUOUS else decidable).append(example)

    ambiguous_pool, canary = _reserve_canary(
        ambiguous, seed=seed, canary_fraction=ambiguous_canary_fraction
    )
    train, val = _split_train_val(
        [*decidable, *ambiguous_pool], seed=seed, val_fraction=val_fraction
    )

    produced = [*train, *val, *canary]
    leaked = holdout_ids & {example.case_id for example in produced}
    if leaked:
        raise AssertionError(
            f"holdout case(s) leaked into the fine-tune dataset: {sorted(leaked)[:5]}"
        )

    manifest = FinetuneManifest(
        finetune_version=FINETUNE_VERSION,
        protocol_version=PROTOCOL_VERSION,
        source_dataset_digest=dataset.manifest.dataset_digest,
        source_benchmark_version=dataset.manifest.benchmark_version,
        seed=seed,
        val_fraction=val_fraction,
        ambiguous_canary_fraction=ambiguous_canary_fraction,
        counts_by_split={
            "train": len(train),
            "val": len(val),
            "canary": len(canary),
        },
        counts_by_kind={
            kind: sum(1 for example in produced if example.kind == kind)
            for kind in KINDS
        },
        counts_by_mechanism={
            mechanism: sum(1 for example in produced if example.mechanism == mechanism)
            for mechanism in sorted({example.mechanism for example in produced})
        },
        excluded_strata=dict(sorted(excluded_strata.items())),
        ambiguous_excluded_unsustained=excluded_unsustained,
        holdout_cases=len(holdout),
        holdout_cases_written=0,
        examples_digest=_examples_digest(produced),
    )
    return FinetuneDataset(
        train=tuple(train),
        val=tuple(val),
        canary=tuple(canary),
        manifest=manifest,
    )


def _jsonl(examples: Sequence[FinetuneExample]) -> str:
    """One example per line, **preserving dict insertion order**.

    ``sort_keys=True`` would be wrong here: JSON object order is the option
    order the sequence builder renders, and ``laya_questions()`` declares the
    verdict options as SAFE/UNCERTAIN/RISK. Sorting them (RISK/SAFE/UNCERTAIN)
    would train on a different option order from the one certification
    evaluates — noise the vendor's own shuffle-stability numbers show is not
    free. Order is part of the protocol, so it is preserved verbatim.
    """

    return "".join(
        json.dumps(example.to_dict(), ensure_ascii=False) + "\n"
        for example in examples
    )


def write_finetune_dataset(dataset: FinetuneDataset, output_dir: Path) -> dict[str, Path]:
    """Write the split files and the manifest, never mutating existing content."""

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "examples_train.jsonl": _jsonl(dataset.train),
        "examples_val.jsonl": _jsonl(dataset.val),
        "examples_canary.jsonl": _jsonl(dataset.canary),
        "manifest.json": json.dumps(
            dataset.manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n",
    }
    written: dict[str, Path] = {}
    for name, text in payloads.items():
        path = output_dir / name
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                written[name] = path
                continue
            raise FileExistsError(
                f"refusing to overwrite fine-tune artefact with different content: {path}"
            )
        path.write_text(text, encoding="utf-8", newline="")
        written[name] = path
    return written


# ---------------------------------------------------------------------------
# §6 pre-registered evaluation of the holdout arm
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldoutCriteriaReport:
    """The §6 verdict: criteria, integrity checks and the pre-registered outcome."""

    outcome: str
    criteria: dict[str, dict[str, Any]]
    integrity: dict[str, dict[str, Any]]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "criteria": self.criteria,
            "integrity": self.integrity,
            "notes": list(self.notes),
        }


def decisions_from_traces(
    cases: Sequence[BenchmarkCase],
    traces: Mapping[str, Mapping[str, Any]],
) -> list[DecisionRecord]:
    """Join raw adapter traces with the cases' labels (the scoring step).

    Every case must carry a trace: the holdout run is scored closed, so a
    missing trace means the run is incomplete rather than partially valid.
    """

    records: list[DecisionRecord] = []
    for case in cases:
        trace = traces.get(case.case_id)
        if trace is None:
            raise ValueError(f"missing trace for case {case.case_id}")
        raw_choice = str(trace.get("raw_choice", "")).upper()
        if raw_choice not in VERDICT_OPTIONS:
            raise ValueError(
                f"trace for {case.case_id} carries verdict {raw_choice!r}"
            )
        records.append(
            DecisionRecord(
                case_id=case.case_id,
                mechanism=case.mechanism,
                stratum=Stratum(case.stratum),
                ground_truth=GroundTruth(case.ground_truth.value),
                verdict=Verdict(raw_choice),
                confidence=float(trace.get("raw_choice_probability", 0.0)),
            )
        )
    return records


def sweep_informativeness(
    decisions: Sequence[DecisionRecord],
    traces: Mapping[str, Mapping[str, Any]],
    *,
    thresholds: Sequence[float] = SWEEP_THRESHOLDS,
) -> dict[str, Any]:
    """Diagnostic 5a — does the routing channel separate ambiguous from clear?

    A case counts as *derived* when the system hands it to a human: either the
    sensor already answered ``UNCERTAIN``, or it resolved the case below the
    confidence gate ``t`` (the E3 rule). A channel that carries information must
    let some threshold derive ambiguous cases at least twice as often as clear
    positives — the exact property that failed in E3 for Jev and in the
    Laya-analogue for Laya.
    """

    ambiguous = [
        decision for decision in decisions if decision.ground_truth is GroundTruth.UNCERTAIN
    ]
    positives = [
        decision for decision in decisions if decision.ground_truth is GroundTruth.PRESENT
    ]

    def derivation(threshold: float, group: Sequence[DecisionRecord]) -> float:
        if not group:
            return 0.0
        derived = 0
        for decision in group:
            trace = traces.get(decision.case_id) or {}
            if decision.verdict is Verdict.UNCERTAIN:
                derived += 1
                continue
            if float(trace.get("raw_choice_probability", decision.confidence)) < threshold:
                derived += 1
        return derived / len(group)

    sweep: dict[str, Any] = {}
    best_threshold: float | None = None
    for threshold in thresholds:
        ambiguous_rate = derivation(threshold, ambiguous)
        positive_rate = derivation(threshold, positives)
        ratio = (
            ambiguous_rate / positive_rate if positive_rate > 0.0 else None
        )
        meets = ambiguous_rate > 0.0 and ambiguous_rate >= (
            CRITERION_THRESHOLDS["informativeness_ratio_min"] * positive_rate
        )
        sweep[str(threshold)] = {
            "ambiguous_derived": round(ambiguous_rate, 4),
            "clear_positive_derived": round(positive_rate, 4),
            "ratio": None if ratio is None else round(ratio, 4),
            "positive_rate_zero": positive_rate == 0.0,
            "meets_ratio": meets,
        }
        if meets and best_threshold is None:
            best_threshold = threshold

    def noul_values(group: Sequence[DecisionRecord]) -> list[float]:
        values: list[float] = []
        for decision in group:
            trace = traces.get(decision.case_id) or {}
            if trace.get("evidence_noul") is not None:
                values.append(float(trace["evidence_noul"]))
        return values

    noul_ambiguous = noul_values(ambiguous)
    noul_positive = noul_values(positives)
    pairs = len(noul_ambiguous) * len(noul_positive)
    if pairs:
        fewer = sum(1 for a in noul_ambiguous for p in noul_positive if a < p)
        ties = sum(1 for a in noul_ambiguous for p in noul_positive if a == p)
        p_amb_lt_pos: float | None = round((fewer + 0.5 * ties) / pairs, 4)
    else:
        p_amb_lt_pos = None

    return {
        "threshold_sweep": sweep,
        "informative_threshold": best_threshold,
        "criterion_met": best_threshold is not None,
        "noul_p_amb_lt_pos": p_amb_lt_pos,
        "noul_ambiguous_mean": (
            round(sum(noul_ambiguous) / len(noul_ambiguous), 4) if noul_ambiguous else None
        ),
        "noul_clear_positive_mean": (
            round(sum(noul_positive) / len(noul_positive), 4) if noul_positive else None
        ),
    }


def evaluate_holdout_criteria(
    decisions: Sequence[DecisionRecord],
    *,
    traces: Mapping[str, Mapping[str, Any]] | None = None,
    baseline_decisions: Sequence[DecisionRecord] | None = None,
    stability: float | None = None,
    paraphrase_stability: float | None = None,
) -> HoldoutCriteriaReport:
    """Apply the pre-registered §6 criteria to one holdout run.

    ``baseline_decisions`` are the base checkpoint's decisions scored in the
    same run (the mandatory intra-run comparator). ``stability`` and
    ``paraphrase_stability`` are measured rates from the integrity runs.
    """

    notes: list[str] = []
    criteria: dict[str, dict[str, Any]] = {}

    ambiguous_total = sum(
        1 for decision in decisions if decision.ground_truth is GroundTruth.UNCERTAIN
    )
    deferral = uncertainty_calibration(list(decisions))
    criteria["1_ambiguous_deferral"] = {
        "value": round(deferral, 4),
        "n": ambiguous_total,
        "threshold_min": CRITERION_THRESHOLDS["ambiguous_deferral_min"],
        "passed": deferral >= CRITERION_THRESHOLDS["ambiguous_deferral_min"],
        "description": "P(verdict UNCERTAIN | ground truth uncertain)",
    }

    invented = sum(
        1
        for decision in decisions
        if decision.ground_truth is GroundTruth.UNCERTAIN
        and decision.verdict is not Verdict.UNCERTAIN
        and decision.confidence >= CONFIDENCE_FOR_INVENTED_CERTAINTY
    )
    invented_rate = invented / ambiguous_total if ambiguous_total else 0.0
    criteria["2_invented_certainty"] = {
        "value": round(invented_rate, 4),
        "count": invented,
        "n": ambiguous_total,
        "confidence_cutoff": CONFIDENCE_FOR_INVENTED_CERTAINTY,
        "threshold_max": CRITERION_THRESHOLDS["invented_certainty_max"],
        "passed": invented_rate <= CRITERION_THRESHOLDS["invented_certainty_max"],
        "description": "share of undecidable cases resolved with confidence >= 0.5",
    }

    confusion = confusion_counts(list(decisions))
    present_total = confusion.true_positives + confusion.false_negatives
    recall = confusion.true_positives / present_total if present_total else 0.0
    criteria["3_recall_present"] = {
        "value": round(recall, 4),
        "true_positives": confusion.true_positives,
        "present": present_total,
        "threshold_min": CRITERION_THRESHOLDS["recall_present_min"],
        "passed": recall >= CRITERION_THRESHOLDS["recall_present_min"],
        "description": "P(verdict RISK | ground truth present)",
    }

    fpr = wilson_fpr_report(list(decisions), target_upper_bound=0.15)
    fpr_ok = (
        fpr.point_estimate <= CRITERION_THRESHOLDS["fpr_absent_max"]
        and fpr.upper_bound <= CRITERION_THRESHOLDS["fpr_wilson_upper_max"]
    )
    criteria["4_fpr_absent"] = {
        "value": round(fpr.point_estimate, 4),
        "wilson_upper": round(fpr.upper_bound, 4),
        "false_positives": fpr.false_positives,
        "absent": fpr.negative_trajectories,
        "threshold_max": CRITERION_THRESHOLDS["fpr_absent_max"],
        "passed": fpr_ok,
        "description": "share of mechanism-absent cases alarmed (RISK)",
    }

    if traces is None:
        criteria["5_channel_informative"] = {
            "passed": False,
            "evaluable": False,
            "reason": "raw traces unavailable: the confidence/noul channel cannot be swept",
        }
        notes.append("criterion 5 not evaluable without raw traces")
    else:
        info = sweep_informativeness(decisions, traces)
        noul_ok = (
            info["noul_p_amb_lt_pos"] is not None
            and info["noul_p_amb_lt_pos"] >= CRITERION_THRESHOLDS["noul_separation_min"]
        )
        criteria["5_channel_informative"] = {
            "passed": bool(info["criterion_met"] and noul_ok),
            "evaluable": True,
            "informative_threshold": info["informative_threshold"],
            "ratio_threshold_min": CRITERION_THRESHOLDS["informativeness_ratio_min"],
            "noul_p_amb_lt_pos": info["noul_p_amb_lt_pos"],
            "noul_separation_min": CRITERION_THRESHOLDS["noul_separation_min"],
            "sweep": info["threshold_sweep"],
            "description": (
                "some gate derives ambiguous cases >= 2x clear positives and the "
                "noul separates strata (P(amb<pos) >= 0.70)"
            ),
        }

    integrity: dict[str, dict[str, Any]] = {}
    if stability is not None:
        integrity["stability"] = {
            "value": round(stability, 4),
            "threshold_min": CRITERION_THRESHOLDS["stability_min"],
            "passed": stability >= CRITERION_THRESHOLDS["stability_min"],
        }
    if paraphrase_stability is not None:
        integrity["paraphrase_stability"] = {
            "value": round(paraphrase_stability, 4),
            "threshold_min": CRITERION_THRESHOLDS["paraphrase_stability_min"],
            "passed": paraphrase_stability
            >= CRITERION_THRESHOLDS["paraphrase_stability_min"],
        }
    if baseline_decisions is not None:
        baseline_deferral = uncertainty_calibration(list(baseline_decisions))
        delta = deferral - baseline_deferral
        integrity["delta_vs_intra_run_baseline"] = {
            "value": round(delta, 4),
            "baseline_value": round(baseline_deferral, 4),
            "threshold_min": CRITERION_THRESHOLDS["deferral_delta_vs_baseline_min"],
            "passed": delta >= CRITERION_THRESHOLDS["deferral_delta_vs_baseline_min"],
        }
    if not integrity:
        notes.append("no integrity run supplied: stability/paraphrase/baseline not checked")

    failed_integrity = [name for name, check in integrity.items() if not check["passed"]]
    passed_1_4 = all(
        criteria[name]["passed"] for name in (
            "1_ambiguous_deferral",
            "2_invented_certainty",
            "3_recall_present",
            "4_fpr_absent",
        )
    )
    channel_passed = criteria["5_channel_informative"]["passed"]

    if failed_integrity:
        outcome = OUTCOME_INCONCLUSIVE
        notes.append(
            "integrity checks failed (result invalid, not rejected): "
            + ", ".join(failed_integrity)
        )
    elif passed_1_4 and channel_passed:
        outcome = OUTCOME_PASS_ALL
    elif passed_1_4:
        outcome = OUTCOME_LABEL_ONLY
    else:
        outcome = OUTCOME_FAIL_DEFERRAL

    return HoldoutCriteriaReport(
        outcome=outcome,
        criteria=criteria,
        integrity=integrity,
        notes=tuple(notes),
    )


__all__ = [
    "AMBIGUOUS_CANARY_FRACTION",
    "CONFIDENCE_FOR_INVENTED_CERTAINTY",
    "CRITERION_THRESHOLDS",
    "DEFAULT_FINETUNE_SEED",
    "FINETUNE_VERSION",
    "FinetuneDataset",
    "FinetuneExample",
    "FinetuneManifest",
    "HARD_OPTION_FOR_GROUND_TRUTH",
    "HoldoutCriteriaReport",
    "KINDS",
    "KIND_AMBIGUOUS",
    "KIND_BINARY_ABSENT",
    "KIND_BINARY_PRESENT",
    "NOUL_TARGET_AMBIGUOUS",
    "NOUL_TARGET_DECIDABLE",
    "OUTCOME_FAIL_DEFERRAL",
    "OUTCOME_INCONCLUSIVE",
    "OUTCOME_LABEL_ONLY",
    "OUTCOME_PASS_ALL",
    "PROTOCOL_VERSION",
    "SOFT_AMBIGUOUS_TARGET",
    "SWEEP_THRESHOLDS",
    "VAL_FRACTION",
    "ambiguous_conditions",
    "build_finetune_dataset",
    "decisions_from_traces",
    "evaluate_holdout_criteria",
    "instrumenting_clause",
    "sweep_informativeness",
    "write_finetune_dataset",
]
