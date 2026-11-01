#!/usr/bin/env python3
"""Offline verifier for the preregistered blind guard evaluation freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_eval_freeze_v0.1.json"
STIMULI_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_eval_stimuli_v0.1.json"
ORACLE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_eval_oracle_v0.1.json"
PREREG_PATH = ROOT / "docs/meta-guard-balanced-eval-prereg-v0.1.md"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify() -> dict[str, Any]:
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    stimuli = json.loads(STIMULI_PATH.read_text(encoding="utf-8"))
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    prereg_text = PREREG_PATH.read_text(encoding="utf-8")

    if freeze.get("execution_authorized") is not False:
        raise ValueError("freeze must leave live execution unauthorized")
    if freeze.get("status") != "frozen_before_live_execution":
        raise ValueError("freeze status must be frozen_before_live_execution")
    if stimuli.get("suite_id") != oracle.get("suite_id") or stimuli.get("suite_id") != freeze.get("suite_id"):
        raise ValueError("suite IDs differ")
    items = stimuli.get("stimuli", [])
    labels = oracle.get("labels", [])
    ids = [item.get("id") for item in items]
    label_ids = [label.get("id") for label in labels]
    if len(items) != 12 or len(set(ids)) != 12:
        raise ValueError("stimuli must contain twelve unique cases")
    if set(ids) != set(label_ids) or len(label_ids) != 12:
        raise ValueError("oracle labels must match the twelve stimulus IDs exactly")
    if any(any(key in item for key in ("class", "label", "pressure_type", "exception_request", "authorization")) for item in items):
        raise ValueError("stimuli contain a prohibited label or trusted-state field")
    if any(not isinstance(item.get("text"), str) or not item["text"].strip() for item in items):
        raise ValueError("every stimulus must have nonempty text")
    counts = {
        "adversarial_exception": sum(label.get("class") == "adversarial_exception" for label in labels),
        "benign_control": sum(label.get("class") == "benign_control" for label in labels),
    }
    if counts != {"adversarial_exception": 6, "benign_control": 6}:
        raise ValueError("oracle must be balanced six adversarial / six benign")
    if any(label.get("exception_request") is not (label.get("class") == "adversarial_exception") for label in labels):
        raise ValueError("oracle classes and exception_request values disagree")
    if oracle.get("oracle_role") != "scoring_only_never_sent_to_agent":
        raise ValueError("oracle role must explicitly prohibit sending it to the agent")
    if oracle.get("stimulus_sha256") != sha256(STIMULI_PATH):
        raise ValueError("oracle stimulus hash does not match the frozen stimulus file")
    if "24 solicitudes" not in prereg_text or "no ejecutar todavía" not in prereg_text.lower():
        raise ValueError("preregistration must document the 24-call ceiling and pending execution")

    checked = []
    for artifact in freeze.get("artifacts", []):
        relative = PurePosixPath(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in artifact["path"]:
            raise ValueError(f"unsafe frozen path: {artifact['path']}")
        path = ROOT.joinpath(*relative.parts)
        actual = sha256(path)
        if actual != artifact.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {artifact['path']}")
        checked.append({"path": artifact["path"], "sha256": actual})

    return {
        "freeze_id": freeze["freeze_id"],
        "suite_id": stimuli["suite_id"],
        "stimuli": len(items),
        "label_counts": counts,
        "stimulus_and_oracle_separated": True,
        "execution_authorized": False,
        "verified_artifacts": checked,
        "live_calls_made": False,
    }


def main() -> int:
    print(json.dumps(verify(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
