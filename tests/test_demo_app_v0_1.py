"""Offline tests for the bounded live demo app."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_app import (  # noqa: E402
    DemoLedger,
    DemoLimits,
    DemoOutcome,
    DemoService,
    _case_intro,
    _clause_catalog,
    _code_block,
    _run_outputs,
    _replay_live,
    _replay_proposal,
    _synthetic_control,
    build_demo,
    visitor_hash,
)
from hackatons.meta import run_meta_harness as harness  # noqa: E402


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def generate_with_tools(self, system_prompt, messages, tools):
        self.calls += 1
        return "I will keep the payment on hold and route it for review.", []

    def generate(self, system_prompt, messages):
        self.calls += 1
        return "I will keep the payment on hold and route it for review."


def _live_runner(monkeypatch: pytest.MonkeyPatch) -> tuple:
    fake = FakeProvider()
    monkeypatch.setenv("META_MODEL_API_KEY", "offline-test-key")
    monkeypatch.setenv("META_MODEL_NAME", "offline-test-model")
    monkeypatch.setattr(harness, "_live_provider", lambda config: fake)

    def runner(*, output_dir: Path, case_id: str):
        return harness.run_meta_safety_scan(
            output_dir=output_dir,
            live_agent=True,
            demo_only=case_id,
        )

    return runner, fake


def test_ledger_enforces_visitor_day_total_and_request_caps(tmp_path: Path) -> None:
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    ledger = DemoLedger(
        tmp_path / "ledger.sqlite3",
        DemoLimits(
            max_runs_per_visitor_24h=1,
            max_runs_per_day=2,
            max_runs_total=3,
            max_requests_total=18,
        ),
    )
    assert ledger.reserve("a", now).allowed is True
    assert ledger.reserve("a", now).allowed is False
    assert ledger.reserve("b", now).allowed is True
    assert ledger.reserve("c", now).allowed is False
    assert ledger.reserve("d", now + timedelta(days=1, seconds=1)).allowed is True
    assert ledger.reserve("e", now + timedelta(days=2)).allowed is False

    request_ledger = DemoLedger(
        tmp_path / "requests.sqlite3",
        DemoLimits(
            max_runs_per_visitor_24h=10,
            max_runs_per_day=10,
            max_runs_total=10,
            max_requests_total=12,
        ),
    )
    assert request_ledger.reserve("x", now).allowed is True
    assert request_ledger.reserve("y", now).allowed is True
    denied = request_ledger.reserve("z", now)
    assert denied.allowed is False
    assert denied.reason == "requests"


def test_service_refuses_without_a_key_and_does_not_reserve(tmp_path: Path) -> None:
    called: list[int] = []

    def runner(**_kwargs):
        called.append(1)
        raise AssertionError("runner must not be called without a key")

    service = DemoService(tmp_path, key_check=lambda: False, runner=runner)
    outcome = service.run(None)

    assert outcome.ok is False
    assert "REFUSED" in outcome.message
    assert called == []
    assert service.ledger.stats()["runs_total"] == 0


def _fake_request(
    host: str, session: str, headers: dict[str, str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        session_hash=session,
        request=SimpleNamespace(
            client=SimpleNamespace(host=host), headers=headers or {}
        ),
    )


def test_visitors_behind_one_address_do_not_share_quota(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_TRUSTED_PROXIES", raising=False)
    first = visitor_hash(_fake_request("203.0.113.7", "session-a"), "salt")
    second = visitor_hash(_fake_request("203.0.113.7", "session-b"), "salt")
    assert first != second


def test_forwarded_header_is_ignored_without_a_declared_proxy(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_TRUSTED_PROXIES", raising=False)
    plain = visitor_hash(_fake_request("198.51.100.7", "", {}), "salt")
    spoofed = visitor_hash(
        _fake_request("198.51.100.7", "", {"x-forwarded-for": "1.1.1.1"}), "salt"
    )
    assert plain == spoofed


def test_trusted_proxy_ignores_a_client_supplied_prefix(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_TRUSTED_PROXIES", "10.0.0.0/8")
    first = _fake_request("10.0.0.5", "", {"x-forwarded-for": "1.1.1.1, 203.0.113.9"})
    second = _fake_request("10.0.0.5", "", {"x-forwarded-for": "9.9.9.9, 203.0.113.9"})
    assert visitor_hash(first, "salt") == visitor_hash(second, "salt")
    other = _fake_request("10.0.0.5", "", {"x-forwarded-for": "203.0.113.10"})
    assert visitor_hash(first, "salt") != visitor_hash(other, "salt")


def test_untrusted_peer_cannot_assert_a_forwarded_address(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_TRUSTED_PROXIES", "10.0.0.5")
    one = visitor_hash(
        _fake_request("198.51.100.7", "", {"x-forwarded-for": "1.1.1.1"}), "salt"
    )
    two = visitor_hash(
        _fake_request("198.51.100.7", "", {"x-forwarded-for": "2.2.2.2"}), "salt"
    )
    assert one == two


def test_service_runs_signs_verifies_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, fake = _live_runner(monkeypatch)
    calls: list[int] = []

    def counted_runner(*, output_dir: Path, case_id: str):
        calls.append(1)
        return runner(output_dir=output_dir, case_id=case_id)

    service = DemoService(
        tmp_path,
        limits=DemoLimits(max_runs_per_visitor_24h=1),
        key_check=lambda: True,
        runner=counted_runner,
    )
    outcome = service.run(None)

    assert outcome.ok is True, outcome.message
    assert fake.calls == 6
    assert outcome.report is not None
    assert outcome.report["live_run_limits"]["requests_observed"] == 6
    assert outcome.report["live_run_limits"]["requests_by_arm"] == {
        "unguarded": 3,
        "guarded": 3,
    }
    assert "No restricted operation was proposed" in _replay_live(outcome)
    assert fake.calls == 6

    signed = service.sign(outcome.run_id)
    assert signed.ok is True, signed.message
    assert signed.package_path is not None
    assert signed.package_path.exists()
    assert signed.verification is not None
    assert signed.verification["signed"] is True
    assert signed.tamper is not None
    assert signed.tamper["rejected"] is True
    assert len(calls) == 1

    again = service.run(None)
    assert again.ok is False
    assert "24 h" in again.message
    assert len(calls) == 1


def test_ui_has_no_free_text_input(tmp_path: Path) -> None:
    service = DemoService(tmp_path, key_check=lambda: False)
    demo = build_demo(service)

    assert isinstance(demo, gr.Blocks)
    blocks = list(demo.blocks.values())
    assert not any(isinstance(block, gr.Textbox) for block in blocks)
    assert any(isinstance(block, gr.Button) for block in blocks)


def test_case_and_timeline_preserve_recorded_evidence(tmp_path: Path) -> None:
    intro = _case_intro()
    assert "approval evidence is not attached" in intro
    assert "Release the settlement now" in intro
    assert "metadata" in intro
    assert "Selected case" in intro
    assert _code_block("untrusted ````` text").startswith("``````text\n")

    def arm(status: str) -> dict:
        turns = []
        for index in range(1, 4):
            turns.extend([
                {"ref": f"turn-{index:03d}-user", "content": f"request {index}"},
                {
                    "ref": f"turn-{index:03d}-assistant",
                    "role": "assistant",
                    "content": f"effective {status} {index}",
                    "model_proposal_text": f"proposal {status} {index}",
                    "guard_intervention": None,
                },
            ])
        return {
            "overall_status": status,
            "conversation_turns": turns,
            "tool_calls": [],
            "clause_results": [{
                "clause_id": "boundary",
                "status": status,
                "reason": "recorded reason",
            }],
        }

    before, after = arm("fail"), arm("indeterminate")
    after["tool_calls"] = [{
        "ref": "turn-002-tool-01-create_human_escalation_handoff",
        "name": "create_human_escalation_handoff",
        "arguments": {"queue": "compliance_review_queue"},
        "success": True,
        "provenance": {
            "source": "model_proposal",
            "guard_intervention": "canonicalized_model_handoff",
            "model_proposal": {"arguments": {}},
        },
    }]
    report = {
        "meta_model_name": "test-model",
        "live_run_limits": {"requests_observed": 6},
        "external_api_calls_made": False,
        "side_effects_enabled": False,
        "comparisons": [{
            "before_remediation_unguarded": before,
            "after_remediation_guarded": after,
        }],
    }
    service = DemoService(tmp_path, key_check=lambda: False)
    result = _run_outputs(DemoOutcome(True, run_id="test", report=report), service)
    assert len(result) == 14
    cards = result[2]
    assert "Contract: fail" in cards and "Contract: indeterminate" in cards
    assert "canonicalized_model_handoff" not in cards  # final turn did not intervene
    assert "Local mocks only" in cards
    timeline, explanations, diagnostics, table = result[8:12]
    assert "independent calls" in timeline
    assert "request 1" in timeline and "request 3" in timeline
    assert "proposal fail 2" in timeline and "effective indeterminate 2" in timeline
    assert '"effective_arguments"' in timeline
    assert '"model_original_proposal"' in timeline
    assert "canonicalized_model_handoff" in timeline
    assert "No tool calls were recorded" in timeline
    assert "indeterminate" in explanations and "not enough evidence to decide" in explanations
    assert "recorded reason" in explanations
    assert "Clause-by-clause diagnosis" in diagnostics
    assert "Required:" in diagnostics and "Not described in the loaded contract." in diagnostics
    assert table == [["boundary", "fail", "indeterminate"]]

    refused = _run_outputs(DemoOutcome(False, message="refused"), service)
    assert len(refused) == len(result)
    assert refused[1] == "refused"


def test_summary_cards_escape_model_and_tool_output(tmp_path: Path) -> None:
    report = {
        "meta_model_name": "test", "live_run_limits": {"requests_observed": 6},
        "comparisons": [{
            "before_remediation_unguarded": {
                "overall_status": "pass", "conversation_turns": [{"role": "assistant", "content": "<img src=x onerror=alert(1)>"}],
                "tool_calls": [{"name": "<script>", "success": False}],
            },
            "after_remediation_guarded": {
                "overall_status": "fail", "conversation_turns": [{"role": "assistant", "content": "safe", "model_proposal_text": "<script>"}],
            },
        }],
    }
    cards = _run_outputs(DemoOutcome(True, report=report), DemoService(tmp_path))[2]
    assert "<img src=x" not in cards and "<script>" not in cards
    assert "&lt;img" in cards and "&lt;script&gt;" in cards


def test_visitor_hash_is_stable_and_salted() -> None:
    assert visitor_hash(None, "salt") == visitor_hash(None, "salt")
    assert visitor_hash(None, "salt") != visitor_hash(None, "other")


@pytest.mark.parametrize("authorized", [False, True])
def test_local_replay_uses_same_proposal_and_honors_authorization(authorized: bool) -> None:
    call = {"id": "fixture", "type": "function", "function": {
        "name": "execute_restricted_operation", "arguments": "{}"}}
    results = _replay_proposal("synthetic", [call], authorized=authorized)
    assert results["sin_frontera"][0]["success"] is True
    assert results["con_frontera"][0]["success"] is authorized
    if not authorized:
        assert results["con_frontera"][0]["result"]["reason"] == "verified_authorization_required"
    assert "fixture creates this operation proposal, not Meta" in _synthetic_control()


def test_replay_uses_recorded_live_arguments_and_preserves_no_attempt() -> None:
    proposal = {"id": "from-model", "type": "function", "function": {
        "name": "execute_restricted_operation", "arguments": '{"transaction":"T-1"}'}}
    report = {"comparisons": [{"before_remediation_unguarded": {
        "conversation_turns": [{"ref": "turn-003-assistant", "model_proposal_text": "go",
                                "model_tool_proposals": [proposal]}]}}]}
    text = _replay_live(DemoOutcome(True, report=report))
    assert "T-1" in text
    assert "verified_authorization_required" in text
    assert "No restricted operation was proposed" not in text
    report["comparisons"][0]["before_remediation_unguarded"]["conversation_turns"][0].pop("model_tool_proposals")
    assert "replay unavailable" in _replay_live(DemoOutcome(True, report=report))


def test_clause_diagnostics_use_recorded_contract_text() -> None:
    catalog = _clause_catalog()
    assert "SI-ENTONCES-RESTATE-BOUNDARY-001" in catalog
    assert len(catalog) == 5
    requirement = " ".join(str(catalog["NO-DEBE-UNAUTHORIZED-OVERRIDE-001"]["description"]).split())
    assert requirement.startswith("The agent must not approve or execute a restricted operation")


def test_invalid_case_is_refused_before_reservation(tmp_path: Path) -> None:
    service = DemoService(tmp_path, key_check=lambda: True)
    outcome = service.run(None, "not-allowed")
    assert not outcome.ok
    assert service.ledger.stats()["runs_total"] == 0
