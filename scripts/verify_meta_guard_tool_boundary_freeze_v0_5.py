#!/usr/bin/env python3
"""Verify v0.5 protocol artifacts and live-disabled freeze invariants."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.5.json"
STIMULI_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.4.json"
ORACLE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_oracle_v0.4.json"
EXPECTED_PROTOCOL = "meta-guard-tool-boundary-v0.5"


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected an object in {path}")
    return payload


def verify() -> dict[str, Any]:
    freeze = _json(FREEZE_PATH)
    if freeze.get("protocol_version") != EXPECTED_PROTOCOL:
        raise ValueError("unexpected v0.5 protocol")
    if freeze.get("status") != "frozen_offline_only_live_disabled":
        raise ValueError("v0.5 must remain frozen offline-only")
    if freeze.get("live_capture_enabled") is not False or freeze.get("live_capture_made") is not False:
        raise ValueError("live capture must remain disabled and unmade")
    if freeze.get("execution_authorized") is not False or freeze.get("maximum_live_requests_authorized") != 0:
        raise ValueError("this freeze authorizes zero live requests")
    expected_offline = {
        "provider_calls": 0,
        "external_network_calls": 0,
        "real_world_tool_execution": 0,
        "expected_unauthorized_mock_blocks_with_boundary": 8,
        "expected_authorized_mock_successes_with_boundary": 8,
        "expected_no_boundary_mock_successes": 16,
    }
    if freeze.get("offline_fault_injection") != expected_offline:
        raise ValueError("offline fault injection constraints changed")
    review = freeze.get("review_record", {})
    if review.get("review_status") != "internal_author_review_complete" or review.get("independent") is not False:
        raise ValueError("review must be accurately recorded as internal and non-independent")
    stimuli = _json(STIMULI_PATH)["stimuli"]
    labels_doc = _json(ORACLE_PATH)
    labels = {item["id"]: item for item in labels_doc["labels"]}
    if len(stimuli) != 28 or len(labels) != 28 or set(x["id"] for x in stimuli) != set(labels):
        raise ValueError("v0.5 reused corpus or scorer labels changed unexpectedly")
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for item in stimuli:
        pair_id = item.get("pair_id")
        if pair_id and pair_id.startswith("pair-"):
            pairs.setdefault(pair_id, {})[item["condition"]] = item
    if len(pairs) != 8:
        raise ValueError("v0.5 requires the eight frozen action pairs")
    for pair_id, pair in pairs.items():
        if set(pair) != {"verified", "unverified"} or pair["verified"]["text"] != pair["unverified"]["text"]:
            raise ValueError(f"exact-text pair mismatch: {pair_id}")
    seen: set[str] = set()
    verified: list[dict[str, str]] = []
    for item in freeze.get("artifacts", []):
        name = item["path"]
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or "\\" in name or name in seen:
            raise ValueError(f"unsafe or duplicate path: {name}")
        seen.add(name)
        digest = hashlib.sha256(ROOT.joinpath(*rel.parts).read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {name}")
        verified.append({"path": name, "sha256": digest})
    required = {
        "docs/meta-guard-tool-boundary-prereg-v0.5.md",
        "scripts/verify_meta_guard_tool_boundary_freeze_v0_5.py",
        "tests/test_meta_guard_tool_boundary_v0_5_offline.py",
        "hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.4.json",
        "hackatons/meta/scenarios/blind_guard_tool_boundary_oracle_v0.4.json",
    }
    if seen != required:
        raise ValueError("freeze artifact list differs from required v0.5 set")
    return {
        "freeze_id": freeze["freeze_id"],
        "protocol_version": freeze["protocol_version"],
        "verified_artifacts": verified,
        "stimuli": len(stimuli),
        "matched_action_pairs": len(pairs),
        "live_capture_enabled": False,
        "maximum_live_requests_authorized": 0,
        "provider_calls_for_offline_test": 0,
        "external_network_calls_for_offline_test": 0,
        "review_independent": False,
    }


def main() -> int:
    print(json.dumps(verify(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
