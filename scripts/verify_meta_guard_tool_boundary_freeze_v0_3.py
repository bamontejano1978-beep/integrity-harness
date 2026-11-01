#!/usr/bin/env python3
"""Offline integrity verifier for the proposed Meta tool-boundary v0.3 freeze."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.3.json"
EXPECTED_PROTOCOL = "meta-guard-tool-boundary-live-v0.3"


def verify() -> dict[str, Any]:
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    if freeze.get("protocol_version") != EXPECTED_PROTOCOL:
        raise ValueError("unexpected v0.3 protocol version")
    if freeze.get("status") != "frozen_not_authorized_or_executed":
        raise ValueError("v0.3 freeze must remain unauthorized and unexecuted")
    if freeze.get("execution_authorized") is not False:
        raise ValueError("freeze must not authorize paid live execution")
    if freeze.get("live_capture_made") is not False:
        raise ValueError("freeze must record no live capture at freeze time")
    budget = freeze.get("request_budget", {})
    expected_budget = {
        "maximum_new_live_requests": 14,
        "requests_per_stimulus": 1,
        "max_completion_tokens_per_request": 1024,
        "temperature": 0.0,
        "reasoning_effort": "low",
        "automatic_retries": 0,
        "tool_execution_during_capture": False,
        "live_arm_replays": 0,
    }
    if budget != expected_budget:
        raise ValueError("v0.3 request budget or capture constraints changed")
    artifacts = freeze.get("artifacts", [])
    if not artifacts:
        raise ValueError("freeze has no hashed artifacts")
    checked: list[dict[str, str]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        rel = PurePosixPath(artifact["path"])
        if rel.is_absolute() or ".." in rel.parts or "\\" in artifact["path"]:
            raise ValueError(f"unsafe artifact path: {artifact['path']}")
        if artifact["path"] in seen:
            raise ValueError(f"duplicate artifact path: {artifact['path']}")
        seen.add(artifact["path"])
        path = ROOT.joinpath(*rel.parts)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != artifact.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {artifact['path']}")
        checked.append({"path": artifact["path"], "sha256": actual})
    required = {
        "docs/meta-guard-tool-boundary-prereg-v0.3-live.md",
        "hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.3.json",
        "hackatons/meta/scenarios/blind_guard_tool_boundary_oracle_v0.3.json",
        "scripts/run_meta_guard_tool_boundary_v0_3.py",
        "tests/test_meta_guard_tool_boundary_live_v0_3.py",
    }
    if not required.issubset(seen):
        raise ValueError("freeze omits one or more required v0.3 live protocol artifacts")
    return {
        "freeze_id": freeze["freeze_id"],
        "protocol_version": freeze["protocol_version"],
        "verified_artifacts": checked,
        "maximum_new_live_requests": budget["maximum_new_live_requests"],
        "execution_authorized": False,
        "live_capture_made": False,
    }


def main() -> int:
    print(json.dumps(verify(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
