"""Offline regressions for the v0.2 live-run preparation; these tests make no API calls."""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.run_blind_guard_eval_live import (  # noqa: E402
    MAX_COMPLETION_TOKENS,
    _build_config,
    _safe_error_diagnostics,
    run_live,
)


def test_v02_error_diagnostics_keep_http_code_but_drop_body() -> None:
    body = b"provider response echoed a fake-secret-value"
    http_error = urllib.error.HTTPError(
        "https://api.invalid/chat/completions",
        401,
        "unauthorized",
        hdrs=None,
        fp=__import__("io").BytesIO(body),
    )
    wrapped = RuntimeError("provider failed")
    wrapped.__cause__ = http_error

    diagnostic = _safe_error_diagnostics(wrapped)

    assert diagnostic == {"category": "http_error", "http_status": 401}
    assert "fake-secret-value" not in json.dumps(diagnostic)
    assert MAX_COMPLETION_TOKENS == 1024


def test_v02_arm_configs_differ_only_by_generic_exception_guard() -> None:
    off = _build_config(False, "test-model", "https://api.invalid/v1", "MODEL_API_KEY")
    on = _build_config(True, "test-model", "https://api.invalid/v1", "MODEL_API_KEY")

    assert off.provider == on.provider
    assert off.policy == on.policy
    assert off.environment.model_copy(update={"generic_exception_guard": True}) == on.environment
    assert off.environment.generic_exception_guard is False
    assert on.environment.generic_exception_guard is True
    assert off.provider.max_tokens == on.provider.max_tokens == 1024


def test_v02_failed_provider_call_is_counted_and_never_retried(tmp_path: Path) -> None:
    class FailingProvider:
        calls = 0

        def generate_with_tools(self, system_prompt, messages, tools):
            self.calls += 1
            error = urllib.error.HTTPError(
                "https://api.invalid/chat/completions",
                429,
                "rate limited",
                hdrs=None,
                fp=__import__("io").BytesIO(b"private response body"),
            )
            raise RuntimeError("provider wrapper") from error

        def generate(self, system_prompt, messages):
            raise AssertionError("unexpected non-tool provider call")

    provider = FailingProvider()
    run_dir = tmp_path / "v0_2_partial"
    try:
        run_live(
            run_dir,
            provider_factory=lambda _config: provider,
            model="test-model",
            base_url="https://api.invalid/v1",
        )
    except RuntimeError as exc:
        assert "no retry" in str(exc)
    else:
        raise AssertionError("expected the simulated provider error to stop the run")

    checkpoint = json.loads((run_dir / "live_run_checkpoint.json").read_text(encoding="utf-8"))
    assert provider.calls == 1
    assert checkpoint["model_requests_started"] == 1
    assert checkpoint["status"] == "stopped_on_provider_or_harness_error"
    assert checkpoint["safe_error_diagnostics"] == {
        "category": "http_error",
        "http_status": 429,
    }
    checkpoint_text = json.dumps(checkpoint)
    assert "private response body" not in checkpoint_text
    assert "private" not in checkpoint_text
