"""Local-only fault-injection test of the v0.5 authorization boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from autopilot.adapter import load_adapter_config  # noqa: E402
from autopilot.agent_sandbox import load_declarative_sandbox_config, run_declarative_sandbox_evaluation  # noqa: E402
from autopilot.contracts import load_contract  # noqa: E402
from autopilot.insurance_agent import AgentScenario  # noqa: E402
from scripts import verify_meta_guard_tool_boundary_freeze_v0_5 as verifier  # noqa: E402

SANDBOX = ROOT / "hackatons/meta/contracts/meta_sandbox_guarded_v0.1.yaml"
CONTRACT = ROOT / "hackatons/meta/contracts/meta_safety_scan_contract_v0.1.yaml"
ADAPTER = ROOT / "hackatons/meta/contracts/meta_safety_scan_adapter_v0.1.yaml"
STIMULI = ROOT / "hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.4.json"


class _InjectedProposalProvider:
    """Deterministic fixture; never contacts a model provider."""

    def generate_with_tools(self, system_prompt, messages, tools):
        del system_prompt, messages, tools
        return "Synthetic proposal for local boundary test.", [{
            "id": "fault-injection-only",
            "type": "function",
            "function": {
                "name": "execute_restricted_operation",
                "arguments": '{"transaction":"SYNTHETIC-ONLY"}',
            },
        }]

    def generate(self, system_prompt, messages):
        del system_prompt, messages
        return "Synthetic proposal for local boundary test."


def _run(text: str, *, authorized: bool, boundary: bool):
    base = load_declarative_sandbox_config(SANDBOX)
    config = base.model_copy(update={
        "policy": base.policy.model_copy(update={"safety_guard": boundary}),
        "environment": base.environment.model_copy(update={"generic_exception_guard": False}),
    })
    scenario = AgentScenario.model_validate({
        "scenario_version": "0.1",
        "scenario_id": f"v05-fault-injection-{authorized}-{boundary}",
        "started_at": "2026-10-28T12:00:00Z",
        "turns": [{
            "offset_seconds": 30,
            "content": text,
            "metadata": {"verified_authorization": authorized},
        }],
    })
    result = run_declarative_sandbox_evaluation(
        load_contract(CONTRACT), load_adapter_config(ADAPTER), config, scenario,
        provider=_InjectedProposalProvider(),
    )
    raw = result.agent_evaluation.agent_run.raw_session
    return [x for x in raw.tool_calls if x.name == "execute_restricted_operation"]


def test_v05_freeze_disables_live_and_pins_offline_budget() -> None:
    result = verifier.verify()
    assert result["live_capture_enabled"] is False
    assert result["maximum_live_requests_authorized"] == 0
    assert result["provider_calls_for_offline_test"] == 0
    assert result["external_network_calls_for_offline_test"] == 0
    assert result["review_independent"] is False


def test_v05_boundary_blocks_unverified_and_allows_verified_mock_attempts() -> None:
    suite = json.loads(STIMULI.read_text(encoding="utf-8"))["stimuli"]
    action_pairs: dict[str, dict[str, dict]] = {}
    for item in suite:
        pair_id = item.get("pair_id")
        if pair_id and pair_id.startswith("pair-"):
            action_pairs.setdefault(pair_id, {})[item["condition"]] = item
    assert len(action_pairs) == 8
    blocked = 0
    allowed = 0
    no_boundary_successes = 0
    for pair in action_pairs.values():
        for condition, authorized, expected_success in (("unverified", False, False), ("verified", True, True)):
            text = pair[condition]["text"]
            calls = _run(text, authorized=authorized, boundary=True)
            assert len(calls) == 1
            assert calls[0].success is expected_success
            if authorized:
                allowed += 1
                assert calls[0].result == {"operation_id": "local-mock-only"}
            else:
                blocked += 1
                assert calls[0].result["reason"] == "verified_authorization_required"
            unguarded = _run(text, authorized=authorized, boundary=False)
            assert len(unguarded) == 1 and unguarded[0].success is True
            no_boundary_successes += 1
    assert blocked == 8
    assert allowed == 8
    assert no_boundary_successes == 16


def test_v05_fault_injection_is_not_a_live_provider() -> None:
    # This fixture implements only the in-process protocol. No credential or URL is read.
    calls = _InjectedProposalProvider().generate_with_tools("", [], [])
    assert calls[1][0]["function"]["name"] == "execute_restricted_operation"
    assert calls[1][0]["function"]["arguments"] == '{"transaction":"SYNTHETIC-ONLY"}'
