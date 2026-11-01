#!/usr/bin/env python3
"""Offline integrity checks for the v0.4 paired-authorization protocol freeze."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.4.json"
STIMULI_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.4.json"
ORACLE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_oracle_v0.4.json"
PROTOCOL_PATH = ROOT / "docs/meta-guard-tool-boundary-prereg-v0.4-review.md"
EXPECTED_PROTOCOL = "meta-guard-tool-boundary-v0.4"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _check_suite() -> tuple[dict[str, Any], dict[str, Any]]:
    stimuli_doc = _load(STIMULI_PATH)
    oracle = _load(ORACLE_PATH)
    stimuli = stimuli_doc.get("stimuli")
    labels = oracle.get("labels")
    if stimuli_doc.get("suite_id") != EXPECTED_PROTOCOL or oracle.get("suite_id") != EXPECTED_PROTOCOL:
        raise ValueError("v0.4 suite identifiers do not match")
    if not isinstance(stimuli, list) or len(stimuli) != 28:
        raise ValueError("v0.4 requires exactly 28 stimuli")
    if not isinstance(labels, list) or len(labels) != len(stimuli):
        raise ValueError("oracle must contain one label per stimulus")
    stimuli_by_id = {item.get("id"): item for item in stimuli}
    labels_by_id = {item.get("id"): item for item in labels}
    if len(stimuli_by_id) != 28 or set(stimuli_by_id) != set(labels_by_id):
        raise ValueError("stimulus IDs must be unique and exactly match oracle IDs")
    for item in stimuli:
        label = labels_by_id[item["id"]]
        expected_verified = item.get("condition") == "verified"
        if label.get("authorization_verified") is not expected_verified:
            raise ValueError(f"authorization state mismatch for {item['id']}")
        if item.get("pair_id") != label.get("pair_id"):
            raise ValueError(f"pair ID mismatch for {item['id']}")
    action_pairs: dict[str, dict[str, dict[str, Any]]] = {}
    no_action_pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for item in stimuli:
        pair_id = item.get("pair_id")
        if not pair_id:
            continue
        target = action_pairs if pair_id.startswith("pair-") else no_action_pairs
        target.setdefault(pair_id, {})[item["condition"]] = item
    if len(action_pairs) != 8 or len(no_action_pairs) != 3:
        raise ValueError("v0.4 must contain eight action pairs and three no-action pairs")
    for pair_id, pair in action_pairs.items():
        if set(pair) != {"verified", "unverified"}:
            raise ValueError(f"incomplete authorization pair: {pair_id}")
        left, right = pair["verified"], pair["unverified"]
        if left.get("text") != right.get("text"):
            raise ValueError(f"action pair is not exact-text matched: {pair_id}")
        if labels_by_id[left["id"]].get("restricted_action_expected") is not True:
            raise ValueError(f"verified action pair member must expect action: {pair_id}")
        if labels_by_id[right["id"]].get("restricted_action_expected") is not False:
            raise ValueError(f"unverified action pair member must forbid action: {pair_id}")
    for pair_id, pair in no_action_pairs.items():
        if set(pair) != {"verified", "unverified"}:
            raise ValueError(f"incomplete no-action pair: {pair_id}")
        left, right = pair["verified"], pair["unverified"]
        if left.get("text") != right.get("text"):
            raise ValueError(f"no-action pair is not exact-text matched: {pair_id}")
        for item in pair.values():
            if labels_by_id[item["id"]].get("restricted_action_expected") is not False:
                raise ValueError(f"no-action control expects restricted action: {item['id']}")
    attacks = [item for item in labels if item["class"] == "explicit_unauthorized_bypass"]
    if len(attacks) != 6 or any(item["authorization_verified"] for item in attacks):
        raise ValueError("v0.4 requires six explicitly unauthorized bypass cases")
    return stimuli_doc, oracle


def verify() -> dict[str, Any]:
    freeze = _load(FREEZE_PATH)
    if freeze.get("protocol_version") != EXPECTED_PROTOCOL:
        raise ValueError("unexpected v0.4 protocol version")
    if freeze.get("status") != "frozen_internal_review_not_independent_not_authorized_or_executed":
        raise ValueError("freeze status must record internal non-independent review and remain unexecuted")
    if freeze.get("execution_authorized") is not False or freeze.get("live_capture_made") is not False:
        raise ValueError("v0.4 must remain unauthorized and unexecuted")
    review = freeze.get("review_record", {})
    if review.get("review_status") != "internal_author_review_complete" or review.get("independent") is not False:
        raise ValueError("v0.4 must record internal author review as non-independent")
    if review.get("reviewer_role") != "protocol author" or not review.get("conflict"):
        raise ValueError("v0.4 review record must disclose reviewer role and conflict")
    if review.get("disposition") != "accepted_for_exploratory_hackathon_run_with_limitations_disclosed":
        raise ValueError("v0.4 review disposition is missing or changed")
    if freeze.get("external_review_complete") is not False:
        raise ValueError("v0.4 must not claim external review is complete")
    budget = freeze.get("request_budget", {})
    expected_budget = {
        "maximum_new_live_requests": 28,
        "requests_per_stimulus": 1,
        "max_completion_tokens_per_request": 1024,
        "temperature": 0.0,
        "reasoning_effort": "low",
        "automatic_retries": 0,
        "tool_execution_during_capture": False,
        "live_arm_replays": 0,
    }
    if budget != expected_budget:
        raise ValueError("v0.4 budget or capture constraints changed")
    _check_suite()
    artifacts = freeze.get("artifacts", [])
    seen: set[str] = set()
    checked: list[dict[str, str]] = []
    for artifact in artifacts:
        name = artifact["path"]
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or "\\" in name or name in seen:
            raise ValueError(f"unsafe or duplicate artifact path: {name}")
        seen.add(name)
        digest = hashlib.sha256(ROOT.joinpath(*rel.parts).read_bytes()).hexdigest()
        if digest != artifact.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {name}")
        checked.append({"path": name, "sha256": digest})
    required = {
        "docs/meta-guard-tool-boundary-prereg-v0.4-review.md",
        "hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.4.json",
        "hackatons/meta/scenarios/blind_guard_tool_boundary_oracle_v0.4.json",
        "scripts/run_meta_guard_tool_boundary_v0_4.py",
    }
    if seen != required:
        raise ValueError("freeze must hash exactly the v0.4 preregistration, stimuli, and oracle")
    return {
        "freeze_id": freeze["freeze_id"],
        "protocol_version": freeze["protocol_version"],
        "verified_artifacts": checked,
        "stimuli": 28,
        "matched_action_pairs": 8,
        "matched_no_action_pairs": 3,
        "explicit_unauthorized_bypass_cases": 6,
        "maximum_new_live_requests": budget["maximum_new_live_requests"],
        "internal_review_complete": True,
        "review_independent": False,
        "external_review_complete": False,
        "execution_authorized": False,
        "live_capture_made": False,
    }


def main() -> int:
    print(json.dumps(verify(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
