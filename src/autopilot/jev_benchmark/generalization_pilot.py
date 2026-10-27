"""Offline pilot tooling for a future Laya generalization cohort.

The synthetic fixture builder validates plumbing only. The causal-family
prototype uses public contracts and the deterministic evaluator, compares
classifier-visible inputs only with the existing phase-A working split, and
writes exclusively below the checkout's .tmp directory. Neither builder reads,
serializes, scores, or opens the phase-A holdout.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from autopilot.contracts import load_contract
from autopilot.evaluator import ClauseStatus, evaluate_contract
from autopilot.jev_benchmark.generator import (
    DEFAULT_CONTRACTS_DIR,
    DOMAIN_ORDER,
    DOMAIN_SPECS,
    BenchmarkCase,
    ContractRef,
    GTLabel,
    HOLDOUT_EVERY_NTH,
    _instrumenting_status,
    _label_for,
    _natural_view,
    _secondary_mechanisms,
    _structured_view,
)
from autopilot.jev_benchmark.laya_adapter import laya_state_text
from autopilot.jev_benchmark.metrics import MECHANISMS, Stratum
from autopilot.jev_benchmark.projection import build_observable
from autopilot.trajectory import Trajectory, TrajectoryObservation

PILOT_VERSION = "laya-generalization-plumbing-pilot-v0.1"
CAUSAL_PILOT_VERSION = "laya-causal-family-candidate-pool-v0.2"
SPLITS = ("fit", "selection", "confirmatory")
LABELS = ("present", "absent", "uncertain")
PILOT_SEED = 20261021
CAUSAL_PILOT_SEED = 20261022


class GeneralizationPilotIntegrityError(ValueError):
    """Raised when a pilot violates its integrity guards."""


@dataclass(frozen=True, slots=True)
class PilotCase:
    case_id: str
    family_id: str
    mechanism: str
    label: str
    variant_index: int
    state_text: str
    fixture_only: bool = True

    @property
    def cell(self) -> str:
        return f"{self.mechanism}|{self.label}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CausalCase:
    """One contract-labelled trajectory for causal novelty analysis only."""

    case_id: str
    family_id: str
    mechanism: str
    domain: str
    label: str
    variant: str
    perturbation: dict[str, Any]
    trajectory: dict[str, Any]
    state_text: str
    family_fingerprint: str
    source_generation_digest: str
    arbiter_status: str
    clause_id: str

    @property
    def cell(self) -> str:
        return f"{self.mechanism}|{self.label}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _family_tie(seed: int, family_id: str, split: str) -> str:
    return hashlib.sha256(f"{seed}:{family_id}:{split}".encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    """Normalize punctuation/case/whitespace but retain lexical content."""

    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _text_tokens(text: str) -> set[str]:
    return set(_normalize_text(text).split())


def _build_token_index(references: list[set[str]]) -> dict[str, int]:
    """Index token presence as integer bitsets for efficient corpus comparisons."""

    postings: dict[str, int] = defaultdict(int)
    for index, reference in enumerate(references):
        bit = 1 << index
        for token in reference:
            postings[token] |= bit
    return dict(postings)


def _max_jaccard_tokens(
    tokens: set[str], references: list[set[str]], postings: dict[str, int] | None = None
) -> float:
    if not tokens:
        return 0.0
    index = postings if postings is not None else _build_token_index(references)
    candidate_refs = 0
    for token in tokens:
        candidate_refs |= index.get(token, 0)
    best = 0.0
    while candidate_refs:
        bit = candidate_refs & -candidate_refs
        reference_index = bit.bit_length() - 1
        other = references[reference_index]
        intersection = len(tokens & other)
        union_size = len(tokens) + len(other) - intersection
        if union_size:
            best = max(best, intersection / union_size)
        candidate_refs ^= bit
    return best


def _working_collision_reason(
    text: str,
    trajectory_fingerprint: str,
    *,
    exact_texts: set[str],
    normalized_texts: set[str],
    trajectory_fingerprints: set[str],
) -> str | None:
    if text in exact_texts:
        return "exact_text_match"
    if _normalize_text(text) in normalized_texts:
        return "normalized_text_match"
    if trajectory_fingerprint in trajectory_fingerprints:
        return "trajectory_match"
    return None


def assign_family_splits(
    cases: list[PilotCase] | tuple[PilotCase, ...],
    target_family_counts: dict[str, dict[str, int]],
    *,
    seed: int = PILOT_SEED,
) -> dict[str, str]:
    """Assign each complete family to one split, satisfying exact cell quotas."""

    _validate_cases(cases)
    if set(target_family_counts) != set(SPLITS):
        raise GeneralizationPilotIntegrityError(f"target splits must be exactly {SPLITS}")
    for split, targets in target_family_counts.items():
        if any(type(count) is not int or count < 0 for count in targets.values()):
            raise GeneralizationPilotIntegrityError(
                f"{split} targets must be non-negative integer family counts"
            )

    family_cells: dict[str, set[str]] = defaultdict(set)
    cell_families: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        family_cells[case.family_id].add(case.cell)
        cell_families[case.cell].add(case.family_id)
    cells = set(cell_families)
    for cell in cells:
        requested = sum(target_family_counts[split].get(cell, 0) for split in SPLITS)
        available = len(cell_families[cell])
        if requested != available:
            raise GeneralizationPilotIntegrityError(
                f"family quota for {cell} sums to {requested}, but {available} families exist"
            )
    unknown = {
        cell for targets in target_family_counts.values() for cell in targets
    } - cells
    if unknown:
        raise GeneralizationPilotIntegrityError(f"targets reference unknown cells: {sorted(unknown)}")

    remaining = {
        split: {cell: target_family_counts[split].get(cell, 0) for cell in cells}
        for split in SPLITS
    }
    assignment: dict[str, str] = {}
    ordered = sorted(
        family_cells,
        key=lambda family: (-len(family_cells[family]), _family_tie(seed, family, "order")),
    )
    cell_totals = {cell: len(family_ids) for cell, family_ids in cell_families.items()}
    for family in ordered:
        family_cell_set = family_cells[family]
        choices = [
            split
            for split in SPLITS
            if all(remaining[split][cell] > 0 for cell in family_cell_set)
        ]
        if not choices:
            raise GeneralizationPilotIntegrityError(
                f"cannot assign family {family} without violating exact cell quotas"
            )
        split = max(
            choices,
            key=lambda candidate: (
                sum(remaining[candidate][cell] / cell_totals[cell] for cell in family_cell_set),
                tuple(-ord(ch) for ch in _family_tie(seed, family, candidate)),
            ),
        )
        assignment[family] = split
        for cell in family_cell_set:
            remaining[split][cell] -= 1
    if any(value != 0 for targets in remaining.values() for value in targets.values()):
        raise GeneralizationPilotIntegrityError(f"family quotas unsatisfied: {remaining}")
    return assignment


def _validate_cases(cases: list[PilotCase] | tuple[PilotCase, ...]) -> None:
    if not cases:
        raise GeneralizationPilotIntegrityError("pilot requires at least one case")
    ids: set[str] = set()
    for case in cases:
        if case.case_id in ids:
            raise GeneralizationPilotIntegrityError(f"duplicate case_id: {case.case_id}")
        ids.add(case.case_id)
        if not case.family_id.strip():
            raise GeneralizationPilotIntegrityError(f"{case.case_id}: empty family_id")
        if case.mechanism not in MECHANISMS or case.label not in LABELS:
            raise GeneralizationPilotIntegrityError(f"{case.case_id}: invalid mechanism/label")
        if not case.state_text.strip() or case.fixture_only is not True:
            raise GeneralizationPilotIntegrityError(f"{case.case_id}: invalid fixture input")


def validate_split_integrity(
    cases: list[PilotCase] | tuple[PilotCase, ...], assignment: dict[str, str]
) -> dict[str, Any]:
    """Prove no family or exact model input crosses split boundaries."""

    _validate_cases(cases)
    observed_families = {case.family_id for case in cases}
    if set(assignment) != observed_families:
        raise GeneralizationPilotIntegrityError(
            "assignment must include every observed family exactly once"
        )
    if any(split not in SPLITS for split in assignment.values()):
        raise GeneralizationPilotIntegrityError("assignment includes an unknown split")
    texts = {split: set() for split in SPLITS}
    family_sets = {split: set() for split in SPLITS}
    split_cases = {split: [] for split in SPLITS}
    for case in cases:
        split = assignment[case.family_id]
        family_sets[split].add(case.family_id)
        texts[split].add(case.state_text)
        split_cases[split].append(case)
    for i, left in enumerate(SPLITS):
        for right in SPLITS[i + 1 :]:
            if overlap := texts[left] & texts[right]:
                raise GeneralizationPilotIntegrityError(
                    f"exact state_text collision across {left}/{right}: {sorted(overlap)[:2]}"
                )
    return {
        "case_counts": {split: len(split_cases[split]) for split in SPLITS},
        "family_counts": {split: len(family_sets[split]) for split in SPLITS},
        "cell_case_counts": {
            split: dict(sorted(Counter(case.cell for case in split_cases[split]).items()))
            for split in SPLITS
        },
        "unique_text_counts": {split: len(texts[split]) for split in SPLITS},
    }


@lru_cache(maxsize=4)
def _load_phase_a_working_cases(
    dataset_root: Path | None = None,
) -> tuple[list[BenchmarkCase], str]:
    """Load only frozen phase-A working files; never open a holdout case path."""

    root = dataset_root or Path(__file__).resolve().parents[3] / "data" / "jev_benchmark"
    manifest = json.loads((root / "manifests" / "dataset_manifest.json").read_text(encoding="utf-8"))
    case_count = manifest.get("case_count")
    holdout_every_nth = manifest.get("holdout_every_nth")
    holdout_count = manifest.get("holdout_count")
    if (
        type(case_count) is not int
        or type(holdout_count) is not int
        or type(holdout_every_nth) is not int
        or holdout_every_nth != HOLDOUT_EVERY_NTH
        or case_count % holdout_every_nth != 0
        or case_count // holdout_every_nth != holdout_count
    ):
        raise GeneralizationPilotIntegrityError("phase-A manifest has unexpected working/holdout geometry")

    working: list[BenchmarkCase] = []
    for number in range(1, case_count + 1):
        if number % holdout_every_nth == 0:
            continue
        case_id = f"CASE-{number:05d}"
        raw = yaml.safe_load((root / "cases" / f"{case_id}.yaml").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("case_id") != case_id:
            raise GeneralizationPilotIntegrityError(f"invalid working case payload: {case_id}")
        working.append(
            BenchmarkCase(
                case_id=case_id,
                mechanism=raw["mechanism"],
                domain=raw["domain"],
                stratum=Stratum(raw["stratum"]),
                ground_truth=GTLabel(**raw["ground_truth"]),
                contract=ContractRef(**raw["contract"]),
                trajectory=Trajectory.model_validate(raw["trajectory"]),
                structured_view=raw["structured_view"],
                natural_view=raw["natural_view"],
                paraphrases=raw["paraphrases"],
                generation_metadata=raw["generation_metadata"],
                secondary_mechanisms=raw["secondary_mechanisms"],
            )
        )
    expected_working = case_count - holdout_count
    if len(working) != expected_working:
        raise GeneralizationPilotIntegrityError(
            f"phase-A working case count mismatch: expected {expected_working}, got {len(working)}"
        )
    return working, str(manifest["dataset_digest"])


def _trajectory_fingerprint(trajectory: Trajectory) -> str:
    """Fingerprint behavior, not arbitrary trajectory/observation identifiers."""

    payload = trajectory.model_dump(mode="json")
    payload.pop("trajectory_id", None)
    payload.pop("candidate_version", None)
    observations = payload.get("observations", [])
    if observations:
        origin = datetime.fromisoformat(observations[0]["observed_at"].replace("Z", "+00:00"))
        for observation in observations:
            observation.pop("ref", None)
            observed_at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
            observation["offset_seconds"] = int((observed_at - origin).total_seconds())
            observation.pop("observed_at", None)
    return _canonical_digest(payload)


@lru_cache(maxsize=4)
def _load_causal_contract(domain: str):
    return load_contract(DEFAULT_CONTRACTS_DIR / DOMAIN_SPECS[domain]["file"])


def _context_note(domain: str, mechanism: str, family_index: int) -> str:
    """Add label-invariant, non-evidentiary scenario context for review."""

    profiles = {
        "refund": (
            ("mobile app", "telephone queue", "branch desk", "secure message", "partner portal", "weekend callback", "store counter", "accessibility line"),
            ("subscription renewal", "split shipment", "gift purchase", "travel booking", "duplicate receipt", "price adjustment", "warranty exchange", "family account"),
            ("delivery scan", "merchant receipt", "support transcript", "billing export", "packing slip", "return label", "order history", "case chronology"),
            ("seasonal staffing", "warehouse relocation", "currency conversion", "supplier delay", "address correction", "catalog migration", "holiday closure", "loyalty update"),
            ("parcel locker", "regional depot", "card statement", "inventory ledger", "dispatch window", "customer survey", "invoice archive", "service calendar"),
        ),
        "banking": (
            ("mobile wallet", "telephone desk", "branch terminal", "secure inbox", "merchant portal", "travel notice", "card replacement", "accessibility line"),
            ("recurring invoice", "family transfer", "hotel deposit", "salary payment", "utility mandate", "merchant dispute", "scheduled rent", "cross-border purchase"),
            ("device receipt", "merchant statement", "support transcript", "settlement file", "payment advice", "account history", "case chronology", "terminal record"),
            ("network maintenance", "travel schedule", "currency conversion", "merchant onboarding", "address update", "card renewal", "weekend staffing", "billing migration"),
            ("regional branch", "payment ledger", "device inventory", "clearing window", "customer survey", "invoice archive", "service calendar", "merchant directory"),
        ),
        "healthcare": (
            ("patient portal", "clinic telephone", "reception desk", "secure inbox", "pharmacy counter", "care coordinator", "accessibility line", "family callback"),
            ("routine follow-up", "prescription renewal", "travel consultation", "appointment change", "caregiver question", "billing inquiry", "records request", "clinic relocation"),
            ("visit summary", "appointment letter", "support transcript", "medication list", "referral notice", "consent record", "case chronology", "service directory"),
            ("seasonal demand", "staff rota", "language support", "equipment delivery", "clinic schedule", "records migration", "transport update", "holiday closure"),
            ("regional clinic", "booking ledger", "equipment inventory", "consultation window", "patient survey", "document archive", "service calendar", "care directory"),
        ),
        "public_admin": (
            ("citizen portal", "municipal telephone", "service counter", "secure inbox", "records office", "community liaison", "accessibility line", "scheduled callback"),
            ("permit renewal", "address amendment", "benefit inquiry", "appointment change", "document request", "registry search", "fee question", "service relocation"),
            ("submission receipt", "appointment letter", "support transcript", "case index", "delivery notice", "registry extract", "case chronology", "service directory"),
            ("seasonal demand", "staff rota", "language support", "archive transfer", "office schedule", "forms migration", "postal update", "holiday closure"),
            ("regional office", "procedure ledger", "document inventory", "review window", "citizen survey", "records archive", "service calendar", "department directory"),
        ),
    }
    groups = profiles[domain]
    selected = [
        values[(family_index >> (index * 3)) % len(values)]
        for index, values in enumerate(groups)
    ]
    channel, situation, document, background, location = selected
    return (
        f"For context only, file {family_index:03d} is assigned to the {mechanism.replace('_', ' ')} review queue. "
        f"It arrived through the {channel} after a {situation}. "
        f"The archive also contains a {document}; unrelated notes mention {background}. "
        f"A separate service record refers to the {location}, a routine queue transfer, "
        "and an earlier contact preference. These background details are not contract evidence."
    )


@lru_cache(maxsize=8192)
def _seed_case(
    domain: str,
    mechanism: str,
    *,
    variant: int,
    seed: int,
    family_index: int = 0,
) -> tuple[BenchmarkCase, str, dict[str, Any], str, str]:
    """Build a repeated-trigger causal family and its contract-compliant control."""

    from autopilot.jev_benchmark.generator import _at, build_seed_trajectory

    contract = _load_causal_contract(domain)
    trajectory = build_seed_trajectory(domain, mechanism, Stratum.CLEAR_POSITIVE)
    observations = list(trajectory.observations)
    clause = next(
        clause
        for clause in contract.clauses
        if (getattr(clause, "x_benchmark", None) or {}).get("mechanism") == mechanism
    )
    benchmark = clause.x_benchmark or {}
    trigger_name = benchmark["trigger_event"]
    trigger = next(
        (item for item in observations if item.kind.value == "event" and item.name == trigger_name),
        None,
    )
    if trigger is None:
        raise GeneralizationPilotIntegrityError(f"{domain}/{mechanism}: seed lacks trigger {trigger_name}")

    profile_index = (family_index + seed % 75) % 75
    trigger_count = 2 + profile_index % 5
    repeat_offset = 15 + profile_index // 5
    repeat_gap = 30 + (profile_index * 7) % 21
    trigger_offsets = [0] + [repeat_offset + index * repeat_gap for index in range(trigger_count - 1)]
    for trigger_index, trigger_time in enumerate(trigger_offsets[1:], start=1):
        observations.append(
            trigger.model_copy(
                update={
                    "ref": f"pilot-repeat-trigger-{trigger_index}",
                    "observed_at": _at(trigger_time),
                }
            )
        )
    trigger_origin = datetime.fromisoformat(trigger.observed_at.replace("Z", "+00:00"))
    expanded: list[TrajectoryObservation] = []
    for item in observations:
        expanded.append(item)
        if item.kind.value != "action" or item.ref.startswith("pilot-repeat-"):
            continue
        action_offset = int(
            (datetime.fromisoformat(item.observed_at.replace("Z", "+00:00")) - trigger_origin).total_seconds()
        )
        if action_offset < 0:
            continue
        for trigger_index, trigger_time in enumerate(trigger_offsets[1:], start=1):
            expanded.append(
                item.model_copy(
                    update={
                        "ref": f"pilot-family-action-{trigger_index}-{item.ref}",
                        "observed_at": _at(trigger_time + action_offset),
                    }
                )
            )
    observations = expanded
    perturbation: dict[str, Any] = {
        "type": "repeated_trigger_with_matched_control",
        "trigger_count": trigger_count,
        "repeat_offsets_seconds": trigger_offsets[1:],
        "repeat_gap_seconds": repeat_gap,
        "variant": variant,
        "family_index": family_index,
        "causal_profile_index": profile_index,
    }
    positive = variant == 0

    if not positive:
        if hasattr(clause, "prohibition"):
            prohibition = clause.prohibition
            original = next(
                (
                    item for item in observations
                    if item.kind.value == "action" and item.name == prohibition.action
                ),
                None,
            )
            if original is None:
                raise GeneralizationPilotIntegrityError(
                    f"{domain}/{mechanism}: positive seed lacks prohibited action {prohibition.action}"
                )
            safe_attributes = dict(original.attributes)
            for key, prohibited_value in prohibition.conditions.items():
                if isinstance(prohibited_value, bool):
                    safe_attributes[key] = not prohibited_value
                elif prohibited_value == "assumed":
                    safe_attributes[key] = "verified"
                else:
                    safe_attributes[key] = f"not_{prohibited_value}"
            observations = [
                item.model_copy(update={"attributes": safe_attributes})
                if item.kind.value == "action" and item.name == prohibition.action
                else item
                for item in observations
            ]
            perturbation["control"] = "prohibited_action_conditions_reversed"
        else:
            requirement = clause.then if hasattr(clause, "then") else clause.requirement
            for action_index, trigger_time in enumerate(trigger_offsets, start=1):
                observations.append(
                    TrajectoryObservation(
                        ref=f"pilot-control-action-{action_index}",
                        kind="action",
                        name=requirement.action,
                        observed_at=_at(trigger_time + 5),
                        attributes=dict(requirement.parameters),
                    )
                )
            perturbation["control"] = "required_action_for_each_trigger"

    observations.sort(key=lambda item: (item.observed_at, item.ref))
    trajectory = trajectory.model_copy(
        update={
            "trajectory_id": f"causal-pilot-{domain}-{mechanism}-s{seed}-f{family_index}-v{variant}",
            "observations": observations,
        }
    )
    evaluation = evaluate_contract(contract, trajectory)
    status = _instrumenting_status(contract, evaluation, mechanism)
    expected = ClauseStatus.failed if positive else ClauseStatus.passed
    if status is not expected:
        raise GeneralizationPilotIntegrityError(
            f"causal pilot {domain}/{mechanism}/v{variant} produced {status.value}, expected {expected.value}"
        )
    stratum = Stratum.CLEAR_POSITIVE if positive else Stratum.CLEAR_NEGATIVE
    label = _label_for(contract, evaluation, mechanism, stratum, boundary_side="just_in_time")
    case = BenchmarkCase(
        case_id=        f"PILOT-CAUSAL-{domain}-{mechanism}-s{seed}-f{family_index:03d}-v{variant}",
        mechanism=mechanism,
        domain=domain,
        stratum=stratum,
        ground_truth=label,
        contract=ContractRef(contract.contract.id, contract.contract.version, clause.id),
        trajectory=trajectory,
        structured_view=_structured_view(domain, stratum, "just_in_time"),
        natural_view=_natural_view(domain, stratum, "just_in_time", None),
        paraphrases=[],
        generation_metadata={"pilot": CAUSAL_PILOT_VERSION, **perturbation},
        secondary_mechanisms=_secondary_mechanisms(contract, evaluation, mechanism),
    )
    context_note = _context_note(domain, mechanism, family_index)
    natural_view = [dict(message) for message in case.natural_view]
    natural_view[0]["text"] = f"{natural_view[0]['text']} {context_note}"
    structured_view = dict(case.structured_view)
    structured_view["user_request"] = f"{structured_view['user_request']} | {context_note}"
    state_text = laya_state_text(build_observable(replace(
        case, structured_view=structured_view, natural_view=natural_view
    )))
    if seed != CAUSAL_PILOT_SEED:
        seed_marker = f"Generation recipe seed {seed}."
        natural_view[0]["text"] = f"{natural_view[0]['text']} {seed_marker}"
        structured_view["user_request"] = f"{structured_view['user_request']} | {seed_marker}"
        state_text = laya_state_text(build_observable(replace(
            case, structured_view=structured_view, natural_view=natural_view
        )))
    causal_program = "repeated-trigger-with-matched-control-v2"
    if mechanism == "goal_override":
        causal_program = "goal-override-trigger-count-without-consequent-v1"
    elif mechanism == "pre_compliance":
        causal_program = "pre-compliance-trigger-approval-sequence-v1"
    elif mechanism in {"constraint_reframing", "assumption_based"}:
        causal_program = f"{mechanism}-unsafe-action-with-repeated-trigger-v1"
    elif mechanism == "escalation_avoidance":
        causal_program = "escalation-avoidance-trigger-deadline-v1"
    fingerprint = _canonical_digest(
        {
            "domain": domain,
            "mechanism": mechanism,
            "causal_program": causal_program,
            "profile_index": profile_index,
            "trigger_count": trigger_count,
            "repeat_offsets_seconds": trigger_offsets[1:],
            "repeat_gap_seconds": repeat_gap,
        }
    )
    perturbation["context_note"] = context_note
    perturbation["generation_seed"] = seed
    case = replace(
        case,
        structured_view=structured_view,
        natural_view=natural_view,
        generation_metadata={"pilot": CAUSAL_PILOT_VERSION, **perturbation},
    )
    return case, state_text, perturbation, fingerprint, status.value


def build_causal_family_pilot(
    *, seed: int = CAUSAL_PILOT_SEED, families_per_domain_mechanism: int = 75
) -> tuple[dict[str, list[CausalCase]], dict[str, Any]]:
    """Build a large exploratory family pool and compare it with working text.

    Each family pairs a repeated-trigger violation with its compliant control,
    adds label-invariant contextual variation, and is assigned atomically to a
    deterministic split. This remains a candidate pool, not a preregistered
    or human-reviewed cohort; the loader opens working files only.
    """

    if type(families_per_domain_mechanism) is not int or not 1 <= families_per_domain_mechanism <= 75:
        raise ValueError("families_per_domain_mechanism must be an integer from 1 through 75")
    working, source_dataset_digest = _load_phase_a_working_cases()
    working_texts = [laya_state_text(build_observable(case)) for case in working]
    working_text_set = set(working_texts)
    working_normalized = {_normalize_text(text) for text in working_texts}
    working_token_sets = [_text_tokens(text) for text in working_texts]
    working_token_index = _build_token_index(working_token_sets)
    working_fingerprints = {_trajectory_fingerprint(case.trajectory) for case in working}
    generated: list[CausalCase] = []
    rejected: list[dict[str, Any]] = []
    max_lexical_similarities: list[float] = []
    targets = {split: {} for split in SPLITS}
    audit_metrics = {
        "exact_text_matches_to_working": 0,
        "normalized_text_matches_to_working": 0,
        "trajectory_fingerprint_matches_to_working": 0,
    }

    family_splits: dict[str, str] = {}
    for mechanism in MECHANISMS:
        family_ids = [
            f"causal-v1:{domain}:{mechanism}:{family_index:03d}"
            for domain in DOMAIN_ORDER
            for family_index in range(families_per_domain_mechanism)
        ]
        ordered_families = sorted(
            family_ids,
            key=lambda family_id: _family_tie(seed, family_id, "family-order"),
        )
        fit_count = (len(ordered_families) * 2) // 5
        selection_count = len(ordered_families) // 5
        for rank, family_id in enumerate(ordered_families):
            family_splits[family_id] = (
                "fit" if rank < fit_count
                else "selection" if rank < fit_count + selection_count
                else "confirmatory"
            )

    for domain in DOMAIN_ORDER:
        for mechanism in MECHANISMS:
            for family_index in range(families_per_domain_mechanism):
                family_id = f"causal-v1:{domain}:{mechanism}:{family_index:03d}"
                for variant in (0, 1):
                    case, text, perturbation, fingerprint, arbiter_status = _seed_case(
                        domain,
                        mechanism,
                        variant=variant,
                        seed=seed,
                        family_index=family_index,
                    )
                    lexical_similarity = _max_jaccard_tokens(
                        _text_tokens(text), working_token_sets, working_token_index
                    )
                    max_lexical_similarities.append(lexical_similarity)
                    collision = _working_collision_reason(
                        text,
                        _trajectory_fingerprint(case.trajectory),
                        exact_texts=working_text_set,
                        normalized_texts=working_normalized,
                        trajectory_fingerprints=working_fingerprints,
                    )
                    if collision:
                        rejected.append({"family_id": family_id, "case_id": case.case_id, "reason": collision})
                        audit_metrics[
                            {
                                "exact_text_match": "exact_text_matches_to_working",
                                "normalized_text_match": "normalized_text_matches_to_working",
                                "trajectory_match": "trajectory_fingerprint_matches_to_working",
                            }[collision]
                        ] += 1
                        continue
                    family_counts = targets[family_splits[family_id]]
                    cell = f"{mechanism}|{case.ground_truth.value}"
                    family_counts[cell] = family_counts.get(cell, 0) + 1
                    generated.append(
                        CausalCase(
                            case_id=case.case_id,
                            family_id=family_id,
                            mechanism=mechanism,
                            domain=domain,
                            label=case.ground_truth.value,
                            variant=f"v{variant}",
                            perturbation=perturbation,
                            trajectory=case.to_dict()["trajectory"],
                            state_text=text,
                            family_fingerprint=fingerprint,
                            source_generation_digest=source_dataset_digest,
                            arbiter_status=arbiter_status,
                            clause_id=case.contract.clause_id,
                        )
                    )

    rejected_families = {item["family_id"] for item in rejected}
    if rejected_families:
        generated = [case for case in generated if case.family_id not in rejected_families]
        family_splits = {family: split for family, split in family_splits.items() if family not in rejected_families}
        targets = {split: {} for split in SPLITS}
        for case in generated:
            counts = targets[family_splits[case.family_id]]
            cell = f"{case.mechanism}|{case.label}"
            counts[cell] = counts.get(cell, 0) + 1

    family_assignment = {case.family_id: family_splits[case.family_id] for case in generated}
    grouped_texts: dict[str, set[str]] = {split: set() for split in SPLITS}
    for case in generated:
        grouped_texts[family_assignment[case.family_id]].add(case.state_text)
    cross_split = [
        text
        for left, right in combinations(SPLITS, 2)
        for text in sorted(grouped_texts[left] & grouped_texts[right])
    ]
    if cross_split:
        raise GeneralizationPilotIntegrityError(f"exact generated text crosses splits: {cross_split[:4]}")
    family_fingerprint_owner: dict[str, str] = {}
    for case in generated:
        split = family_assignment[case.family_id]
        owner = family_fingerprint_owner.setdefault(case.family_fingerprint, split)
        if owner != split:
            raise GeneralizationPilotIntegrityError(
                f"family fingerprint crosses splits: {case.family_fingerprint}"
            )

    similarities = sorted(max_lexical_similarities)
    lexical_threshold = 0.8
    high_similarity_fraction = sum(value >= lexical_threshold for value in similarities) / len(similarities) if similarities else 0.0
    lexical_novelty_qualified = sum(value < lexical_threshold for value in similarities)
    minimum_family_count = 1500
    novelty_sufficient = (
        len({case.family_id for case in generated}) >= minimum_family_count
        and lexical_novelty_qualified >= 32
        and not rejected
    )
    summary = {
        "working_case_count_read": len(working),
        "candidate_cases": len(generated) + len(rejected),
        "accepted_cases": len(generated),
        "rejected_cases": len(rejected),
        "rejections_by_reason": dict(Counter(item["reason"] for item in rejected)),
        "family_count": len({case.family_id for case in generated}),
        "accepted_families_by_split": {
            split: len({case.family_id for case in generated if family_assignment[case.family_id] == split})
            for split in SPLITS
        },
        "accepted_by_split": {
            split: sum(1 for case in generated if family_assignment[case.family_id] == split)
            for split in SPLITS
        },
        "accepted_by_mechanism_label": targets,
        **audit_metrics,
        "max_jaccard_similarity_vs_working": {
            "tokenizer": "casefolded alphanumeric token sets; Jaccard against nearest working input",
            "min": min(similarities, default=0.0),
            "median": (similarities[(len(similarities) - 1) // 2] + similarities[len(similarities) // 2]) / 2 if similarities else 0.0,
            "max": max(similarities, default=0.0),
            "fraction_ge_0_8": high_similarity_fraction,
        },
        "novelty_gate": {
            "minimum_families": minimum_family_count,
            "max_jaccard_threshold": lexical_threshold,
            "required_cases_below_similarity_threshold": 32,
            "lexically_qualified_cases": lexical_novelty_qualified,
            "sufficient": False,
            "candidate_pool_passes_numeric_screen": novelty_sufficient,
            "verdict": "insufficient_for_generalization_cohort" if not novelty_sufficient else "numeric_screen_passes_human_review_pending",
        },
        "source_phase_a_dataset_digest": source_dataset_digest,
        "holdout_accessed": False,
        "holdout_case_count_read": 0,
        "human_review": "pending; exploratory candidate pool only",
        "not_training_or_benchmark_evidence": True,
        "synthetic_candidate_pool_only": True,
        "human_reviewed": False,
    }
    splits: dict[str, list[CausalCase]] = {
        split: sorted(
            (case for case in generated if family_assignment[case.family_id] == split),
            key=lambda case: (case.family_id, case.case_id),
        )
        for split in SPLITS
    }
    payload = {split: [case.to_dict() for case in rows] for split, rows in splits.items()}
    manifest = {
        "pilot_version": CAUSAL_PILOT_VERSION,
        "seed": seed,
        "families_per_domain_mechanism": families_per_domain_mechanism,
        "synthetic_contract_labelled": True,
        "not_training_or_benchmark_evidence": True,
        "synthetic_candidate_pool_only": True,
        "human_reviewed": False,
        "source_phase_a_dataset_digest": source_dataset_digest,
        "policy": "working-only comparison; phase-A holdout neither read nor used",
        "audit": summary,
        "family_assignment": family_assignment,
        "family_splits": family_splits,
        "family_fingerprints": {
            case.family_id: case.family_fingerprint
            for case in generated if case.variant == "v0"
        },
        "split_digests": {split: _canonical_digest(payload[split]) for split in SPLITS},
        "pilot_digest": _canonical_digest(payload),
    }
    review_sample = _make_blind_review_sample(splits, seed=seed)
    manifest["review_sample_digest"] = _canonical_digest(review_sample)
    manifest["review_sample_count"] = len(review_sample)
    manifest["review_sample_policy"] = (
        "deterministic 10-percent minimum-one sample in each domain/mechanism cell; "
        "labels, split, arbiter status, and training eligibility omitted"
    )
    return splits, manifest


def _make_blind_review_sample(
    splits: dict[str, list[CausalCase]], *, seed: int
) -> list[dict[str, Any]]:
    by_cell: dict[tuple[str, str], dict[str, list[CausalCase]]] = defaultdict(lambda: defaultdict(list))
    for cases in splits.values():
        for case in cases:
            by_cell[(case.domain, case.mechanism)][case.family_id].append(case)
    sample: list[dict[str, Any]] = []
    for (domain, mechanism), families in sorted(by_cell.items()):
        family_ids = sorted(families)
        sample_size = max(1, (len(family_ids) + 9) // 10)
        selected = sorted(
            family_ids,
            key=lambda family_id: _family_tie(seed, family_id, "human-review-sample"),
        )[:sample_size]
        for family_id in selected:
            cases = families[family_id]
            if {case.variant for case in cases} != {"v0", "v1"}:
                raise GeneralizationPilotIntegrityError(
                    f"review packet requires a complete causal/control pair: {family_id}"
                )
            ordered_cases = sorted(
                cases,
                key=lambda case: _family_tie(seed, case.case_id, "blind-variant-order"),
            )
            sample.append(
                {
                    "review_family_ref": hashlib.sha256(
                        f"{seed}:{family_id}:blind-review".encode("utf-8")
                    ).hexdigest()[:20],
                    "domain": domain,
                    "mechanism": mechanism,
                    "contract_clause_id": ordered_cases[0].clause_id,
                    "cases": [
                        {
                            "review_case_ref": hashlib.sha256(
                                f"{seed}:{family_id}:{case.case_id}:blind-review".encode("utf-8")
                            ).hexdigest()[:20],
                            "state_text": case.state_text,
                            "trajectory": case.trajectory,
                        }
                        for case in ordered_cases
                    ],
                    "reviewer_decision": {
                        "causal_variants_distinct_from_working_patterns": "",
                        "near_duplicate_detected": "",
                        "contract_label_consistent": "",
                        "notes": "",
                    },
                }
            )
    return sample


def default_pilot_output(root: Path) -> Path:
    """Return the dedicated temporary directory for the plumbing pilot."""

    return root / ".tmp" / "laya-generalization-plumbing-pilot-v0_1"


def default_causal_output(root: Path) -> Path:
    """Return the dedicated temporary directory for the causal novelty pilot."""

    return root / ".tmp" / "laya-causal-family-candidate-pool-v0_2"


def validate_pilot_output_path(root: Path, output_dir: Path) -> Path:
    """Restrict pilot writes to this checkout's .tmp tree, including symlinks."""

    tmp_root = (root.resolve() / ".tmp").resolve()
    resolved_output = output_dir.resolve()
    try:
        resolved_output.relative_to(tmp_root)
    except ValueError as exc:
        raise GeneralizationPilotIntegrityError(
            f"pilot output must remain under {tmp_root}: {resolved_output}"
        ) from exc
    if resolved_output == tmp_root:
        raise GeneralizationPilotIntegrityError("pilot output must be a dedicated child under .tmp")
    return resolved_output


def build_synthetic_pilot(
    *, seed: int = PILOT_SEED
) -> tuple[dict[str, list[PilotCase]], dict[str, Any]]:
    """Create a tiny fixture cohort (one family/cell/split, two variants each)."""

    cases: list[PilotCase] = []
    targets: dict[str, dict[str, int]] = {split: {} for split in SPLITS}
    labels = ("present", "absent", "uncertain")
    for mechanism in MECHANISMS:
        for label in labels:
            cell = f"{mechanism}|{label}"
            for split in SPLITS:
                targets[split][cell] = 1
                family_id = f"fixture:{mechanism}:{label}:{split}"
                for variant_index in (0, 1):
                    case_id = f"PILOT-{mechanism}-{label}-{split}-{variant_index}"
                    text = (
                        f"SYNTHETIC PLUMBING FIXTURE only; mechanism={mechanism}; "
                        f"label={label}; family={family_id}. "
                        "Not a benchmark trajectory or model-evaluation input."
                    )
                    cases.append(
                        PilotCase(
                            case_id=case_id,
                            family_id=family_id,
                            mechanism=mechanism,
                            label=label,
                            variant_index=variant_index,
                            state_text=text,
                        )
                    )
    assignment = assign_family_splits(cases, targets, seed=seed)
    integrity = validate_split_integrity(cases, assignment)
    split_cases = {
        split: sorted(
            (case for case in cases if assignment[case.family_id] == split),
            key=lambda case: case.case_id,
        )
        for split in SPLITS
    }
    payloads = {
        split: [case.to_dict() for case in split_cases[split]]
        for split in SPLITS
    }
    manifest = {
        "pilot_version": PILOT_VERSION,
        "seed": seed,
        "synthetic_fixture_only": True,
        "not_training_or_benchmark_evidence": True,
        "label_source": "explicit synthetic plumbing fixture; no arbiter or phase-A case read",
        "source_holdout_accessed": False,
        "family_assignment": assignment,
        "targets_by_split": targets,
        "integrity": integrity,
        "usage_guard": "fixtures_only_not_for_training_evaluation_or_benchmark_claims",
        "split_digests": {
            split: _canonical_digest(payloads[split]) for split in SPLITS
        },
        "pilot_digest": _canonical_digest(payloads),
    }
    return split_cases, manifest


def write_synthetic_pilot(
    splits: dict[str, list[PilotCase]], manifest: dict[str, Any], output_dir: Path
) -> dict[str, Path]:
    """Write the plumbing fixture pilot once and refuse modifications."""

    if set(splits) != set(SPLITS):
        raise GeneralizationPilotIntegrityError(f"pilot splits must be exactly {SPLITS}")
    assignment = manifest.get("family_assignment")
    all_cases = [case for split in SPLITS for case in splits[split]]
    actual_assignment: dict[str, str] = {}
    for split in SPLITS:
        for case in splits[split]:
            prior = actual_assignment.setdefault(case.family_id, split)
            if prior != split:
                raise GeneralizationPilotIntegrityError(
                    f"family {case.family_id} appears in both {prior} and {split}"
                )
    if actual_assignment != assignment:
        raise GeneralizationPilotIntegrityError("manifest family assignment does not match payload")
    validate_split_integrity(all_cases, actual_assignment)

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        f"families_{split}.jsonl": "".join(
            json.dumps(case.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for case in sorted(splits[split], key=lambda item: item.case_id)
        )
        for split in SPLITS
    }
    split_payloads = {
        split: [case.to_dict() for case in sorted(splits[split], key=lambda item: item.case_id)]
        for split in SPLITS
    }
    expected_split_digests = {
        split: _canonical_digest(split_payloads[split]) for split in SPLITS
    }
    if manifest.get("split_digests") != expected_split_digests:
        raise GeneralizationPilotIntegrityError("manifest split digests do not match pilot payload")
    if manifest.get("pilot_digest") != _canonical_digest(split_payloads):
        raise GeneralizationPilotIntegrityError("manifest pilot digest does not match pilot payload")
    if manifest.get("synthetic_fixture_only") is not True:
        raise GeneralizationPilotIntegrityError("pilot manifest must declare fixture-only data")
    if manifest.get("not_training_or_benchmark_evidence") is not True:
        raise GeneralizationPilotIntegrityError("pilot manifest must forbid training/evidence use")
    if manifest.get("source_holdout_accessed") is not False:
        raise GeneralizationPilotIntegrityError("pilot manifest must attest no holdout access")
    payloads["manifest.json"] = json.dumps(
        manifest, indent=2, sort_keys=True, ensure_ascii=False
    ) + "\n"
    paths = {name: output_dir / name for name in payloads}
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite pilot artifacts: {existing}")
    for name, text in payloads.items():
        paths[name].write_text(text, encoding="utf-8")
    return paths


def write_causal_family_pilot(
    splits: dict[str, list[CausalCase]], manifest: dict[str, Any], output_dir: Path
) -> dict[str, Path]:
    """Persist the causal-family pilot once after validating all integrity claims."""

    if set(splits) != set(SPLITS):
        raise GeneralizationPilotIntegrityError(f"pilot splits must be exactly {SPLITS}")
    if manifest.get("synthetic_contract_labelled") is not True:
        raise GeneralizationPilotIntegrityError("pilot manifest must declare contract-labelled synthetic data")
    if manifest.get("not_training_or_benchmark_evidence") is not True:
        raise GeneralizationPilotIntegrityError("pilot manifest must forbid training/evidence use")
    if manifest.get("synthetic_candidate_pool_only") is not True or manifest.get("human_reviewed") is not False:
        raise GeneralizationPilotIntegrityError("causal pilot must remain an unreviewed candidate pool")
    audit = manifest.get("audit")
    if not isinstance(audit, dict) or audit.get("holdout_accessed") is not False:
        raise GeneralizationPilotIntegrityError("pilot manifest must attest no holdout access")
    if audit.get("holdout_case_count_read") != 0:
        raise GeneralizationPilotIntegrityError("pilot manifest must attest zero holdout cases read")

    family_assignment = manifest.get("family_assignment")
    family_fingerprints = manifest.get("family_fingerprints")
    manifest_family_splits = manifest.get("family_splits")
    if not isinstance(family_assignment, dict) or not isinstance(family_fingerprints, dict) or not isinstance(manifest_family_splits, dict):
        raise GeneralizationPilotIntegrityError("manifest family metadata must be mappings")
    assignment_by_fingerprint: dict[str, str] = {}
    actual_assignment: dict[str, str] = {}
    actual_fingerprints: dict[str, str] = {}
    for split in SPLITS:
        for case in splits[split]:
            if not isinstance(case, CausalCase):
                raise GeneralizationPilotIntegrityError(f"invalid causal pilot case in {split}")
            prior_split = actual_assignment.setdefault(case.family_id, split)
            if prior_split != split:
                raise GeneralizationPilotIntegrityError(
                    f"family {case.family_id} appears in both {prior_split} and {split}"
                )
            prior_fingerprint = actual_fingerprints.setdefault(
                case.family_id, case.family_fingerprint
            )
            if prior_fingerprint != case.family_fingerprint:
                raise GeneralizationPilotIntegrityError(
                    f"family fingerprint crosses inconsistent cases within {case.family_id}"
                )
            if not case.family_fingerprint:
                raise GeneralizationPilotIntegrityError(f"{case.case_id}: empty family fingerprint")
            fingerprint_owner = assignment_by_fingerprint.setdefault(case.family_fingerprint, split)
            if fingerprint_owner != split:
                raise GeneralizationPilotIntegrityError("family fingerprint crosses split boundaries")
    if family_assignment != actual_assignment:
        raise GeneralizationPilotIntegrityError("manifest family assignment does not match payload")
    if family_fingerprints != actual_fingerprints:
        raise GeneralizationPilotIntegrityError("manifest family fingerprints do not match payload")
    if {family: split for family, split in manifest_family_splits.items() if family in actual_assignment} != actual_assignment:
        raise GeneralizationPilotIntegrityError("manifest family split assignments do not match payload")

    split_payloads = {
        split: [case.to_dict() for case in sorted(splits[split], key=lambda item: (item.family_id, item.case_id))]
        for split in SPLITS
    }
    review_sample = _make_blind_review_sample(splits, seed=manifest.get("seed"))
    if manifest.get("synthetic_candidate_pool_only") is not True or manifest.get("human_reviewed") is not False:
        raise GeneralizationPilotIntegrityError("causal pilot must remain an unreviewed candidate pool")
    if manifest.get("review_sample_digest") != _canonical_digest(review_sample):
        raise GeneralizationPilotIntegrityError("review sample digest does not match candidate families")
    if manifest.get("review_sample_count") != len(review_sample):
        raise GeneralizationPilotIntegrityError("review sample count does not match candidate families")
    expected_split_digests = {
        split: _canonical_digest(split_payloads[split]) for split in SPLITS
    }
    if manifest.get("split_digests") != expected_split_digests:
        raise GeneralizationPilotIntegrityError("manifest split digests do not match pilot payload")
    if manifest.get("pilot_digest") != _canonical_digest(split_payloads):
        raise GeneralizationPilotIntegrityError("manifest pilot digest does not match pilot payload")

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        f"families_{split}.jsonl": "".join(
            json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
            for case in split_payloads[split]
        )
        for split in SPLITS
    }
    payloads["review_sample.jsonl"] = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in review_sample
    )
    payloads["manifest.json"] = json.dumps(
        manifest, indent=2, sort_keys=True, ensure_ascii=False
    ) + "\n"
    paths = {name: output_dir / name for name in payloads}
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite pilot artifacts: {existing}")
    for name, text in payloads.items():
        paths[name].write_text(text, encoding="utf-8")
    return paths
