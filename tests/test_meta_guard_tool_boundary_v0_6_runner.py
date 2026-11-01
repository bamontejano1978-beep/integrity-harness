"""Offline regression tests for the v0.6 capture/replay runner; no provider calls."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts import run_meta_guard_tool_boundary_v0_6 as runner  # noqa: E402
from scripts import verify_meta_guard_tool_boundary_freeze_v0_6 as verifier  # noqa: E402


def _frozen() -> dict:
    return json.loads(runner.FREEZE_PATH.read_text(encoding="utf-8"))


def _capture_setup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "FREEZE_PATH", verifier.FREEZE_PATH)
    monkeypatch.setattr(runner, "verify_freeze", lambda: {
        "live_capture_enabled": False,
        "verified_artifacts": [],
    })
    output = tmp_path / ".tmp" / "run-v0.6"
    return output


def test_freeze_is_valid_discloses_non_independent_review_and_disables_live() -> None:
    result = verifier.verify()
    assert result["stimuli"] == 28
    assert result["matched_action_pairs"] == 8
    assert result["matched_no_action_pairs"] == 3
    assert result["explicit_unauthorized_bypass_cases"] == 6
    assert result["live_capture_enabled"] is False
    assert result["maximum_live_requests_authorized"] == 0
    assert result["review_independent"] is False
    assert result["external_review_required"] is False


def test_capture_requires_authorization_before_creating_output_or_provider(tmp_path, monkeypatch) -> None:
    output = _capture_setup(tmp_path, monkeypatch)
    created: list[bool] = []

    def factory(config):
        created.append(True)
        raise AssertionError("offline guard must not instantiate a provider")

    with pytest.raises(PermissionError, match="explicit authorization"):
        runner.capture_live(output, live_authorized=False, provider_factory=factory)
    with pytest.raises(PermissionError, match="inference-only confirmation"):
        runner.capture_live(
            output, live_authorized=True, provider_factory=factory,
        )
    with pytest.raises(PermissionError, match="inference-only confirmation"):
        runner.capture_live(
            output, live_authorized=True, confirm_inference_only=True,
            confirm_max_requests=27, provider_factory=factory,
        )
    assert not output.exists()
    assert created == []


def test_cli_requires_all_live_confirmations_before_credentials_or_network(capsys) -> None:
    assert runner.main(["capture"]) == 1
    assert "confirm-max-requests 28" in capsys.readouterr().err


def test_capture_fake_provider_is_bounded_and_sends_only_neutral_frozen_inputs(tmp_path, monkeypatch) -> None:
    output = _capture_setup(tmp_path, monkeypatch)
    frozen = _frozen()
    observed: list[dict] = []

    class FakeProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            observed.append({"system": system_prompt, "messages": messages, "tools": tools})
            return "I will assess the request within the supplied context.", []

    def factory(config):
        assert config.model == "muse-spark-1.3"
        assert config.base_url == "https://api.meta.ai/v1"
        assert config.timeout_seconds == 45
        assert config.max_tokens == 1024
        assert config.temperature == 0.0
        assert config.reasoning_effort == "low"
        return FakeProvider()

    result = runner.capture_live(
        output,
        live_authorized=True,
        confirm_inference_only=True,
        confirm_max_requests=28,
        provider_factory=factory,
    )
    checkpoint = json.loads((output / "capture_checkpoint.json").read_text(encoding="utf-8"))
    suite = json.loads(runner.STIMULI_PATH.read_text(encoding="utf-8"))
    stimuli_by_id = {item["id"]: item for item in suite["stimuli"]}

    assert result["status"] == "complete"
    assert result["requests_started"] == result["request_limit"] == 28
    assert result["automatic_retries"] == 0
    assert result["tool_execution_during_capture"] is False
    assert result["real_world_side_effects"] is False
    assert len(observed) == len(result["completions"]) == 28
    assert checkpoint["capture_authorized_for_this_invocation"] is True
    assert checkpoint["completions_sha256"] == result["completions_sha256"]
    assert [call["tools"] for call in observed] == [frozen["tool_schemas"]] * 28

    for request, stimulus_id in zip(observed, result["request_order"], strict=True):
        stimulus = stimuli_by_id[stimulus_id]
        assert request["system"] == runner._system_prompt(
            frozen["system_prompt_template"], stimulus["condition"]
        )
        assert request["messages"] == [{"role": "user", "content": stimulus["text"]}]
        encoded = json.dumps(request, ensure_ascii=False)
        assert stimulus_id not in encoded
        pair_id = stimulus.get("pair_id") or ""
        assert not pair_id or pair_id not in encoded
        assert "oracle" not in encoded.lower()
        assert "restricted_action_expected" not in encoded
        assert "pair_id" not in encoded
        assert "stimulus_id" not in encoded


def test_capture_stops_without_retry_and_persists_only_safe_diagnostics(tmp_path, monkeypatch) -> None:
    output = _capture_setup(tmp_path, monkeypatch)
    requests: list[int] = []

    class FailingProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            requests.append(1)
            raise RuntimeError("sensitive provider body must not be persisted")

    with pytest.raises(RuntimeError, match="no retry was attempted"):
        runner.capture_live(
            output,
            live_authorized=True,
            confirm_inference_only=True,
            confirm_max_requests=28,
            provider_factory=lambda config: FailingProvider(),
        )
    checkpoint = json.loads((output / "capture_checkpoint.json").read_text(encoding="utf-8"))
    assert len(requests) == 1
    assert checkpoint["requests_started"] == 1
    assert checkpoint["status"] == "stopped_on_provider_or_harness_error"
    assert checkpoint["safe_error_diagnostics"] == {
        "category": "provider_or_harness_error",
        "exception_type": "RuntimeError",
    }
    assert "sensitive provider body" not in json.dumps(checkpoint)


def test_capture_rejects_tool_names_outside_the_frozen_mock_allowlist(tmp_path, monkeypatch) -> None:
    output = _capture_setup(tmp_path, monkeypatch)

    class BadProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            return "Proposal", [{
                "id": "bad-call",
                "type": "function",
                "function": {"name": "real_bank_transfer", "arguments": "{}"},
            }]

    with pytest.raises(RuntimeError, match="no retry was attempted"):
        runner.capture_live(
            output,
            live_authorized=True,
            confirm_inference_only=True,
            confirm_max_requests=28,
            provider_factory=lambda config: BadProvider(),
        )
    checkpoint = json.loads((output / "capture_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["requests_started"] == 1
    assert checkpoint["completions"] == []
    assert checkpoint["tool_execution_during_capture"] is False


def test_replay_requires_complete_capture_before_running_local_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "verify_freeze", lambda: {"verified_artifacts": []})
    run_dir = tmp_path / ".tmp" / "incomplete"
    run_dir.mkdir(parents=True)
    (run_dir / "capture_checkpoint.json").write_text(
        json.dumps({"protocol_version": "meta-guard-tool-boundary-v0.6", "status": "running"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="complete v0.6 capture"):
        runner.replay_offline(run_dir)


def test_replay_runs_only_offline_mocks_for_a_complete_fake_capture(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "verify_freeze", lambda: {"verified_artifacts": []})
    suite_bytes = runner.STIMULI_PATH.read_bytes()
    suite = json.loads(suite_bytes)
    frozen = _frozen()
    run_dir = tmp_path / ".tmp" / "complete"
    run_dir.mkdir(parents=True)
    completions = [
        {"stimulus_id": item["id"], "content": "No external action taken.", "tool_calls": []}
        for item in suite["stimuli"]
    ]
    capture = {
        "protocol_version": "meta-guard-tool-boundary-v0.6",
        "freeze_id": frozen["freeze_id"],
        "status": "complete",
        "requests_started": 28,
        "model": "offline-test-fixture",
        "stimulus_sha256": hashlib.sha256(suite_bytes).hexdigest(),
        "request_order": [item["id"] for item in suite["stimuli"]],
        "completions": completions,
        "completions_sha256": hashlib.sha256(
            json.dumps(completions, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }
    (run_dir / "capture_checkpoint.json").write_text(json.dumps(capture), encoding="utf-8")
    report = runner.replay_offline(run_dir)
    assert report["replay_rows"] == 56
    assert report["capture_requests_started"] == 28
    assert report["real_world_side_effects"] is False
    assert report["evaluation_kind"] == "live_model_proposals_replayed_offline_in_local_mock_sandbox"
    assert (run_dir / "replay_report.json").is_file()
