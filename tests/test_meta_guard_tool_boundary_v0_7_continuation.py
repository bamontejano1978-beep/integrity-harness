"""Offline tests for the v0.7 continuation; never contact the provider."""
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
from scripts import run_meta_guard_tool_boundary_v0_7 as runner  # noqa: E402
from scripts import verify_meta_guard_tool_boundary_freeze_v0_7 as verifier  # noqa: E402


def _baseline_fixture(tmp_path: Path) -> tuple[Path, bytes, dict]:
    suite = json.loads(v06.STIMULI_PATH.read_text(encoding="utf-8"))
    stimuli = suite["stimuli"]
    ordered = v06._ordered_stimuli(stimuli, v06.RANDOM_SEED)
    order = [item["id"] for item in ordered]
    assert order[6] == "pair-008-verified"
    prior_completions = [
        {"stimulus_id": item["id"], "content": "Earlier valid offline fixture response.", "tool_calls": []}
        for item in ordered[:6]
    ]
    payload = {
        "protocol_version": "meta-guard-tool-boundary-v0.6",
        "freeze_id": "meta-guard-tool-boundary-v0.6-neutral-capture-freeze-20261029",
        "request_limit": 28,
        "requests_started": 7,
        "automatic_retries": 0,
        "status": "stopped_on_provider_or_harness_error",
        "request_order": order,
        "completions": prior_completions,
        "safe_error_diagnostics": {"category": "provider_or_harness_error", "exception_type": "ValueError"},
    }
    raw = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path = tmp_path / "source" / "capture_checkpoint.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return path, raw, payload


def _configure(tmp_path: Path, monkeypatch) -> tuple[Path, Path, bytes, dict]:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "BASELINE_CHECKPOINT", tmp_path / "source" / "capture_checkpoint.json")
    path, raw, payload = _baseline_fixture(tmp_path)
    monkeypatch.setattr(runner, "BASELINE_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(runner, "verify_freeze", lambda: {"execution_authorized": False})
    monkeypatch.setattr(v06, "ROOT", tmp_path)
    monkeypatch.setattr(v06, "_check_output_dir", lambda value: Path(value).resolve())
    return path, tmp_path / ".tmp" / "v0_7", raw, payload


def test_v07_freeze_disables_live_calls_and_pins_immutable_source() -> None:
    result = verifier.verify()
    assert result["previous_requests"] == 7
    assert result["maximum_new_requests"] == 21
    assert result["maximum_cumulative_requests"] == 28
    assert result["automatic_retries"] == 0
    assert result["failed_request_retried"] is False
    assert result["source_checkpoint_mutated"] is False
    assert result["execution_authorized"] is False


def test_live_authorization_gates_run_before_creating_output_or_provider(tmp_path, monkeypatch) -> None:
    _, output, _, _ = _configure(tmp_path, monkeypatch)
    provider_created = []

    def factory(config):
        provider_created.append(True)
        raise AssertionError("provider must not be constructed without authorization")

    with pytest.raises(PermissionError, match="separate explicit authorization"):
        runner.capture_continuation(output, provider_factory=factory)
    with pytest.raises(PermissionError, match="exactly 21"):
        runner.capture_continuation(
            output, live_authorized=True, confirm_inference_only=True,
            confirm_max_new_requests=20, provider_factory=factory,
        )
    assert not output.exists()
    assert provider_created == []


def test_continuation_skips_failed_stimulus_preserves_source_and_stops_at_28(tmp_path, monkeypatch) -> None:
    source, output, source_bytes, baseline = _configure(tmp_path, monkeypatch)
    calls: list[dict] = []
    frozen = v06._load_json(v06.FREEZE_PATH)

    class FakeProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            calls.append({"system": system_prompt, "messages": messages, "tools": tools})
            return "Offline test fixture response.", []

    result = runner.capture_continuation(
        output, live_authorized=True, confirm_inference_only=True,
        confirm_max_new_requests=21, provider_factory=lambda config: FakeProvider(),
        baseline_path=source,
    )
    checkpoint = json.loads((output / "continuation_checkpoint.json").read_text(encoding="utf-8"))
    pending = baseline["request_order"][7:]
    suite = json.loads(v06.STIMULI_PATH.read_text(encoding="utf-8"))
    stimuli = {item["id"]: item for item in suite["stimuli"]}

    assert len(calls) == 21
    assert result["status"] == "complete_with_missing_responses"
    assert result["requests_started"] == 28
    assert result["new_requests_started"] == 21
    assert result["valid_completion_count"] == 27
    assert result["missing_response_count"] == 1
    assert result["remaining_unstarted"] == 0
    assert result["automatic_retries"] == 0
    assert result["tool_execution_during_capture"] is False
    assert result["real_world_side_effects"] is False
    assert [item["stimulus_id"] for item in result["continuation_completions"]] == pending
    assert [item["stimulus_id"] for item in result["prior_valid_completions"]] == baseline["request_order"][:6]
    assert result["missing_attempts"] == [{
        "stimulus_id": "pair-008-verified", "request_index": 7,
        "classification": "attempted_invalid_or_unavailable_response",
        "diagnostic": baseline["safe_error_diagnostics"], "retry_attempted": False,
    }]
    assert checkpoint["source_checkpoint_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert source.read_bytes() == source_bytes
    assert [request["tools"] for request in calls] == [frozen["tool_schemas"]] * 21
    for request, stimulus_id in zip(calls, pending, strict=True):
        stimulus = stimuli[stimulus_id]
        assert request["system"] == v06._system_prompt(frozen["system_prompt_template"], stimulus["condition"])
        assert request["messages"] == [{"role": "user", "content": stimulus["text"]}]
        encoded = json.dumps(request, ensure_ascii=False)
        assert stimulus_id not in encoded
        assert "pair_id" not in encoded and "oracle" not in encoded.lower()


def test_invalid_continuation_response_becomes_missing_without_retry(tmp_path, monkeypatch) -> None:
    source, output, source_bytes, _ = _configure(tmp_path, monkeypatch)
    attempts: list[int] = []

    class InvalidProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            attempts.append(1)
            return "", []

    with pytest.raises(RuntimeError, match="cumulative request 8/28; no retry"):
        runner.capture_continuation(
            output, live_authorized=True, confirm_inference_only=True,
            confirm_max_new_requests=21, provider_factory=lambda config: InvalidProvider(),
            baseline_path=source,
        )
    checkpoint = json.loads((output / "continuation_checkpoint.json").read_text(encoding="utf-8"))
    assert len(attempts) == 1
    assert checkpoint["requests_started"] == 8
    assert checkpoint["new_requests_started"] == 1
    assert checkpoint["status"] == "stopped_on_provider_or_harness_error"
    assert checkpoint["automatic_retries"] == 0
    assert len(checkpoint["missing_attempts"]) == 2
    assert checkpoint["missing_attempts"][-1]["stimulus_id"] == checkpoint["request_order"][7]
    assert checkpoint["missing_attempts"][-1]["retry_attempted"] is False
    assert source.read_bytes() == source_bytes


def test_checkpoint_identity_mismatch_refuses_before_provider_or_output(tmp_path, monkeypatch) -> None:
    source, output, _, _ = _configure(tmp_path, monkeypatch)
    source.write_bytes(source.read_bytes() + b" ")
    provider_created = []
    with pytest.raises(ValueError, match="hash mismatch"):
        runner.capture_continuation(
            output, live_authorized=True, confirm_inference_only=True,
            confirm_max_new_requests=21,
            provider_factory=lambda config: provider_created.append(True), baseline_path=source,
        )
    assert provider_created == []
    assert not output.exists()


def test_cli_never_runs_without_explicit_continuation_authorization(capsys) -> None:
    assert runner.main([]) == 1
    assert "separate explicit authorization" in capsys.readouterr().err
