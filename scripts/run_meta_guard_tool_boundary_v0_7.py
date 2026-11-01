#!/usr/bin/env python3
"""Continue the frozen v0.6 capture without retrying its failed request.

This runner is inference-only: proposed tools are never executed. Its default
output is separate from the immutable v0.6 checkpoint. Live use requires a
separate explicit authorization for exactly 21 new requests.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent.parent
for path in (str(REPO), str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from scripts import run_meta_guard_tool_boundary_v0_6 as v06  # noqa: E402

_verifier_spec = importlib.util.spec_from_file_location(
    "meta_guard_tool_boundary_v0_7_freeze_verifier",
    ROOT / "scripts/verify_meta_guard_tool_boundary_freeze_v0_7.py",
)
if _verifier_spec is None or _verifier_spec.loader is None:
    raise ImportError("cannot load the local v0.7 freeze verifier")
_v07_verifier = importlib.util.module_from_spec(_verifier_spec)
_verifier_spec.loader.exec_module(_v07_verifier)
verify_freeze = _v07_verifier.verify

BASELINE_CHECKPOINT = v06.DEFAULT_OUTPUT / "capture_checkpoint.json"
DEFAULT_OUTPUT = ROOT / ".tmp/meta-guard-tool-boundary-live-v0_7-20261029"
BASELINE_SHA256 = "831055c809fcad90924040d1014330ee8a36b37ce74cfe04ee6d9fc65e585c72"
PRIOR_REQUESTS = 7
NEW_REQUEST_LIMIT = 21
TOTAL_REQUEST_LIMIT = 28


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_baseline(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != BASELINE_SHA256:
        raise ValueError("v0.6 source checkpoint hash mismatch; refusing continuation")
    checkpoint = json.loads(raw)
    if (checkpoint.get("freeze_id") != "meta-guard-tool-boundary-v0.6-neutral-capture-freeze-20261029"
            or checkpoint.get("protocol_version") != "meta-guard-tool-boundary-v0.6"
            or checkpoint.get("requests_started") != PRIOR_REQUESTS
            or checkpoint.get("request_limit") != TOTAL_REQUEST_LIMIT
            or checkpoint.get("status") != "stopped_on_provider_or_harness_error"
            or checkpoint.get("automatic_retries") != 0):
        raise ValueError("v0.6 source checkpoint does not match the frozen partial capture")
    order = checkpoint.get("request_order")
    completions = checkpoint.get("completions")
    if not isinstance(order, list) or len(order) != TOTAL_REQUEST_LIMIT or not isinstance(completions, list):
        raise ValueError("v0.6 checkpoint order or completions are malformed")
    if [row.get("stimulus_id") for row in completions] != order[:6]:
        raise ValueError("v0.6 valid completions do not match their original request positions")
    if order[6] != "pair-008-verified" or len(completions) != 6:
        raise ValueError("v0.6 failed request is not the expected unreplayable seventh request")
    return checkpoint, digest


def capture_continuation(
    output_dir: Path,
    *,
    live_authorized: bool = False,
    confirm_inference_only: bool = False,
    confirm_max_new_requests: int = 0,
    provider_factory: Callable[[Any], Any] | None = None,
    baseline_path: Path = BASELINE_CHECKPOINT,
) -> dict[str, Any]:
    """Capture the 21 never-started stimuli once each; errors stop without retry."""
    if not live_authorized:
        raise PermissionError("continuation requires separate explicit authorization for live inference")
    if not confirm_inference_only or confirm_max_new_requests != NEW_REQUEST_LIMIT:
        raise PermissionError("confirm inference-only capture and exactly 21 new requests")
    verify_freeze()
    baseline, digest = _load_baseline(baseline_path)
    stimuli = json.loads(v06.STIMULI_PATH.read_text(encoding="utf-8"))["stimuli"]
    by_id = {item["id"]: item for item in stimuli}
    order = baseline["request_order"]
    if set(order) != set(by_id) or len(by_id) != TOTAL_REQUEST_LIMIT:
        raise ValueError("frozen v0.4 stimulus IDs do not match the original request order")
    pending = order[PRIOR_REQUESTS:]
    if len(pending) != NEW_REQUEST_LIMIT:
        raise ValueError("continuation must contain exactly 21 never-started stimuli")

    out = v06._check_output_dir(output_dir)
    frozen = v06._load_json(v06.FREEZE_PATH)
    env_name = "MODEL_API_KEY"
    if provider_factory is None:
        secret, env_name = v06._resolve_local_api_key()
        if not secret or not env_name:
            raise ValueError("Meta API key unavailable in submission-local environment files")
        os.environ[env_name] = secret
    provider = provider_factory(v06._provider_config(env_name)) if provider_factory else v06.OpenAICompatibleProvider(v06._provider_config(env_name))
    allowed = {tool["function"]["name"] for tool in frozen["tool_schemas"]}
    missing = [{
        "stimulus_id": order[6], "request_index": 7,
        "classification": "attempted_invalid_or_unavailable_response",
        "diagnostic": baseline.get("safe_error_diagnostics", {"category": "unknown"}),
        "retry_attempted": False,
    }]
    manifest: dict[str, Any] = {
        "schema_version": "0.1", "protocol_version": "meta-guard-tool-boundary-v0.7",
        "freeze_id": "meta-guard-tool-boundary-v0.7-partial-continuation-freeze-20261029",
        "status": "continuation_in_progress", "started_at": datetime.now(UTC).isoformat(),
        "source_checkpoint": str(baseline_path.relative_to(ROOT)).replace("\\", "/"),
        "source_checkpoint_sha256": digest, "source_requests_started": PRIOR_REQUESTS,
        "source_valid_completions": len(baseline["completions"]), "source_request_order": order,
        "request_order": order, "request_limit_total": TOTAL_REQUEST_LIMIT,
        "new_request_limit": NEW_REQUEST_LIMIT, "requests_started": PRIOR_REQUESTS,
        "new_requests_started": 0, "automatic_retries": 0,
        "tool_execution_during_capture": False, "real_world_side_effects": False,
        "prior_valid_completions": baseline["completions"], "continuation_completions": [],
        "missing_attempts": missing, "remaining_unstarted": NEW_REQUEST_LIMIT,
        "system_prompt_template": frozen["system_prompt_template"],
        "tool_schemas": frozen["tool_schemas"],
    }
    checkpoint_path = out / "continuation_checkpoint.json"
    _atomic_json(checkpoint_path, manifest)
    for new_index, stimulus_id in enumerate(pending, start=1):
        manifest["requests_started"] = PRIOR_REQUESTS + new_index
        manifest["new_requests_started"] = new_index
        manifest["remaining_unstarted"] = NEW_REQUEST_LIMIT - new_index
        _atomic_json(checkpoint_path, manifest)
        item = by_id[stimulus_id]
        try:
            content, calls = provider.generate_with_tools(
                v06._system_prompt(frozen["system_prompt_template"], item["condition"]),
                [{"role": "user", "content": item["text"]}], frozen["tool_schemas"],
            )
            v06._validate_completion(content, calls, allowed)
        except Exception as exc:
            manifest["missing_attempts"].append({
                "stimulus_id": stimulus_id, "request_index": PRIOR_REQUESTS + new_index,
                "classification": "attempted_invalid_or_unavailable_response",
                "diagnostic": v06._safe_diagnostic(exc), "retry_attempted": False,
            })
            manifest["status"] = "stopped_on_provider_or_harness_error"
            manifest["stopped_at"] = datetime.now(UTC).isoformat()
            _atomic_json(checkpoint_path, manifest)
            raise RuntimeError(f"continuation stopped at cumulative request {PRIOR_REQUESTS + new_index}/28; no retry") from None
        manifest["continuation_completions"].append({
            "stimulus_id": stimulus_id, "content": content, "tool_calls": calls,
        })
        _atomic_json(checkpoint_path, manifest)
    manifest["status"] = "complete_with_missing_responses"
    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["valid_completion_count"] = len(manifest["prior_valid_completions"]) + len(manifest["continuation_completions"])
    manifest["missing_response_count"] = len(manifest["missing_attempts"])
    _atomic_json(checkpoint_path, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--confirm-inference-only", action="store_true")
    parser.add_argument("--confirm-max-new-requests", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = capture_continuation(
            args.output_dir, live_authorized=args.authorize_live,
            confirm_inference_only=args.confirm_inference_only,
            confirm_max_new_requests=args.confirm_max_new_requests,
        )
    except (OSError, ValueError, RuntimeError, PermissionError, FileExistsError) as exc:
        print(f"v0.7 continuation stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "requests_started": result["requests_started"],
                      "new_requests_started": result["new_requests_started"],
                      "valid_completion_count": result["valid_completion_count"],
                      "missing_response_count": result["missing_response_count"],
                      "run_dir": str(args.output_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
