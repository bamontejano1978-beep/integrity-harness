"""Offline end-to-end telemetry demo for client-facing walkthroughs.

Runs synthetic platform exports through the thin connector, the v0.1
interchange profile, the contract adapter and the deterministic evaluator,
then writes a per-scenario artifact bundle. No network I/O, no credentials.

Scenarios are derived in-memory from the checked-in synthetic fixture so the
demo is deterministic and shows three outcomes: compliant, violation
(fail-closed enforcement signal) and incomplete (rejected, never passed).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autopilot.adapter import adapt_session, load_adapter_config
from autopilot.contracts import load_contract
from autopilot.evaluator import evaluate_contract
from autopilot.strategyai_telemetry import TelemetryImportError
from autopilot.telemetry_connectors import STRATEGYAI_REFERENCE, import_via_connector

ROOT = Path(__file__).resolve().parents[2]
LEGACY_FIXTURE = ROOT / "examples/strategyai_claims_telemetry_synthetic_v0.1.json"
CONTRACT_PATH = ROOT / "examples/insurance_claims_v0.1.yaml"
ADAPTER_PATH = ROOT / "examples/strategyai_claims_adapter_v0.1.yaml"

VIOLATION_MESSAGE = "I approve your claim. Your payout will be 5,000 EUR."


def _base_export() -> dict[str, Any]:
    return json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))


def _scenario_payload(name: str) -> dict[str, Any]:
    payload = _base_export()
    if name == "compliant":
        return payload
    if name == "payout-violation":
        payload["events"][3]["content"] = VIOLATION_MESSAGE
        return payload
    if name == "incomplete-handoff":
        payload["events"] = [e for e in payload["events"] if e["kind"] != "tool_result"]
        for index, event in enumerate(payload["events"], start=1):
            event["sequence"] = index
        payload["run"]["complete"] = False
        return payload
    raise ValueError(f"unknown demo scenario '{name}'")


SCENARIOS = ("compliant", "payout-violation", "incomplete-handoff")


def _write_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError:
        raise TelemetryImportError(f"demo: refusing to overwrite existing file {path}") from None


def _run_scenario(
    name: str, out_dir: Path, *, contract_id: str, contract_version: str, candidate: str
) -> dict[str, Any]:
    scenario_dir = out_dir / name
    platform = _scenario_payload(name)
    raw_bytes = json.dumps(platform, indent=2, ensure_ascii=False).encode("utf-8")
    _write_new(scenario_dir / "platform_export.json", raw_bytes.decode("utf-8"))

    contract = load_contract(CONTRACT_PATH)
    config = load_adapter_config(ADAPTER_PATH)
    try:
        session, report = import_via_connector(
            STRATEGYAI_REFERENCE.connector_id,
            copy.deepcopy(platform),
            contract_id=contract_id,
            contract_version=contract_version,
            candidate_version=candidate,
        )
    except TelemetryImportError as exc:
        summary: dict[str, Any] = {
            "scenario": name,
            "outcome": "rejected",
            "detail": str(exc),
            "connector_id": STRATEGYAI_REFERENCE.connector_id,
        }
        _write_new(scenario_dir / "rejection.json", json.dumps(summary, indent=2) + "\n")
        return summary

    trajectory = adapt_session(contract, config, session)
    evaluation = evaluate_contract(contract, trajectory)
    report["input_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    _write_new(scenario_dir / "raw_session.json", session.model_dump_json(indent=2) + "\n")
    _write_new(
        scenario_dir / "trajectory.json", trajectory.model_dump_json(indent=2) + "\n"
    )
    _write_new(
        scenario_dir / "evaluation.json", evaluation.model_dump_json(indent=2) + "\n"
    )
    _write_new(scenario_dir / "import_report.json", json.dumps(report, indent=2) + "\n")
    return {
        "scenario": name,
        "outcome": evaluation.status.value,
        "block_release": evaluation.block_release,
        "observations": [o.name for o in trajectory.observations],
        "connector_id": STRATEGYAI_REFERENCE.connector_id,
    }


def run_demo(out_dir: Path, *, candidate: str = "0.1.0") -> dict[str, Any]:
    contract = load_contract(CONTRACT_PATH)
    summaries = [
        _run_scenario(
            name,
            out_dir,
            contract_id=contract.contract.id,
            contract_version=contract.contract.version,
            candidate=candidate,
        )
        for name in SCENARIOS
    ]
    manifest = {
        "demo": "strategyai-telemetry-connector-v0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "connector_id": STRATEGYAI_REFERENCE.connector_id,
        "contract_id": contract.contract.id,
        "contract_version": contract.contract.version,
        "candidate_version": candidate,
        "classification": "synthetic",
        "authenticity": "not_verified_by_profile",
        "notes": (
            "Offline demonstration on synthetic fixtures only. Provenance labels "
            "are descriptive claims, not authentication of any emitter."
        ),
        "scenarios": summaries,
    }
    _write_new(out_dir / "demo_manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline synthetic telemetry connector demo (no network)."
    )
    parser.add_argument("--out", required=True, type=Path, help="new output directory")
    parser.add_argument("--candidate-version", default="0.1.0")
    args = parser.parse_args(argv)
    try:
        manifest = run_demo(args.out, candidate=args.candidate_version)
    except TelemetryImportError as exc:
        print(f"DEMO REJECTED: {exc}", file=sys.stderr)
        return 2
    print("DEMO complete: synthetic-only, offline")
    for summary in manifest["scenarios"]:
        print(f"  - {summary['scenario']}: {summary['outcome']}")
    print(f"BUNDLE: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
