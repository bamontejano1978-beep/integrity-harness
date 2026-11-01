#!/usr/bin/env python3
"""Verify v0.7 amendment artifacts and bounded-continuation invariants."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.7.json"
EXPECTED_BASELINE_SHA256 = "831055c809fcad90924040d1014330ee8a36b37ce74cfe04ee6d9fc65e585c72"
EXPECTED_ARTIFACTS = {
    "docs/meta-guard-tool-boundary-amendment-v0.7.md",
    "scripts/run_meta_guard_tool_boundary_v0_7.py",
    "scripts/verify_meta_guard_tool_boundary_freeze_v0_7.py",
    "tests/test_meta_guard_tool_boundary_v0_7_continuation.py",
    "hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.6.json",
}


def verify() -> dict[str, Any]:
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    if freeze.get("protocol_version") != "meta-guard-tool-boundary-v0.7":
        raise ValueError("unexpected v0.7 protocol version")
    if freeze.get("status") != "frozen_continuation_live_disabled_not_authorized":
        raise ValueError("v0.7 must remain live-disabled")
    if freeze.get("execution_authorized") is not False or freeze.get("maximum_new_live_requests_authorized") != 0:
        raise ValueError("freeze must authorize zero new requests")
    if freeze.get("cumulative_request_budget") != {
        "previous_requests_already_started": 7,
        "maximum_new_requests": 21,
        "maximum_cumulative_requests": 28,
        "automatic_retries": 0,
    }:
        raise ValueError("cumulative v0.7 request budget changed")
    if freeze.get("source_checkpoint_sha256") != EXPECTED_BASELINE_SHA256:
        raise ValueError("source checkpoint identity changed")
    if freeze.get("failed_request_policy") != {
        "failed_stimulus_id": "pair-008-verified",
        "classification": "attempted_invalid_or_unavailable_response",
        "cause": "unknown_from_safe_checkpoint_diagnostic",
        "retry": False,
        "count_toward_cumulative_budget": True,
    }:
        raise ValueError("failed-request disposition changed")
    if freeze.get("continuation_scope") != "only the 21 never-started stimuli in original v0.6 request order":
        raise ValueError("continuation scope changed")
    if freeze.get("source_checkpoint_mutation") != "prohibited; write continuation to a separate output directory":
        raise ValueError("source checkpoint must remain immutable")
    artifacts = freeze.get("artifacts")
    names: set[str] = set()
    verified: list[dict[str, str]] = []
    for artifact in artifacts or []:
        name = artifact["path"]
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or name in names:
            raise ValueError(f"unsafe or duplicate frozen artifact path: {name}")
        names.add(name)
        actual = hashlib.sha256(ROOT.joinpath(*relative.parts).read_bytes()).hexdigest()
        if actual != artifact.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {name}")
        verified.append({"path": name, "sha256": actual})
    if names != EXPECTED_ARTIFACTS:
        raise ValueError("v0.7 freeze must hash exactly the amendment, runner, verifier, tests and v0.6 freeze")
    return {
        "freeze_id": freeze["freeze_id"], "protocol_version": freeze["protocol_version"],
        "verified_artifacts": verified, "previous_requests": 7, "maximum_new_requests": 21,
        "maximum_cumulative_requests": 28, "automatic_retries": 0,
        "failed_request_retried": False, "source_checkpoint_mutated": False,
        "execution_authorized": False,
    }


def main() -> int:
    try:
        result = verify()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"v0.7 freeze verification failed: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
