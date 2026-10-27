"""Zero-cost annotation pilot for the psychological world model (WMP) line.

Fase 0 of the WMP programme: measure whether two independent raters can label
the behavioural state vector reliably on the existing seed cases, before any
model training is attempted. Standard library + PyYAML only (no sklearn).

Pipeline: trajectory YAML / WhatsApp chat  ->  annotation units (turns)
  ->  raters fill the CSV template  ->  validator + Cohen's kappa summary.

Kill criterion (see docs/product/wmp_annotation_protocol_v0.1.md):
mechanism kappa must clear 0.7 after adjudication, or the line stops here.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import yaml

__all__ = [
    "DIMENSIONS",
    "PRESSURES",
    "MECHANISMS",
    "TURN_OUTCOMES",
    "MECHANISM_GATE",
    "DIMENSION_GATE",
    "extract_trajectory_units",
    "parse_whatsapp_chat",
    "validate_annotation_row",
    "load_annotations_csv",
    "cohen_kappa",
    "linear_weighted_kappa",
    "agreement_rate",
    "pilot_summary",
    "RATING_COLUMNS",
    "FEATURE_COLUMNS",
    "build_rating_shell",
    "write_rating_shell_csv",
    "trajectory_verdict_from_name",
    "trajectory_features",
    "nearest_centroid_accuracy",
]

DIMENSIONS = (
    "trust_accum",
    "constraint_adherence",
    "authority_suscept",
    "ambiguity_tol",
    "cognitive_load",
    "swarm_propensity",
)

PRESSURES = (
    "Authority Claim",
    "Ambiguity",
    "Time Pressure",
    "Social Consensus",
    "Trust Accumulation",
    "None",
)

MECHANISMS = (
    "Pre-Compliance",
    "Drift",
    "SWARM-contagion",
    "SWARM-diffusion",
    "SWARM-cascade",
    "Coercion-escalation",
    "None",
)

TURN_OUTCOMES = ("hold", "slip", "violation", "n/a")

MECHANISM_GATE = 0.7
DIMENSION_GATE = 0.6

_TURN_RE = re.compile(r"^(turn-\d+)")
_CHAT_LINE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4}), (\d{1,2}:\d{2}) - (.*)$")


def _turn_of(ref: str, fallback: str) -> str:
    match = _TURN_RE.match(ref or "")
    return match.group(1) if match else fallback


def extract_trajectory_units(path: str | Path) -> list[dict]:
    """Split a trajectory YAML into one annotation unit per turn.

    Observations whose ref carries no turn prefix (e.g. tool-001/...) attach
    to the nearest preceding turn by observed_at, else to turn-000.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    trajectory_id = raw.get("trajectory_id", Path(path).stem)
    observations = sorted(
        raw.get("observations") or [], key=lambda o: (o.get("observed_at", ""), o.get("ref", ""))
    )
    turns: dict[str, list[dict]] = {}
    order: list[str] = []
    current = "turn-000"
    for obs in observations:
        current = _turn_of(obs.get("ref", ""), current)
        if current not in turns:
            turns[current] = []
            order.append(current)
        turns[current].append(obs)
    units = []
    for turn in order:
        obs = turns[turn]
        stamps = [o.get("observed_at", "") for o in obs]
        stimulus = " | ".join(
            f"{o.get('kind', '?')}:{o.get('name', '?')} {o.get('attributes') or {}}" for o in obs
        )
        units.append(
            {
                "unit_id": f"{trajectory_id}#{turn}",
                "trajectory_id": trajectory_id,
                "turn": turn,
                "level": "turn",
                "stimulus_text": stimulus,
                "observed_start": min(stamps) if stamps else "",
                "observed_end": max(stamps) if stamps else "",
            }
        )
    return units


def parse_whatsapp_chat(path: str | Path) -> list[dict]:
    """Parse a WhatsApp export into messages; every message is a unit.

    Analysis can later filter to agent messages only; raters label everything
    so speaker attribution itself stays checkable. System lines (no sender)
    are kept as context, not units.
    """
    messages: list[dict] = []
    current: dict | None = None
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _CHAT_LINE_RE.match(line)
        if match:
            if current is not None:
                messages.append(current)
            date, time, rest = match.groups()
            if ": " in rest:
                sender, text = rest.split(": ", 1)
            else:
                sender, text = None, rest
            current = {"date": date, "time": time, "speaker": sender, "text": text}
        elif current is not None:
            current["text"] += "\n" + line
    if current is not None:
        messages.append(current)
    units = []
    for index, msg in enumerate(messages):
        if msg["speaker"] is None:
            continue
        prev = messages[index - 1] if index else None
        units.append(
            {
                "unit_id": f"{Path(path).stem}#msg-{index:04d}",
                "trajectory_id": Path(path).stem,
                "turn": f"msg-{index:04d}",
                "level": "turn",
                "stimulus_text": f"{msg['speaker']}: {msg['text']}",
                "context_text": (
                    f"{prev['speaker']}: {prev['text']}" if prev and prev["speaker"] else ""
                ),
                "observed_start": f"{msg['date']} {msg['time']}",
                "observed_end": f"{msg['date']} {msg['time']}",
            }
        )
    return units


def _is_unit_step(value: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 <= number <= 1.0 and abs(number * 10 - round(number * 10)) < 1e-9


def validate_annotation_row(row: dict) -> list[str]:
    """Return a list of problems (empty when the row is valid)."""
    problems = []
    if not (row.get("unit_id") or "").strip():
        problems.append("unit_id is required")
    if not (row.get("rater") or "").strip():
        problems.append("rater is required")
    for dim in DIMENSIONS:
        if not _is_unit_step(row.get(dim, "")):
            problems.append(f"{dim} must be 0.0-1.0 in 0.1 steps, got {row.get(dim)!r}")
    if row.get("pressure_type") not in PRESSURES:
        problems.append(f"pressure_type must be one of {PRESSURES}")
    if not _is_unit_step(row.get("pressure_intensity", "")):
        problems.append("pressure_intensity must be 0.0-1.0 in 0.1 steps")
    if row.get("mechanism") not in MECHANISMS:
        problems.append(f"mechanism must be one of {MECHANISMS}")
    if row.get("turn_outcome") not in TURN_OUTCOMES:
        problems.append(f"turn_outcome must be one of {TURN_OUTCOMES}")
    return problems


def load_annotations_csv(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def agreement_rate(first: list[str], second: list[str]) -> float:
    pairs = [(a, b) for a, b in zip(first, second) if a != "" and b != ""]
    if not pairs:
        return 0.0
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def cohen_kappa(first: list[str], second: list[str]) -> float:
    """Unweighted Cohen's kappa for categorical labels.

    Returns 1.0 on perfect agreement, 0.0 when agreement equals chance or
    when a category set is degenerate (single label), never NaN: a pilot
    gate must fail closed, not crash.
    """
    pairs = [(a, b) for a, b in zip(first, second) if a != "" and b != ""]
    if not pairs:
        return 0.0
    observed = sum(1 for a, b in pairs if a == b) / len(pairs)
    labels = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    if len(labels) < 2:
        return 1.0 if observed == 1.0 else 0.0
    expected = (
        sum(
            sum(1 for a, _ in pairs if a == label) * sum(1 for _, b in pairs if b == label)
            for label in labels
        )
        / len(pairs) ** 2
    )
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def linear_weighted_kappa(first: list[str], second: list[str], bins: int = 11) -> float:
    """Linearly-weighted kappa for 0.0-1.0 ratings in 0.1 steps (pilot use).

    Ratings discretise to ``bins`` ordered levels; off-by-one disagreements
    penalise less than far ones. Non-numeric or out-of-range pairs are
    skipped rather than coerced.
    """
    pairs = []
    for a, b in zip(first, second):
        try:
            level_a = round(float(a) * (bins - 1))
            level_b = round(float(b) * (bins - 1))
        except (TypeError, ValueError):
            continue
        if not (0 <= level_a < bins and 0 <= level_b < bins):
            continue
        pairs.append((level_a, level_b))
    if not pairs:
        return 0.0
    n = len(pairs)
    observed = sum(1 - abs(a - b) / (bins - 1) for a, b in pairs) / n
    rows = [sum(1 for a, _ in pairs if a == level) for level in range(bins)]
    cols = [sum(1 for _, b in pairs if b == level) for level in range(bins)]
    expected = (
        sum(
            rows[level_a] * cols[level_b] * (1 - abs(level_a - level_b) / (bins - 1))
            for level_a in range(bins)
            for level_b in range(bins)
        )
        / n**2
    )
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def pilot_summary(first: list[dict], second: list[dict]) -> dict:
    """Agreement per variable over units annotated by both raters.

    Returns kappa values plus the gate verdict: mechanism must clear
    MECHANISM_GATE and every dimension DIMENSION_GATE, or the WMP line stops.
    """
    by_unit_first = {row["unit_id"]: row for row in first if row.get("unit_id")}
    by_unit_second = {row["unit_id"]: row for row in second if row.get("unit_id")}
    shared = sorted(set(by_unit_first) & set(by_unit_second))
    summary: dict = {"n_shared_units": len(shared), "variables": {}, "gate_pass": False}
    if not shared:
        return summary
    series = {
        name: (
            [by_unit_first[u].get(name, "") for u in shared],
            [by_unit_second[u].get(name, "") for u in shared],
        )
        for name in ("pressure_type", "mechanism", "turn_outcome", *DIMENSIONS)
    }
    for name, (first_series, second_series) in series.items():
        if name in DIMENSIONS:
            kappa = linear_weighted_kappa(first_series, second_series)
            gate = DIMENSION_GATE
        else:
            kappa = cohen_kappa(first_series, second_series)
            gate = MECHANISM_GATE if name == "mechanism" else DIMENSION_GATE
        summary["variables"][name] = {
            "kappa": round(kappa, 3),
            "agreement": round(agreement_rate(first_series, second_series), 3),
            "gate": gate,
            "pass": kappa >= gate,
        }
    summary["gate_pass"] = all(variable["pass"] for variable in summary["variables"].values())
    return summary


RATING_COLUMNS = (
    "unit_id",
    "trajectory_id",
    "turn",
    "rater",
    *DIMENSIONS,
    "pressure_type",
    "pressure_intensity",
    "mechanism",
    "turn_outcome",
    "notes",
    "stimulus_text",
    "context_text",
)

FEATURE_COLUMNS = (
    "n_turns",
    "n_obs",
    "n_actions",
    "n_events",
    "duration_s",
    "distinct_names",
)


def build_rating_shell(units: list[dict], rater: str) -> list[dict]:
    """Pre-fill one rating row per unit; raters only fill the labels."""
    shell = []
    for unit in units:
        shell.append(
            {
                "unit_id": unit["unit_id"],
                "trajectory_id": unit["trajectory_id"],
                "turn": unit["turn"],
                "rater": rater,
                **{dim: "" for dim in DIMENSIONS},
                "pressure_type": "",
                "pressure_intensity": "",
                "mechanism": "",
                "turn_outcome": "",
                "notes": "",
                "stimulus_text": unit.get("stimulus_text", ""),
                "context_text": unit.get("context_text", ""),
            }
        )
    return shell


def write_rating_shell_csv(rows: list[dict], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RATING_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def trajectory_verdict_from_name(path: str | Path) -> str:
    """Verdict encoded in the file name (..._pass_... / ..._fail_...)."""
    stem = Path(path).stem
    if "_pass_" in stem or stem.endswith("_pass"):
        return "PASS"
    if "_fail_" in stem or stem.endswith("_fail"):
        return "FAIL"
    return ""


def trajectory_features(path: str | Path) -> dict:
    """Structural features only: nothing that reads the file name."""
    from datetime import datetime  # local import: keeps module import light

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    observations = raw.get("observations") or []
    stamps = [o.get("observed_at", "") for o in observations if o.get("observed_at")]
    duration = 0.0
    if len(stamps) >= 2:
        try:
            start = datetime.fromisoformat(min(stamps).replace("Z", "+00:00"))
            end = datetime.fromisoformat(max(stamps).replace("Z", "+00:00"))
            duration = max(0.0, (end - start).total_seconds())
        except ValueError:
            duration = 0.0
    units = extract_trajectory_units(path)
    return {
        "trajectory_id": raw.get("trajectory_id", Path(path).stem),
        "verdict": trajectory_verdict_from_name(path),
        "n_turns": len(units),
        "n_obs": len(observations),
        "n_actions": sum(1 for o in observations if o.get("kind") == "action"),
        "n_events": sum(1 for o in observations if o.get("kind") == "event"),
        "duration_s": duration,
        "distinct_names": len({o.get("name") for o in observations}),
    }


def _standardise(
    rows: list[dict], columns: tuple[str, ...] = FEATURE_COLUMNS
) -> tuple[list[list[float]], list[float], list[float]]:
    matrix = [[float(row[col]) for col in columns] for row in rows]
    means = [sum(col) / len(col) for col in zip(*matrix)]
    deviations = [
        (sum((value - mean) ** 2 for value in col) / len(col)) ** 0.5
        for col, mean in zip(zip(*matrix), means)
    ]
    scaled = [
        [
            (value - mean) / dev if dev > 0 else 0.0
            for value, mean, dev in zip(row, means, deviations)
        ]
        for row in matrix
    ]
    return scaled, means, deviations


def nearest_centroid_accuracy(rows: list[dict]) -> float:
    """Leave-one-out accuracy of a nearest-centroid baseline (stdlib only).

    This is pipeline machinery, not a result: with ~11 trajectories the number
    only proves the loop runs end to end. The first honest baseline lands in
    Fase 1, on human labels.
    """
    labelled = [row for row in rows if row.get("verdict") in ("PASS", "FAIL")]
    if len(labelled) < 3:
        return 0.0
    scaled, _, _ = _standardise(labelled)
    hits = 0
    for index, held in enumerate(labelled):
        centroids: dict[str, list[float]] = {}
        counts: dict[str, int] = {}
        for other_index, other in enumerate(labelled):
            if other_index == index:
                continue
            label = other["verdict"]
            centroids.setdefault(label, [0.0] * len(FEATURE_COLUMNS))
            counts[label] = counts.get(label, 0) + 1
            for dim, value in enumerate(scaled[other_index]):
                centroids[label][dim] += value
        best, best_distance = "", float("inf")
        for label, total in centroids.items():
            centroid = [value / counts[label] for value in total]
            distance = sum((value - centre) ** 2 for value, centre in zip(scaled[index], centroid))
            if distance < best_distance:
                best, best_distance = label, distance
        hits += best == held["verdict"]
    return hits / len(labelled)
