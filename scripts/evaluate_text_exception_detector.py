"""Score the transparent textual exception screen on a synthetic blind set."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autopilot.textual_exception_detector import detect_textual_exception_request  # noqa: E402

STIMULI_PATH = REPO_ROOT / "hackatons/meta/scenarios/text_exception_stimuli_v0.1.json"
ORACLE_PATH = REPO_ROOT / "hackatons/meta/scenarios/text_exception_oracle_v0.1.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evaluate(stimuli_path: Path = STIMULI_PATH, oracle_path: Path = ORACLE_PATH) -> dict[str, Any]:
    """Run detector first using only stimuli, then load oracle solely for scoring."""

    stimuli_bytes = stimuli_path.read_bytes()
    stimuli_doc = json.loads(stimuli_bytes)
    predictions = {
        item["id"]: detect_textual_exception_request(item["text"])
        for item in stimuli_doc["stimuli"]
    }

    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    if oracle.get("stimulus_sha256") != _sha256(stimuli_bytes):
        raise ValueError("oracle stimulus_sha256 does not match the evaluated blind stimuli")
    expected = {item["id"]: item["exception_request"] for item in oracle["labels"]}
    if set(predictions) != set(expected):
        raise ValueError("stimulus and oracle IDs differ")

    tp = fp = tn = fn = 0
    cases = []
    for item in stimuli_doc["stimuli"]:
        predicted = predictions[item["id"]]
        truth = expected[item["id"]]
        if predicted.triggered and truth:
            tp += 1
        elif predicted.triggered and not truth:
            fp += 1
        elif not predicted.triggered and not truth:
            tn += 1
        else:
            fn += 1
        cases.append(
            {
                "id": item["id"],
                "predicted_exception_request": predicted.triggered,
                "reference_exception_request": truth,
                "rule_ids": list(predicted.rule_ids),
                "error": "false_positive" if predicted.triggered and not truth else (
                    "false_negative" if not predicted.triggered and truth else None
                ),
            }
        )

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {
        "schema_version": "0.1",
        "suite_id": stimuli_doc["suite_id"],
        "evaluation_kind": "offline_synthetic_lexical_screen_only",
        "detector": "autopilot.textual_exception_detector",
        "stimuli_sha256": _sha256(stimuli_bytes),
        "totals": {
            "n": len(cases),
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
            "precision": precision,
            "recall": recall,
            "accuracy": (tp + tn) / len(cases) if cases else None,
        },
        "interpretation": (
            "Synthetic rule-screen check only. Labels are human-authored reference fixtures, "
            "not independently adjudicated data. Results do not establish semantic understanding, "
            "production performance, or generalization. Authorization validity remains a separate "
            "trusted input; a text hit alone does not authorize or block a real-world operation."
        ),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stimuli", type=Path, default=STIMULI_PATH)
    parser.add_argument("--oracle", type=Path, default=ORACLE_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = evaluate(args.stimuli, args.oracle)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
