#!/usr/bin/env python3
"""Offline verifier for the v0.2 live-evaluation amendment freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_eval_freeze_v0.2.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify() -> dict[str, Any]:
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    if freeze.get("protocol_version") != "meta-guard-balanced-screen-v0.2":
        raise ValueError("unexpected protocol version")
    if freeze.get("status") != "frozen_not_executed":
        raise ValueError("v0.2 must remain frozen and not executed")
    if freeze.get("execution_authorized") is not False:
        raise ValueError("v0.2 does not authorize a live execution")
    if freeze.get("live_calls_made_under_v0_2") is not False:
        raise ValueError("freeze must declare that no v0.2 calls have been made")
    budget = freeze.get("request_budget", {})
    if budget != {
        "v0_1_requests_already_started": 1,
        "v0_2_maximum_new_requests": 24,
        "combined_maximum_if_v0_2_is_authorized": 25,
        "max_completion_tokens_per_request": 1024,
        "temperature": 0.0,
        "reasoning_effort": "low",
        "automatic_retries": 0,
    }:
        raise ValueError("unexpected v0.2 run budget/parameters")

    checked: list[dict[str, str]] = []
    for artifact in freeze.get("artifacts", []):
        relative = PurePosixPath(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in artifact["path"]:
            raise ValueError(f"unsafe artifact path: {artifact['path']}")
        path = ROOT.joinpath(*relative.parts)
        actual = _sha256(path)
        if actual != artifact.get("sha256"):
            raise ValueError(f"SHA-256 mismatch: {artifact['path']}")
        checked.append({"path": artifact["path"], "sha256": actual})

    prior = json.loads(
        (ROOT / "hackatons/meta/scenarios/blind_guard_eval_freeze_v0.1.json").read_text(
            encoding="utf-8"
        )
    )
    if prior.get("execution_authorized") is not False:
        raise ValueError("v0.1 freeze must remain unchanged and unauthorized")
    return {
        "freeze_id": freeze["freeze_id"],
        "protocol_version": freeze["protocol_version"],
        "verified_artifacts": checked,
        "v0_1_requests_already_started": budget["v0_1_requests_already_started"],
        "v0_2_maximum_new_requests": budget["v0_2_maximum_new_requests"],
        "execution_authorized": False,
        "live_calls_made_under_v0_2": False,
    }


def main() -> int:
    print(json.dumps(verify(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
