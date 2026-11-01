#!/usr/bin/env python3
"""One-shot live runner for the preregistered balanced guard comparison.

This script never loads the scoring oracle and never retries a model request.
It checkpoints after each completed model request and refuses to overwrite an
existing run directory, preventing accidental repeat spend after interruption.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent.parent
SRC = ROOT / "src"
for import_path in (str(REPO), str(ROOT), str(SRC)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from autopilot.adapter import load_adapter_config  # noqa: E402
from autopilot.agent_sandbox import (  # noqa: E402
    SandboxEvaluationBundle,
    SandboxProviderConfig,
    load_declarative_sandbox_config,
    run_declarative_sandbox_evaluation,
)
from autopilot.contracts import load_contract  # noqa: E402
from autopilot.insurance_agent import (  # noqa: E402
    AgentScenario,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderType,
)
from scripts.verify_blind_guard_eval_freeze import verify as verify_freeze  # noqa: E402

CONTRACT_PATH = ROOT / "hackatons/meta/contracts/meta_safety_scan_contract_v0.1.yaml"
GUARDED_SANDBOX_PATH = ROOT / "hackatons/meta/contracts/meta_sandbox_guarded_v0.1.yaml"
STIMULI_PATH = ROOT / "hackatons/meta/scenarios/blind_guard_eval_stimuli_v0.1.json"
API_KEY_NAMES = ("META_MODEL_API_KEY", "MODEL_API_KEY", "LLAMA_API_KEY")


def _resolve_local_api_key() -> tuple[str | None, str | None]:
    """Read the submission-local secret file without printing or exporting its value."""
    for name in API_KEY_NAMES:
        value = os.environ.get(name)
        if value:
            return value, name
    for candidate in (ROOT / ".env", ROOT / ".env.txt", ROOT / "apimeta.env"):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            if key.strip() not in API_KEY_NAMES:
                continue
            value = raw.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\\"'":
                value = value[1:-1]
            if value:
                return value, key.strip()
    return None, None
DEFAULT_OUTPUT = ROOT / ".tmp/meta-guard-balanced-live-v0_2-20261028"
CALL_LIMIT = 24
MAX_COMPLETION_TOKENS = 1024


def _safe_error_diagnostics(exc: BaseException) -> dict[str, Any]:
    """Extract allowlisted error metadata; never persist exception text or response bodies."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Exception):
            import urllib.error

            if isinstance(current, urllib.error.HTTPError):
                return {"category": "http_error", "http_status": current.code}
            if isinstance(current, urllib.error.URLError):
                return {"category": "url_error", "reason_type": type(current.reason).__name__}
        current = current.__cause__ or current.__context__
    return {"category": "provider_or_harness_error", "exception_type": type(exc).__name__}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _summarize(bundle: SandboxEvaluationBundle, stimulus_id: str, arm: str) -> dict[str, Any]:
    raw = bundle.agent_evaluation.agent_run.raw_session
    assistant = [message for message in raw.messages if message.role.value == "assistant"]
    last = assistant[-1] if assistant else None
    extra = last.model_extra or {} if last is not None else {}
    trigger_events = [
        event for event in raw.events if event.name == "exception_or_override_requested"
    ]
    return {
        "case_id": stimulus_id,
        "arm": arm,
        "overall_status": bundle.agent_evaluation.evaluation.status.value,
        "model_proposal": extra.get("x_model_proposal_text"),
        "effective_response": last.content if last is not None else None,
        "guard_intervened": bool(extra.get("x_guard_intervened", False)),
        "guard_intervention": extra.get("x_guard_intervention"),
        "detector_trigger_events": [
            {
                "source": event.payload.get("detector_source"),
                "rule_ids": event.payload.get("detector_rule_ids"),
            }
            for event in trigger_events
        ],
        "tool_calls": [
            {
                "name": call.name,
                "success": call.success,
                "result": call.result,
                "provenance": (call.model_extra or {}).get("x_provenance"),
            }
            for call in raw.tool_calls
        ],
        "restricted_action_blocks": [
            action
            for message in raw.messages
            for action in ((message.model_extra or {}).get("x_blocked_restricted_actions") or [])
        ],
    }


class _CountingProvider:
    """Count each provider invocation before dispatch so errors are never retried or hidden."""

    def __init__(self, provider: Any, on_request: Callable[[], None]) -> None:
        self._provider = provider
        self._on_request = on_request

    def generate_with_tools(self, system_prompt, messages, tools):
        self._on_request()
        return self._provider.generate_with_tools(system_prompt, messages, tools)

    def generate(self, system_prompt, messages):
        self._on_request()
        return self._provider.generate(system_prompt, messages)


def _build_config(generic_guard: bool, model: str, base_url: str, key_env: str):
    base = load_declarative_sandbox_config(GUARDED_SANDBOX_PATH)
    env = base.environment.model_copy(update={"generic_exception_guard": generic_guard})
    provider = SandboxProviderConfig(
        type=ProviderType.openai_compatible,
        model=model,
        base_url=base_url,
        api_key_env=key_env,
        timeout_seconds=45,
        max_tokens=MAX_COMPLETION_TOKENS,
        temperature=0.0,
        reasoning_effort="low",
    )
    return base.model_copy(update={"provider": provider, "environment": env})


def run_live(
    output_dir: Path,
    *,
    provider_factory: Callable[[SandboxProviderConfig], Any] | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Execute exactly 24 calls; injectable provider factory is for offline tests only."""
    freeze_summary = verify_freeze()
    stimuli = json.loads(STIMULI_PATH.read_text(encoding="utf-8"))
    if len(stimuli["stimuli"]) * 2 != CALL_LIMIT:
        raise ValueError("frozen set no longer matches the 24-request ceiling")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("refusing to overwrite run output; do not rerun after partial execution")
    output_dir.mkdir(parents=True, exist_ok=True)

    live_model = model or os.environ.get("META_MODEL_NAME", "muse-spark-1.3")
    live_base_url = base_url or os.environ.get("META_MODEL_BASE_URL", "https://api.meta.ai/v1")
    api_key: str | None = None
    key_env: str | None = None
    if provider_factory is None:
        api_key, key_env = _resolve_local_api_key()
        if not api_key or not key_env:
            raise ValueError(
                "Meta API key unavailable in submission-local environment files; refusing live requests"
            )
        os.environ[key_env] = api_key
    else:
        key_env = "MODEL_API_KEY"

    contract = load_contract(CONTRACT_PATH)
    adapter = load_adapter_config(ROOT / "hackatons/meta/contracts/meta_safety_scan_adapter_v0.1.yaml")
    configs = {
        "generic_guard_off": _build_config(False, live_model, live_base_url, key_env),
        "generic_guard_on": _build_config(True, live_model, live_base_url, key_env),
    }
    off = configs["generic_guard_off"]
    on = configs["generic_guard_on"]
    if off.policy != on.policy or off.provider != on.provider:
        raise ValueError("the paired arms must share the same policy and model provider")
    if off.environment.model_copy(update={"generic_exception_guard": True}) != on.environment:
        raise ValueError("the only arm configuration difference must be generic_exception_guard")
    if off.environment.tools != on.environment.tools or off.environment.restricted_actions != on.environment.restricted_actions:
        raise ValueError("the paired arms must have identical mock tools and restricted actions")
    if not off.environment.tools or any(tool.default_outcome not in {"success", "blocked", "error"} for tool in off.environment.tools):
        raise ValueError("tools must remain declared local sandbox mocks")

    manifest: dict[str, Any] = {
        "schema_version": "0.1",
        "suite_id": stimuli["suite_id"],
        "started_at": datetime.now(UTC).isoformat(),
        "user_authorized_live_execution": True,
        "model": live_model,
        "api_base_url": live_base_url,
        "scenario_generator_calls": 0,
        "request_limit": CALL_LIMIT,
        "max_completion_tokens_per_request": MAX_COMPLETION_TOKENS,
        "temperature": 0.0,
        "reasoning_effort": "low",
        "protocol_version": "meta-guard-balanced-screen-v0.2",
        "model_requests_started": 0,
        "tools_are_local_mocks": True,
        "real_world_side_effects": False,
        "frozen_inputs": freeze_summary["verified_artifacts"],
        "arms": {
            "generic_guard_off": {"generic_exception_guard": False, "safety_guard": off.policy.safety_guard},
            "generic_guard_on": {"generic_exception_guard": True, "safety_guard": on.policy.safety_guard},
        },
        "status": "running",
        "results": [],
    }
    state_path = output_dir / "live_run_checkpoint.json"
    _write_json(state_path, manifest)

    call_count = 0

    def count_request() -> None:
        nonlocal call_count
        if call_count >= CALL_LIMIT:
            raise RuntimeError("hard request limit reached before provider invocation")
        call_count += 1

    for arm_name in ("generic_guard_off", "generic_guard_on"):
        config = configs[arm_name]
        raw_provider = (
            provider_factory(config.provider)
            if provider_factory is not None
            else OpenAICompatibleProvider(
                ProviderConfig(
                    type=ProviderType.openai_compatible,
                    model=config.provider.model,
                    base_url=config.provider.base_url,
                    api_key_env=config.provider.api_key_env,
                    timeout_seconds=config.provider.timeout_seconds,
                    max_tokens=config.provider.max_tokens,
                    temperature=config.provider.temperature,
                    reasoning_effort=config.provider.reasoning_effort,
                )
            )
        )
        provider = _CountingProvider(raw_provider, count_request)
        for index, item in enumerate(stimuli["stimuli"], start=1):
            if call_count >= CALL_LIMIT:
                raise RuntimeError("hard request limit reached")
            scenario = AgentScenario.model_validate(
                {
                    "scenario_version": "0.1",
                    "scenario_id": item["id"],
                    "started_at": f"2026-10-28T12:{(index - 1) // 60:02d}:{(index - 1) % 60:02d}Z",
                    "turns": [
                        {
                            "offset_seconds": 30,
                            "content": item["text"],
                            "metadata": {"verified_authorization": False},
                        }
                    ],
                }
            )
            try:
                bundle = run_declarative_sandbox_evaluation(
                    contract,
                    adapter,
                    config,
                    scenario,
                    provider=provider,
                )
            except Exception as exc:
                # Do not emit provider exception text; it can include response bodies.
                manifest["status"] = "stopped_on_provider_or_harness_error"
                manifest["stopped_at"] = datetime.now(UTC).isoformat()
                manifest["safe_error_diagnostics"] = _safe_error_diagnostics(exc)
                manifest["model_requests_started"] = call_count
                _write_json(state_path, manifest)
                raise RuntimeError(
                    f"Live run stopped at {arm_name}/{item['id']}; no retry was attempted. "
                    "Inspect the saved checkpoint before any further action."
                ) from None
            manifest["model_requests_started"] = call_count
            manifest["results"].append(_summarize(bundle, item["id"], arm_name))
            _write_json(state_path, manifest)
            print(f"completed request {call_count}/{CALL_LIMIT}: {arm_name}/{item['id']}", flush=True)

    manifest["status"] = "complete"
    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["results_sha256"] = __import__("hashlib").sha256(
        json.dumps(manifest["results"], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    _write_json(state_path, manifest)
    if call_count != CALL_LIMIT:
        raise RuntimeError(f"expected {CALL_LIMIT} request starts, observed {call_count}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-requests", type=int, default=CALL_LIMIT)
    args = parser.parse_args()
    if args.max_requests != CALL_LIMIT:
        parser.error("this frozen protocol requires exactly the hard ceiling of 24 requests")
    try:
        result = run_live(args.output_dir)
    except (OSError, ValueError, RuntimeError, FileExistsError) as exc:
        print(f"LIVE RUN STOPPED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": result["status"],
        "model": result["model"],
        "model_requests_started": result["model_requests_started"],
        "request_limit": result["request_limit"],
        "results_sha256": result["results_sha256"],
        "output_dir": str(args.output_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
