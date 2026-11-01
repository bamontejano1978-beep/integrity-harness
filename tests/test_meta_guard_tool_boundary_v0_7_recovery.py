"""Offline tests for safe recovery from a partial v0.7 checkpoint."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts import run_meta_guard_tool_boundary_v0_6 as v06  # noqa: E402
from scripts import run_meta_guard_tool_boundary_v0_7 as v07  # noqa: E402
from scripts import recover_meta_guard_tool_boundary_v0_7 as recovery  # noqa: E402


def _fixtures(tmp_path: Path, monkeypatch):
    frozen_seed_path = recovery.DEFAULT_SEED
    seed_bytes = frozen_seed_path.read_bytes()
    source_seed = tmp_path / "sources" / "continuation_checkpoint.json.partial"
    source_seed.parent.mkdir(parents=True, exist_ok=True)
    source_seed.write_bytes(seed_bytes)
    seed = json.loads(seed_bytes)
    original_path = v07.BASELINE_CHECKPOINT
    original_bytes = original_path.read_bytes()
    monkeypatch.setattr(recovery, "ROOT", tmp_path)
    monkeypatch.setattr(recovery, "DEFAULT_SEED", source_seed)
    monkeypatch.setattr(recovery, "SEED_SHA256", hashlib.sha256(seed_bytes).hexdigest())
    monkeypatch.setattr(recovery, "RECOVERY_FREEZE", tmp_path / "recovery-freeze.json")
    monkeypatch.setattr(recovery, "_verify_recovery_freeze", lambda: None)
    monkeypatch.setattr(recovery, "DEFAULT_OUTPUT", tmp_path / ".tmp" / "recovery")
    monkeypatch.setattr(v07, "ROOT", tmp_path)
    monkeypatch.setattr(v07, "BASELINE_CHECKPOINT", original_path)
    monkeypatch.setattr(v07, "BASELINE_SHA256", hashlib.sha256(original_bytes).hexdigest())
    monkeypatch.setattr(v06, "ROOT", tmp_path)
    monkeypatch.setattr(v06, "_check_output_dir", lambda value: Path(value).resolve())
    return source_seed, seed_bytes, seed, original_path, original_bytes, recovery.DEFAULT_OUTPUT


def test_recovery_carries_forward_partial_valid_response_and_never_retries(tmp_path, monkeypatch) -> None:
    seed_path, seed_bytes, seed, original_path, original_bytes, out = _fixtures(tmp_path, monkeypatch)
    calls: list[str] = []

    class FakeProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            calls.append(messages[0]["content"])
            return "Offline recovery fixture.", []

    result = recovery.run_recovery(
        out, live_authorized=True, confirm_inference_only=True,
        confirm_max_new_requests=20, provider_factory=lambda config: FakeProvider(), seed_path=seed_path,
    )
    checkpoint = json.loads((out / "recovered_capture_checkpoint.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (out / "events.ndjson").read_text(encoding="utf-8").splitlines()]
    request_events = [event for event in events if event["event"] == "request_started"]

    assert len(calls) == 20
    assert result["status"] == "complete_with_one_known_missing_response"
    assert result["cumulative_requests_started"] == 28
    assert result["new_requests_started"] == 20
    assert result["valid_response_count"] == 27
    assert result["missing_response_count"] == 1
    assert [x["stimulus_id"] for x in result["valid_responses"][:6]] == [x["stimulus_id"] for x in seed["prior_valid_completions"]]
    assert result["valid_responses"][6] == seed["continuation_completions"][0]
    assert result["missing_attempts"] == seed["missing_attempts"]
    assert [event["request_index"] for event in request_events] == list(range(9, 29))
    assert all(event["stimulus_id"] != "pair-008-verified" for event in request_events)
    assert all(event["stimulus_id"] != "pair-002-verified" for event in request_events)
    assert len([e for e in events if e["event"] == "completion"]) == 20
    assert events[-1]["event"] == "recovery_complete"
    assert seed_path.read_bytes() == seed_bytes
    assert original_path.read_bytes() == original_bytes
    assert checkpoint["valid_response_count"] == 27


def test_recovery_gates_calls_before_provider_or_destination(tmp_path, monkeypatch) -> None:
    seed, _, _, _, _, out = _fixtures(tmp_path, monkeypatch)
    called = []
    with pytest.raises(PermissionError, match="explicit live authorization"):
        recovery.run_recovery(out, seed_path=seed, provider_factory=lambda config: called.append(1))
    with pytest.raises(PermissionError, match="exactly 20"):
        recovery.run_recovery(
            out, live_authorized=True, confirm_inference_only=True,
            confirm_max_new_requests=21, seed_path=seed,
            provider_factory=lambda config: called.append(1),
        )
    assert called == []
    assert not out.exists()


def test_recovery_seed_hash_mismatch_stops_before_calls(tmp_path, monkeypatch) -> None:
    seed, _, _, _, _, out = _fixtures(tmp_path, monkeypatch)
    seed.write_bytes(seed.read_bytes() + b" ")
    called = []
    with pytest.raises(ValueError, match="seed hash mismatch"):
        recovery.run_recovery(
            out, live_authorized=True, confirm_inference_only=True,
            confirm_max_new_requests=20, seed_path=seed,
            provider_factory=lambda config: called.append(1),
        )
    assert called == []
    assert not out.exists()
