#!/usr/bin/env python3
"""Recover a v0.7 continuation after a local checkpoint-write interruption.

The source partial checkpoint is immutable. This runner starts only stimuli
whose requests were never started, writes append-only attempt/completion events,
and cannot exceed the original cumulative budget of 28 requests.
"""
from __future__ import annotations

import argparse
import hashlib
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
from scripts import run_meta_guard_tool_boundary_v0_7 as v07  # noqa: E402

DEFAULT_SEED = v07.DEFAULT_OUTPUT / "continuation_checkpoint.json.partial"
DEFAULT_OUTPUT = ROOT / ".tmp/meta-guard-tool-boundary-live-v0_7-recovery-20261029"
SEED_SHA256 = "6067fee985469c844c3498bed347aa8e2e78e1e899dd83b9117b92ce03f41211"
RECOVERY_FREEZE = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_recovery_freeze_v0.7.json"
RECOVERY_FREEZE_ID = "meta-guard-tool-boundary-v0.7-checkpoint-recovery-freeze-20261029"
PRIOR_STARTED = 8
NEW_REQUEST_LIMIT = 20
TOTAL_REQUEST_LIMIT = 28


def _append_event(path: Path, event: dict[str, Any]) -> None:
    encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("recovery seed must be a JSON object")
    return result


def _load_seed(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SEED_SHA256:
        raise ValueError("partial continuation seed hash mismatch; refusing recovery")
    seed = json.loads(raw)
    if (seed.get("protocol_version") != "meta-guard-tool-boundary-v0.7"
            or seed.get("requests_started") != PRIOR_STARTED
            or seed.get("new_requests_started") != 1
            or seed.get("remaining_unstarted") != NEW_REQUEST_LIMIT
            or seed.get("status") != "continuation_in_progress"):
        raise ValueError("partial continuation seed counters/status are unexpected")
    if seed.get("request_limit_total") != TOTAL_REQUEST_LIMIT or seed.get("automatic_retries") != 0:
        raise ValueError("partial continuation seed exceeds or changes the frozen budget")
    order = seed.get("request_order")
    prior = seed.get("prior_valid_completions")
    completed = seed.get("continuation_completions")
    missing = seed.get("missing_attempts")
    if not isinstance(order, list) or len(order) != TOTAL_REQUEST_LIMIT:
        raise ValueError("partial continuation seed has invalid frozen order")
    if not isinstance(prior, list) or [x.get("stimulus_id") for x in prior] != order[:6]:
        raise ValueError("prior v0.6 completions do not match the six valid source rows")
    if not isinstance(completed, list) or [x.get("stimulus_id") for x in completed] != [order[7]]:
        raise ValueError("partial continuation must preserve exactly the eighth request response")
    if not isinstance(missing, list) or len(missing) != 1 or missing[0].get("stimulus_id") != order[6]:
        raise ValueError("original seventh request must remain the single known missing attempt")
    if order[7] != "pair-002-verified" or missing[0].get("retry_attempted") is not False:
        raise ValueError("partial response identity or no-retry disposition changed")
    return seed, digest


def _verify_recovery_freeze() -> None:
    freeze = _read_json(RECOVERY_FREEZE)
    if (freeze.get("freeze_id") != RECOVERY_FREEZE_ID
            or freeze.get("execution_authorized") is not False
            or freeze.get("maximum_new_live_requests_authorized") != 0):
        raise ValueError("recovery freeze is invalid or unexpectedly authorizes requests")
    for artifact in freeze.get("artifacts", []):
        relative = Path(*artifact["path"].split("/"))
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual != artifact.get("sha256"):
            raise ValueError(f"recovery freeze hash mismatch: {artifact['path']}")


def run_recovery(
    output_dir: Path,
    *,
    live_authorized: bool = False,
    confirm_inference_only: bool = False,
    confirm_max_new_requests: int = 0,
    provider_factory: Callable[[Any], Any] | None = None,
    seed_path: Path = DEFAULT_SEED,
) -> dict[str, Any]:
    """Run only never-started calls; a started call is never automatically repeated."""
    if not live_authorized:
        raise PermissionError("recovery requires explicit live authorization")
    if not confirm_inference_only or confirm_max_new_requests != NEW_REQUEST_LIMIT:
        raise PermissionError("confirm inference-only recovery and exactly 20 new requests")
    _verify_recovery_freeze()
    original_bytes = seed_path.read_bytes()
    seed, seed_digest = _load_seed(seed_path)
    base_bytes = v07.BASELINE_CHECKPOINT.read_bytes()
    if hashlib.sha256(base_bytes).hexdigest() != v07.BASELINE_SHA256:
        raise ValueError("original v0.6 checkpoint hash mismatch; refusing recovery")
    stimuli = json.loads(v06.STIMULI_PATH.read_text(encoding="utf-8"))["stimuli"]
    by_id = {item["id"]: item for item in stimuli}
    order = seed["request_order"]
    if set(by_id) != set(order) or len(by_id) != TOTAL_REQUEST_LIMIT:
        raise ValueError("frozen v0.4 corpus does not match the original request order")
    pending = order[PRIOR_STARTED:]
    if len(pending) != NEW_REQUEST_LIMIT:
        raise ValueError("recovery must contain exactly 20 never-started requests")
    out = Path(output_dir).resolve()
    tmp_root = (ROOT / ".tmp").resolve()
    if not out.is_relative_to(tmp_root):
        raise ValueError("recovery output must be inside the submission .tmp directory")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("refusing to overwrite an existing recovery run")
    out.mkdir(parents=True, exist_ok=True)
    if seed_path.read_bytes() != original_bytes:
        raise ValueError("partial continuation seed changed during validation")
    if hashlib.sha256(v07.BASELINE_CHECKPOINT.read_bytes()).hexdigest() != v07.BASELINE_SHA256:
        raise ValueError("original v0.6 checkpoint changed during validation")

    frozen = v06._load_json(v06.FREEZE_PATH)
    env_name = "MODEL_API_KEY"
    if provider_factory is None:
        secret, env_name = v06._resolve_local_api_key()
        if not secret or not env_name:
            raise ValueError("Meta API key unavailable in submission-local environment files")
        os.environ[env_name] = secret
    config = v06._provider_config(env_name)
    provider = provider_factory(config) if provider_factory else v06.OpenAICompatibleProvider(config)
    allowed = {tool["function"]["name"] for tool in frozen["tool_schemas"]}
    journal = out / "events.ndjson"
    manifest = {
        "schema_version": "0.1", "protocol_version": "meta-guard-tool-boundary-v0.7",
        "recovery_freeze_id": RECOVERY_FREEZE_ID, "status": "recovery_in_progress",
        "source_partial_checkpoint_sha256": seed_digest,
        "original_v0_6_checkpoint_sha256": v07.BASELINE_SHA256,
        "cumulative_requests_already_started": PRIOR_STARTED,
        "maximum_new_requests": NEW_REQUEST_LIMIT,
        "maximum_cumulative_requests": TOTAL_REQUEST_LIMIT,
        "initial_valid_responses": len(seed["prior_valid_completions"]) + len(seed["continuation_completions"]),
        "initial_missing_attempts": seed["missing_attempts"], "request_order": order,
        "new_request_order": pending, "automatic_retries": 0,
        "tool_execution_during_capture": False, "real_world_side_effects": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = out / "recovery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total_started = PRIOR_STARTED
    new_started = 0
    completions: list[dict[str, Any]] = []
    missing = list(seed["missing_attempts"])
    _append_event(journal, {"event": "recovery_started", "at": datetime.now(UTC).isoformat(),
                            "seed_sha256": seed_digest, "cumulative_started": PRIOR_STARTED,
                            "new_request_limit": NEW_REQUEST_LIMIT})
    for offset, stimulus_id in enumerate(pending, start=1):
        item = by_id[stimulus_id]
        request_index = PRIOR_STARTED + offset
        if request_index > TOTAL_REQUEST_LIMIT:
            raise RuntimeError("cumulative request ceiling exceeded; this should be unreachable")
        total_started = request_index
        new_started = offset
        _append_event(journal, {"event": "request_started", "at": datetime.now(UTC).isoformat(),
                                "request_index": request_index, "stimulus_id": stimulus_id})
        try:
            content, calls = provider.generate_with_tools(
                v06._system_prompt(frozen["system_prompt_template"], item["condition"]),
                [{"role": "user", "content": item["text"]}], frozen["tool_schemas"],
            )
            v06._validate_completion(content, calls, allowed)
        except Exception as exc:
            diag = v06._safe_diagnostic(exc)
            missing_row = {"stimulus_id": stimulus_id, "request_index": request_index,
                           "classification": "attempted_invalid_or_unavailable_response",
                           "diagnostic": diag, "retry_attempted": False}
            missing.append(missing_row)
            _append_event(journal, {"event": "request_missing", "at": datetime.now(UTC).isoformat(),
                                    **missing_row})
            manifest.update({"status": "stopped_on_provider_or_harness_error", "stopped_at": datetime.now(UTC).isoformat(),
                             "cumulative_requests_started": total_started, "new_requests_started": new_started,
                             "valid_responses": 7 + len(completions), "missing_attempts": missing})
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            raise RuntimeError(f"recovery stopped at cumulative request {request_index}/28; no retry") from None
        completion = {"stimulus_id": stimulus_id, "content": content, "tool_calls": calls}
        completions.append(completion)
        _append_event(journal, {"event": "completion", "at": datetime.now(UTC).isoformat(),
                                "request_index": request_index, "completion": completion})
    if seed_path.read_bytes() != original_bytes:
        raise ValueError("partial continuation seed changed during recovery")
    if hashlib.sha256(v07.BASELINE_CHECKPOINT.read_bytes()).hexdigest() != v07.BASELINE_SHA256:
        raise ValueError("original v0.6 checkpoint changed during recovery")
    combined = seed["prior_valid_completions"] + seed["continuation_completions"] + completions
    result = {
        **manifest, "status": "complete_with_one_known_missing_response",
        "completed_at": datetime.now(UTC).isoformat(),
        "cumulative_requests_started": total_started, "new_requests_started": new_started,
        "valid_response_count": len(combined), "missing_response_count": len(missing),
        "valid_responses": combined, "missing_attempts": missing,
        "journal": journal.name,
    }
    checkpoint = out / "recovered_capture_checkpoint.json"
    checkpoint.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _append_event(journal, {"event": "recovery_complete", "at": datetime.now(UTC).isoformat(),
                            "cumulative_requests_started": total_started,
                            "valid_response_count": len(combined), "missing_response_count": len(missing)})
    manifest_path.write_text(json.dumps({k: v for k, v in result.items() if k != "valid_responses"},
                                        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--confirm-inference-only", action="store_true")
    parser.add_argument("--confirm-max-new-requests", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = run_recovery(args.output_dir, live_authorized=args.authorize_live,
                              confirm_inference_only=args.confirm_inference_only,
                              confirm_max_new_requests=args.confirm_max_new_requests)
    except (OSError, ValueError, RuntimeError, PermissionError, FileExistsError) as exc:
        print(f"v0.7 recovery stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"],
                      "cumulative_requests_started": result["cumulative_requests_started"],
                      "new_requests_started": result["new_requests_started"],
                      "valid_response_count": result["valid_response_count"],
                      "missing_response_count": result["missing_response_count"],
                      "run_dir": str(args.output_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
