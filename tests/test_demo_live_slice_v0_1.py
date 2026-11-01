"""Offline tests for the one-case live demo slice."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hackatons.meta import run_meta_harness as harness  # noqa: E402
from scripts import package_meta_evidence as evidence  # noqa: E402

DEMO_CASE = "meta-heldout_authority_01_audit_chair"


def _arm(guard: bool) -> dict:
    turns = [
        {
            "ref": "message-initial-assistant",
            "role": "assistant",
            "content": "Hello.",
            "model_proposal_text": None,
            "guard_intervened": False,
            "guard_intervention": None,
        }
    ]
    for index in range(1, 4):
        turns.append(
            {
                "ref": f"turn-{index:03d}-user",
                "role": "user",
                "content": f"User turn {index}.",
                "model_proposal_text": None,
                "guard_intervened": False,
                "guard_intervention": None,
            }
        )
        turns.append(
            {
                "ref": f"turn-{index:03d}-assistant",
                "role": "assistant",
                "content": f"Effective response {index}.",
                "model_proposal_text": f"Model proposal {index}.",
                "guard_intervened": guard,
                "guard_intervention": "scenario_guard" if guard else None,
            }
        )
    return {
        "scenario_id": DEMO_CASE,
        "provider_model": "muse-spark-1.3",
        "safety_guard_configured": guard,
        "overall_status": "pass" if guard else "fail",
        "conversation_turns": turns,
    }


def _demo_report() -> dict:
    return {
        "project": "Integrity Harness — Automated Behavioral Safety Scan",
        "meta_model_name": "muse-spark-1.3",
        "meta_model_generator_mode": "deterministic_offline",
        "behavioral_agents_mode": "live_meta_model_with_local_mock_tools",
        "external_api_calls_made": True,
        "side_effects_enabled": False,
        "suite_kind": "demo_live_slice",
        "suite_version": evidence.DEMO_SUITE_VERSION,
        "live_run_limits": {
            "maximum_model_requests": 6,
            "requests_observed": 6,
            "automatic_retries": 0,
            "tools_are_local_mocks": True,
            "real_world_side_effects": False,
            "max_completion_tokens": 1024,
            "provider_timeout_seconds": 45,
        },
        "demo": {
            "scenario_id": DEMO_CASE,
            "axis": "authority",
            "turns": 3,
            "arms": 2,
            "started_at": "2026-10-31T12:00:00Z",
        },
        "totals": {
            "scenarios_evaluated": 1,
            "trajectories_executed": 2,
            "unguarded_status_counts": {
                "pass": 0,
                "fail": 1,
                "indeterminate": 0,
                "not_applicable": 0,
            },
            "guarded_status_counts": {
                "pass": 1,
                "fail": 0,
                "indeterminate": 0,
                "not_applicable": 0,
            },
        },
        "comparisons": [
            {
                "scenario_id": DEMO_CASE,
                "suite_source": "demo_live_slice",
                "pressure_type": "authority",
                "before_remediation_unguarded": _arm(False),
                "after_remediation_guarded": _arm(True),
            }
        ],
    }


def test_request_budget_refuses_seventh_call_before_provider() -> None:
    calls: list[int] = []

    class Inner:
        def generate_with_tools(self, system_prompt, messages, tools):
            calls.append(1)
            return "ok", []

        def generate(self, system_prompt, messages):
            calls.append(1)
            return "ok"

    budget = harness.RequestBudget(6)
    provider = harness.BudgetedProvider(Inner(), budget, "unguarded")
    for _ in range(6):
        provider.generate_with_tools("system", [], [])
    with pytest.raises(harness.RequestBudgetExceeded):
        provider.generate_with_tools("system", [], [])
    assert len(calls) == 6
    assert budget.requests_started == 6
    assert budget.requests_by_arm == {"unguarded": 6}


def test_demo_requires_live_agent_and_rejects_other_modes() -> None:
    with pytest.raises(ValueError, match="live-agent"):
        harness.run_meta_safety_scan(demo_only=DEMO_CASE)
    with pytest.raises(ValueError, match="cannot be combined"):
        harness.run_meta_safety_scan(
            demo_only=DEMO_CASE, live_agent=True, held_out_only=True
        )


def test_demo_report_validates_structure_not_a_forced_failure() -> None:
    report = _demo_report()
    summary = evidence.validate_report(report)
    assert summary["scenario_count"] == 1
    assert summary["assistant_proposals_recorded"] == 6
    assert summary["live_run_limits"]["maximum_model_requests"] == 6

    report["comparisons"][0]["before_remediation_unguarded"]["overall_status"] = "pass"
    report["comparisons"][0]["after_remediation_guarded"]["overall_status"] = "pass"
    assert evidence.validate_report(report)["scenario_count"] == 1


def test_demo_report_rejects_seven_requests_or_fewer_proposals() -> None:
    report = _demo_report()
    report["live_run_limits"]["requests_observed"] = 7
    with pytest.raises(ValueError, match="observed requests"):
        evidence.validate_report(report)

    report = _demo_report()
    report["comparisons"][0]["after_remediation_guarded"]["conversation_turns"][-1][
        "model_proposal_text"
    ] = None
    with pytest.raises(ValueError, match="assistant proposals"):
        evidence.validate_report(report)


@pytest.mark.parametrize("case_id", list(harness.LAB_CASES))
def test_demo_run_makes_six_budgeted_calls_and_packager_ready_report(
    case_id: str,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.calls = 0

        def generate_with_tools(self, system_prompt, messages, tools):
            self.calls += 1
            return "I will keep the payment on hold and route it for review.", []

        def generate(self, system_prompt, messages):
            self.calls += 1
            return "I will keep the payment on hold and route it for review."

    fake = FakeProvider()
    monkeypatch.setenv("META_MODEL_API_KEY", "offline-test-key")
    monkeypatch.setenv("META_MODEL_NAME", "offline-test-model")
    monkeypatch.setattr(harness, "_live_provider", lambda config: fake)

    output_dir = tmp_path / "run"
    report = harness.run_meta_safety_scan(
        output_dir=output_dir,
        live_agent=True,
        demo_only=case_id,
    )

    assert fake.calls == 6
    assert report["live_run_limits"]["requests_observed"] == 6
    assert report["live_run_limits"]["requests_by_arm"] == {
        "unguarded": 3,
        "guarded": 3,
    }
    assert report["totals"]["core_scenarios"] == 0
    assert report["totals"]["expanded_variants"] == 0
    assert report["totals"]["demo_scenarios"] == 1
    assert report["demo"]["scenario_id"] == case_id
    assert report["demo"]["axis"] == harness.LAB_CASES[case_id]
    for arm in ("before_remediation_unguarded", "after_remediation_guarded"):
        proposals = [turn for turn in report["comparisons"][0][arm]["conversation_turns"]
                     if turn.get("model_proposal_text") is not None]
        assert len(proposals) == 3
        assert all(turn["model_tool_proposals"] == [] for turn in proposals)
    assert (output_dir / "demo_scenario.json").exists()
    assert evidence.validate_report(report)["assistant_proposals_recorded"] == 6
    output = tmp_path / "signed.zip"
    evidence.build_package(output_dir / "meta_safety_scan_report.json", output,
                           evidence.SUBMISSION_ROOT, tmp_path / "signing.pem")
    assert evidence.verify_package(output)["signed"] is True
    assert evidence.tamper_test_package(output)["rejected"] is True
    with zipfile.ZipFile(output) as archive:
        assert case_id in archive.read("README.md").decode("utf-8")


def test_new_demo_validator_rejects_mismatched_case_and_missing_proposals() -> None:
    report = _demo_report()
    report["suite_version"] = evidence.LAB_SUITE_VERSION
    for arm in ("before_remediation_unguarded", "after_remediation_guarded"):
        for turn in report["comparisons"][0][arm]["conversation_turns"]:
            if turn.get("model_proposal_text") is not None:
                turn["model_tool_proposals"] = []
    assert evidence.validate_report(report)["scenario_count"] == 1
    report["demo"]["axis"] = "urgency"
    with pytest.raises(ValueError, match="axis"):
        evidence.validate_report(report)
    report["demo"]["axis"] = "authority"
    del report["comparisons"][0]["before_remediation_unguarded"]["conversation_turns"][-1]["model_tool_proposals"]
    with pytest.raises(ValueError, match="missing original tool proposals"):
        evidence.validate_report(report)


def test_demo_package_builds_verifies_and_rejects_tampering(tmp_path: Path) -> None:
    report = _demo_report()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report_path = run_dir / "meta_safety_scan_report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_path.with_suffix(".md").write_text("# demo\n", encoding="utf-8")
    (run_dir / "demo_scenario.json").write_text(
        json.dumps(
            {
                "scenario_id": DEMO_CASE,
                "scenario": {"id": "heldout_authority_01_audit_chair"},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "demo-signed.zip"
    built = evidence.build_package(
        report_path, output, evidence.SUBMISSION_ROOT, tmp_path / "private.pem"
    )
    verified = evidence.verify_package(output)
    tamper = evidence.tamper_test_package(output)

    assert built["signature"]["algorithm"] == "Ed25519"
    assert verified["signed"] is True
    assert verified["verified_artifacts"] > 0
    assert verified["experiment"]["suite_kind"] == "demo_live_slice"
    assert tamper["rejected"] is True
    assert tamper["changed_bytes"] == 1
    assert "signature" in tamper["reason"].lower()
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "run/demo_scenario.json" in names
        assert "signature.json" in names
