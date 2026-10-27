"""Command-line interface for Behavioural Autopilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Sequence

import yaml
from pydantic import ValidationError

if TYPE_CHECKING:  # pragma: no cover - annotation only, keeps the CLI import light
    from autopilot.drift_budget import BudgetPolicy, DriftCoalescence
    from autopilot.drift_response import DriftResponse
    from autopilot.drift_watch import DriftWatchReport
    from autopilot.sensor_health import SensorHealthReport

from autopilot.adapter import adapt_session, load_adapter_config, load_raw_session
from autopilot.agent_cohort import load_agent_cohort, run_agent_cohort
from autopilot.agent_sandbox import (
    load_declarative_sandbox_config,
    run_declarative_sandbox_evaluation,
)
from autopilot.swarm import (
    generate_swarm_keypair,
    _canonical_json_bytes,
    load_swarm_channel,
    load_swarm_member_evaluation_paths,
    load_swarm_pair,
    load_swarm_report,
    run_swarm_pair,
    sign_swarm_report,
    verify_swarm_report,
)
from autopilot.ai_act_cycle import (
    SignedMonitorCycleReceipt,
    generate_cycle_keypair,
    run_monitor_cycle,
    verify_cycle_receipt,
)
from autopilot.ai_act_monitor import (
    build_longitudinal_report,
    load_evaluations,
    load_obligation_map,
    load_system_profile,
    write_report_bundle,
)
from autopilot.ai_act_store import AIActLongitudinalStore
from autopilot.audit import (
    AuditLedger,
    record_guard_decision,
    KeyRevocation,
    ReleaseReview,
    ReviewRevocation,
    SandboxReport,
    ChangeCard,
    export_ledger_bundle,
    load_audit_artifact,
    record_change_card,
    record_release_review,
    record_sandbox_report,
    revoke_release_review,
    revoke_signing_key,
    verify_audit_ledger,
)
from autopilot.clause_coverage import (
    ClauseCoverageInputError,
    collect_clause_coverage,
    declared_clause_ids,
    gate_satisfied,
    load_audit_csv,
    render_clause_coverage,
    render_detector_cards_document,
    write_detector_cards,
)
from autopilot.contracts import DSLValidationError, load_contract
from autopilot.contracts.errors import ValidationIssue
from autopilot.guard_server import GuardServer, serve as serve_guard_server
from autopilot.diagnostics import generate_change_cards, render_change_card_markdown
from autopilot.drift import DriftRefusalReason, record_drift_refusal
from autopilot.monitoring_loop import (
    MonitorRefusalError,
    MonitorRefusalReason,
    record_monitor_refusal,
)
from autopilot.enforcement import (
    EnforcementAdapter,
    ToolPolicyFormatError,
    build_guard_decision_payload,
    load_signed_trajectory,
    load_tool_policy,
    sign_guard_decision,
    sign_trajectory_payload,
)
from autopilot.evaluator import evaluate_contract
from autopilot.execution_boundary import (
    BoundaryBackend,
    BoundaryStatus,
    DockerBoundaryConfig,
    load_boundary_plan,
    load_boundary_report,
    run_execution_boundary,
    verify_boundary_report,
)
from autopilot.incidents import IncidentRegistry, detect_incidents
from autopilot.insurance_agent import (
    AgentExecutionError,
    load_agent_config,
    load_agent_scenario,
    run_agent_evaluation,
)
from autopilot.lead_discovery import (
    discover_leads,
    load_search_results,
    write_crm_csv,
)
from autopilot.lead_radar import (
    load_radar_config,
    run_lead_radar,
    write_raw_results_csv,
)
from autopilot.release_review import (
    ReviewDecision,
    ReviewerIdentity,
    create_release_review,
    enforce_envelope_and_audit,
    generate_review_keypair,
    load_release_review,
    load_sandbox_report,
    verify_release_review,
)
from autopilot.sandbox import (
    SandboxApproval,
    SandboxStatus,
    load_change_card,
    run_candidate_sandbox,
)
from autopilot.system1_events import HarnessEventWindow, load_harness_event_window
from autopilot.trajectory import load_trajectory
from autopilot.telemetry_connectors import main as _telemetry_dry_run_main

from autopilot.asch import (
    AschDesign,
    AschMember,
    format_sweep as format_asch_sweep,
    run_asch_sweep,
    sign_sweep_samples as sign_asch_sweep_samples,
    write_sweep as write_asch_sweep,
)
from autopilot.bystander import (
    BystanderDesign,
    BystanderMember,
    format_sweep as format_bystander_sweep,
    run_bystander_sweep,
    sign_sweep_samples as sign_bystander_sweep_samples,
    write_sweep as write_bystander_sweep,
)
from autopilot.cascade import (
    CASCADE_CASES as _CASCADE_CASES,
    CASCADE_VARIANTS as _CASCADE_VARIANTS,
    CascadeDesign,
    CascadeMember,
    format_sweep as format_cascade_sweep,
    run_cascade_sweep,
    sign_sweep_samples as sign_cascade_sweep_samples,
    write_sweep as write_cascade_sweep,
)
from autopilot.social_swarm import verify_social_reports


#: The variants each social experiment can run; the CLI validates --variant here.
_SOCIAL_VARIANTS: dict[str, tuple[str, ...]] = {
    "bystander": ("diffusion", "pluralistic_ignorance"),
    "asch": ("unanime", "con_disidente"),
    "cascade": tuple(_CASCADE_VARIANTS),
}
_DEFAULT_SOCIAL_SIZES = (1, 2, 3, 4, 5)


def _parse_sizes(value: str) -> tuple[int, ...]:
    """Parse a comma-separated group/majority size list."""

    try:
        sizes = tuple(int(item) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--sizes expects comma-separated integers"
        ) from exc
    if not sizes or any(size < 1 for size in sizes):
        raise argparse.ArgumentTypeError("--sizes expects positive, comma-separated sizes")
    return sizes


def _parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(prog="behavioural-autopilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a DSL v0.1 contract")
    validate.add_argument("path", type=Path, help="path to a YAML contract")
    validate.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a machine-readable result",
    )
    validate.add_argument(
        "--normalized",
        action="store_true",
        help="include the normalized contract in JSON output",
    )

    evaluate = subparsers.add_parser(
        "evaluate",
        help="evaluate a canonical trajectory against a DSL v0.1 contract",
    )
    evaluate.add_argument("contract", type=Path, help="path to a YAML contract")
    evaluate.add_argument("trajectory", type=Path, help="path to a YAML trajectory")
    evaluate.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the complete machine-readable evaluation",
    )
    evaluate.add_argument(
        "--trajectory-trust-key",
        type=Path,
        help="verify the trajectory against this signed-envelope Ed25519 public key",
    )
    evaluate.add_argument(
        "--trajectory-envelope",
        type=Path,
        help="signed trajectory envelope to verify (default: trajectory as unsigned YAML)",
    )

    adapt = subparsers.add_parser(
        "adapt",
        help="adapt a raw session into a canonical trajectory",
    )
    adapt.add_argument("contract", type=Path, help="path to a YAML contract")
    adapt.add_argument("adapter", type=Path, help="path to an adapter configuration")
    adapt.add_argument("session", type=Path, help="path to a raw session")
    adapt.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    adapt.add_argument("--output", type=Path, help="write the trajectory to this path")

    trajectory_sign = subparsers.add_parser(
        "trajectory-sign",
        help="wrap a canonical trajectory in a signed Ed25519 envelope",
    )
    trajectory_sign.add_argument("trajectory", type=Path, help="path to a YAML trajectory")
    trajectory_sign.add_argument(
        "--private-key", type=Path, required=True, help="producer Ed25519 private key (PEM)"
    )
    trajectory_sign.add_argument(
        "--public-key", type=Path, required=True, help="producer Ed25519 public key (PEM)"
    )
    trajectory_sign.add_argument(
        "--producer",
        default="trajectory-producer",
        help="label recorded in the envelope for the producing system",
    )
    trajectory_sign.add_argument("--output", type=Path, help="write the envelope to this path")
    trajectory_sign.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the envelope as JSON"
    )

    monitor_loop = subparsers.add_parser(
        "monitor-loop",
        help="run one emission of the continuous monitoring loop (sign → verify → evaluate → snapshot)",
        description=(
            "One scheduled emission of the System 1 monitoring loop: signs the trajectory, "
            "verifies it against the trust key, evaluates the contract deterministically and "
            "re-emits the guard's signed evaluation snapshot with a fresh TTL. "
            "Schedule it every N seconds (cron/Task Scheduler) for continuous monitoring."
        ),
    )
    monitor_loop.add_argument(
        "trajectory",
        type=Path,
        help="path to a YAML/JSON trajectory or directory of signed A2 envelopes",
    )
    monitor_loop.add_argument("contract", type=Path, help="path to a YAML contract")
    monitor_loop.add_argument(
        "--producer-private-key", type=Path, required=True, help="producer Ed25519 private key (PEM)"
    )
    monitor_loop.add_argument(
        "--producer-public-key", type=Path, required=True, help="producer Ed25519 public key (PEM)"
    )
    monitor_loop.add_argument(
        "--trust-key",
        type=Path,
        required=True,
        help="public key the loop itself verifies against before emitting anything",
    )
    monitor_loop.add_argument(
        "--sensor-trust-key",
        type=Path,
        help="require and verify a signed A2 trajectory from this separate sensor trust key",
    )
    monitor_loop.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory receiving the envelope, evaluation, guard snapshot and cycle record",
    )
    monitor_loop.add_argument(
        "--interval-seconds",
        type=float,
        default=300.0,
        help="declared scheduling interval in seconds (default: 300 = 5 minutes)",
    )
    monitor_loop.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="number of scheduled emissions to run every --interval-seconds (default: 1)",
    )
    monitor_loop.add_argument(
        "--snapshot-ttl-seconds",
        type=float,
        default=900.0,
        help=(
            "TTL of the emitted evaluation snapshot and maximum accepted age of "
            "harness-derived observations (default: 900)"
        ),
    )
    monitor_loop.add_argument(
        "--produced-by",
        default="integrity-monitoring-loop",
        help="label recorded in the envelope and cycle for the producing system",
    )
    monitor_loop.add_argument(
        "--previous-cycle",
        type=Path,
        help="previous cycle-*.json record, for fingerprint continuity (auto-detected from --output-dir if omitted)",
    )
    monitor_loop.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help=(
            "append-only ledger receiving one monitoring_loop_cycle event per "
            "emission and a declared record for every refused cycle"
        ),
    )
    monitor_loop.add_argument(
        "--drift-watch",
        action="store_true",
        help=(
            "after each scheduled cycle, compare the cycle history against a signed "
            "baseline and record a drift signal when behaviour changes"
        ),
    )
    monitor_loop.add_argument(
        "--drift-baseline",
        type=Path,
        help=(
            "signed baseline used by --drift-watch (default: "
            "<output-dir>/drift-baseline.json, anchored on the first learning window)"
        ),
    )
    monitor_loop.add_argument(
        "--drift-authority-private-key",
        type=Path,
        help="baseline authority Ed25519 private key (required with --drift-watch)",
    )
    monitor_loop.add_argument(
        "--drift-authority-public-key",
        type=Path,
        help="baseline authority Ed25519 public key (required with --drift-watch)",
    )
    monitor_loop.add_argument(
        "--drift-learning-cycles",
        type=int,
        default=3,
        help="cycles used to anchor the drift baseline (default: 3)",
    )
    monitor_loop.add_argument(
        "--drift-window-cycles",
        type=int,
        default=3,
        help="trailing cycles compared on each drift watch tick (default: 3)",
    )
    monitor_loop.add_argument(
        "--drift-respond",
        action="store_true",
        help=(
            "close the circuit: a recorded drift signal triggers the deep evaluation "
            "and re-emits the guard snapshot in place (implies --drift-watch). A signal "
            "a crashed process left unanswered is answered at the start of the next "
            "tick. Coalescence and the declared budget still decide whether a run is spent"
        ),
    )
    monitor_loop.add_argument(
        "--drift-budget-max-runs",
        type=int,
        default=None,
        help=(
            "declared maximum of recorded deep runs per sliding budget window for the "
            "contract, used by --drift-respond (default: 3)"
        ),
    )
    monitor_loop.add_argument(
        "--drift-budget-window-seconds",
        type=float,
        default=None,
        help=(
            "declared length of the sliding budget window in seconds, used by "
            "--drift-respond (default: 2592000 = 30 days)"
        ),
    )
    monitor_loop.add_argument(
        "--health",
        action="store_true",
        help=(
            "after the tick, print one SENSOR HEALTH line: snapshot age and remaining "
            "TTL, signals still pending and recovery gaps"
        ),
    )
    monitor_loop.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the cycle record as JSON"
    )

    system1_sensor = subparsers.add_parser(
        "system1-sensor",
        help="sign a complete harness event window as a canonical System 1 trajectory",
        description=(
            "Emit a signed trajectory from a complete, minimized harness window. "
            "This authenticates the configured producer key and projection only; it "
            "is not live runtime telemetry or proof of external event completeness."
        ),
    )
    system1_sensor.add_argument("event_window", type=Path, help="minimized harness event window JSON")
    system1_sensor.add_argument("contract", type=Path, help="contract whose vocabulary constrains observations")
    system1_sensor.add_argument("--candidate-version", required=True, help="candidate semantic version")
    system1_sensor.add_argument("--producer-private-key", type=Path, required=True, help="producer Ed25519 private key PEM")
    system1_sensor.add_argument("--producer-public-key", type=Path, required=True, help="producer Ed25519 public key PEM")
    system1_sensor.add_argument("--trust-key", type=Path, required=True, help="configured producer trust public key PEM")
    system1_sensor.add_argument("--output", type=Path, required=True, help="new signed trajectory envelope JSON path")
    system1_sensor.add_argument("--ledger", type=Path, required=True, help="append-only audit ledger")
    system1_sensor.add_argument("--produced-by", default="integrity-system1-harness-producer")
    system1_sensor.add_argument("--emitted-at", help="deterministic RFC 3339 emission time (default: now)")
    system1_sensor.add_argument("--json", action="store_true", dest="as_json", help="emit the emission record as JSON")

    system1_interchange = subparsers.add_parser(
        "system1-interchange",
        help="sign a runtime-observed interchange export as a canonical System 1 trajectory",
        description=(
            "Project an agent-telemetry interchange export into a minimized harness window "
            "and emit it through the System 1 sensor. Only runtime_observer and "
            "enforcement_boundary events are accepted; agent_assertion events are refused. "
            "Completeness requires a complete run status and an explicit --heartbeat-at. "
            "This is not live telemetry or proof of external event completeness."
        ),
    )
    system1_interchange.add_argument("export", type=Path, help="agent-telemetry interchange export JSON (synthetic)")
    system1_interchange.add_argument("contract", type=Path, help="contract whose vocabulary constrains observations")
    system1_interchange.add_argument("--produced-at", required=True, help="RFC 3339 time the window was produced")
    system1_interchange.add_argument("--heartbeat-at", help="RFC 3339 source heartbeat proving coverage; required for completeness")
    system1_interchange.add_argument("--candidate-version", required=True, help="candidate semantic version")
    system1_interchange.add_argument("--producer-private-key", type=Path, required=True, help="producer Ed25519 private key PEM")
    system1_interchange.add_argument("--producer-public-key", type=Path, required=True, help="producer Ed25519 public key PEM")
    system1_interchange.add_argument("--trust-key", type=Path, required=True, help="configured producer trust public key PEM")
    system1_interchange.add_argument("--output", type=Path, required=True, help="new signed trajectory envelope JSON path")
    system1_interchange.add_argument("--ledger", type=Path, required=True, help="append-only audit ledger")
    system1_interchange.add_argument("--produced-by", default="integrity-system1-harness-producer")
    system1_interchange.add_argument("--emitted-at", help="deterministic RFC 3339 emission time (default: now)")
    system1_interchange.add_argument("--json", action="store_true", dest="as_json", help="emit the emission record as JSON")

    drift_baseline = subparsers.add_parser(
        "drift-baseline",
        help="build a signed, versioned behavioural baseline (System 1 OP-2)",
        description=(
            "Build the System 1 baseline (OP-2) from a complete learning window of "
            "cycle-*.json records emitted by monitor-loop. The window must be ordered, "
            "contiguous and single-contract; it is anchored by a sha256 digest and signed "
            "with the baseline authority's Ed25519 key. Re-baselining over an existing "
            "baseline requires --approved-by (threat T-04)."
        ),
    )
    drift_baseline.add_argument(
        "window_dir", type=Path, help="directory with the learning window cycle-*.json records"
    )
    drift_baseline.add_argument(
        "--private-key",
        type=Path,
        required=True,
        help="baseline authority Ed25519 private key (PEM)",
    )
    drift_baseline.add_argument(
        "--public-key",
        type=Path,
        required=True,
        help="baseline authority Ed25519 public key (PEM)",
    )
    drift_baseline.add_argument(
        "--previous", type=Path, help="existing signed baseline being superseded (re-baseline)"
    )
    drift_baseline.add_argument(
        "--approved-by",
        help="named human approval recorded in the re-baseline (required with --previous)",
    )
    drift_baseline.add_argument(
        "--produced-by", default="integrity-drift-baseline", help="label recorded in the baseline"
    )
    drift_baseline.add_argument(
        "--output", type=Path, help="write the signed baseline to this path"
    )
    drift_baseline.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the signed baseline as JSON"
    )

    drift_check = subparsers.add_parser(
        "drift-check",
        help="compare a window against the baseline and record a drift signal (System 1 OP-3)",
        description=(
            "Compare a window of cycle-*.json records against the verified baseline "
            "(System 1 OP-3). The detector never compares against an unverified profile: "
            "unsigned, foreign-key or superseded baselines are refused. Drift signals and "
            "indeterminate declarations are appended to the ledger; a drift signal never "
            "changes autonomy."
        ),
    )
    drift_check.add_argument(
        "baseline", type=Path, help="signed baseline envelope (drift-baseline output)"
    )
    drift_check.add_argument(
        "window_dir", type=Path, help="directory with the window cycle-*.json records"
    )
    drift_check.add_argument(
        "--trust-key",
        type=Path,
        required=True,
        help="baseline authority public key the detector verifies against",
    )
    drift_check.add_argument(
        "--latest-baseline-id",
        help="refuse the baseline when it is not this id (superseded reuse guard)",
    )
    drift_check.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help=(
            "append-only ledger receiving drift_signal events and declared refusals "
            "(mandatory: a refusal that could not be recorded would be silent)"
        ),
    )
    drift_check.add_argument(
        "--max-status-shift",
        type=float,
        default=0.34,
        help="declared tolerance: max total-variation status shift",
    )
    drift_check.add_argument(
        "--max-autonomy-shift",
        type=float,
        default=0.34,
        help="declared tolerance: max total-variation autonomy shift",
    )
    drift_check.add_argument(
        "--max-change-shift",
        type=float,
        default=0.34,
        help="declared tolerance: max total-variation change-class shift",
    )
    drift_check.add_argument(
        "--ignore-new-categories",
        action="store_true",
        help="declared tolerance: do not treat unseen categories as drift",
    )
    drift_check.add_argument(
        "--produced-by", default="integrity-drift-detector", help="label recorded in the signal"
    )
    drift_check.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the drift signal as JSON"
    )

    drift_watch = subparsers.add_parser(
        "drift-watch",
        help="compare the cycle history against a signed baseline and record a drift signal (System 1 OP-2/OP-3)",
        description=(
            "Chain the monitoring loop's cycle history to the drift stack: anchor a signed "
            "baseline on the first learning window (once) and compare a bounded trailing "
            "window on every tick. A window already compared against the same baseline is "
            "not compared again, so a 5-minute scheduler records one signal per change, not "
            "one per tick. The signal never changes autonomy."
        ),
    )
    drift_watch.add_argument(
        "history_dir",
        type=Path,
        help="monitoring-loop output directory holding cycle-history.jsonl",
    )
    drift_watch.add_argument(
        "--authority-private-key",
        type=Path,
        required=True,
        help="baseline authority Ed25519 private key (PEM)",
    )
    drift_watch.add_argument(
        "--authority-public-key",
        type=Path,
        required=True,
        help="baseline authority Ed25519 public key (PEM)",
    )
    drift_watch.add_argument(
        "--trust-key",
        type=Path,
        help="public key the detector verifies the baseline against (default: --authority-public-key)",
    )
    drift_watch.add_argument(
        "--baseline",
        type=Path,
        help=(
            "existing signed baseline (default: <history-dir>/drift-baseline.json, anchored "
            "on the first learning window)"
        ),
    )
    drift_watch.add_argument(
        "--learning-cycles",
        type=int,
        default=3,
        help="cycles used to anchor the baseline (default: 3)",
    )
    drift_watch.add_argument(
        "--window-cycles",
        type=int,
        default=3,
        help="trailing cycles compared on this tick (default: 3)",
    )
    drift_watch.add_argument(
        "--latest-baseline-id",
        help="refuse the baseline when it is not this id (superseded reuse guard)",
    )
    drift_watch.add_argument(
        "--produced-by", default="integrity-drift-watch", help="label recorded in the signal"
    )
    drift_watch.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="append-only ledger receiving drift_signal and drift_refusal records",
    )
    drift_watch.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the drift-watch report as JSON"
    )

    drift_respond = subparsers.add_parser(
        "drift-respond",
        help="trigger the deterministic re-evaluation from a recorded drift signal (System 1)",
        description=(
            "Wire a recorded drift signal to the deterministic engine: the run signs and "
            "verifies the window's trajectory, evaluates the contract and re-emits the "
            "guard's signed snapshot with a fresh TTL. Only a signal present in the ledger "
            "with the same payload triggers a run, and each signal triggers at most one run; "
            "a signal over a window an existing response already answered folds into that "
            "response (P-07 coalescence). Recorded deep runs per contract are capped per "
            "sliding budget window, and a signal the budget cannot afford is refused "
            "fail-closed with a declared record. The signal never changes autonomy: the "
            "engine's recommendation is reported, and the signed snapshot remains the only "
            "authority."
        ),
    )
    drift_respond.add_argument(
        "signal", type=Path, help="drift signal JSON (drift-check --json output)"
    )
    drift_respond.add_argument(
        "trajectory", type=Path, help="signed trajectory source of the window"
    )
    drift_respond.add_argument(
        "contract", type=Path, help="contract to re-evaluate (must match the signal)"
    )
    drift_respond.add_argument(
        "--producer-private-key",
        type=Path,
        required=True,
        help="producer Ed25519 private key (PEM)",
    )
    drift_respond.add_argument(
        "--producer-public-key",
        type=Path,
        required=True,
        help="producer Ed25519 public key (PEM)",
    )
    drift_respond.add_argument(
        "--trust-key",
        type=Path,
        required=True,
        help="public key the run verifies against before emitting anything",
    )
    drift_respond.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory receiving the fresh envelope, evaluation and guard snapshot",
    )
    drift_respond.add_argument(
        "--sensor-trust-key",
        type=Path,
        help=(
            "sensor key the window's signed A2 envelope is verified against; required when "
            "the trajectory source is an A2 envelope, as the drift watch records"
        ),
    )
    drift_respond.add_argument(
        "--previous-cycle", type=Path, help="cycle record the signal window ended with"
    )
    drift_respond.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="append-only ledger holding the signal (and receiving the response)",
    )
    drift_respond.add_argument(
        "--produced-by", default="integrity-drift-response", help="label recorded in the response"
    )
    drift_respond.add_argument(
        "--budget-max-runs",
        type=int,
        default=None,
        help=(
            "declared maximum of recorded deep runs per sliding budget window for the "
            "contract (default: 3)"
        ),
    )
    drift_respond.add_argument(
        "--budget-window-seconds",
        type=float,
        default=None,
        help=(
            "declared length of the sliding budget window in seconds (default: 2592000 = "
            "30 days)"
        ),
    )
    drift_respond.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the response record as JSON"
    )

    sensor_health = subparsers.add_parser(
        "sensor-health",
        help="print one line of sensor health: snapshot freshness, pending signals, recovery gaps",
        description=(
            "Read-only health of the scheduled sensor: the guard snapshot's age and "
            "remaining TTL, drift signals no respond-stage record has answered yet, and "
            "recovery gaps (signals a refusal answered instead of a run). Appends nothing "
            "and decides nothing; an absent ledger is refused, never created. "
            "Exit 0 = ok, 1 = degraded, 2 = critical or unreadable."
        ),
    )
    sensor_health.add_argument(
        "output_dir", type=Path, help="monitoring-loop output directory"
    )
    sensor_health.add_argument(
        "--ledger",
        type=Path,
        required=True,
        help="append-only ledger holding the signals and their responses",
    )
    sensor_health.add_argument(
        "--trust-key",
        type=Path,
        help=(
            "key the snapshot signature is anchored to, as the guard does (default: "
            "self-consistency and freshness only, reported as unverified)"
        ),
    )
    sensor_health.add_argument(
        "--checked-at",
        help="deterministic RFC 3339 time the report is taken at (default: now)",
    )
    sensor_health.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the health report as JSON"
    )

    incidents = subparsers.add_parser(
        "incidents",
        help="detect failed clauses and optionally register incidents",
    )
    incidents.add_argument("contract", type=Path, help="path to a YAML contract")
    incidents.add_argument("trajectory", type=Path, help="path to a YAML trajectory")
    incidents.add_argument("--db", type=Path, help="SQLite registry path")
    incidents.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a machine-readable result",
    )

    diagnose = subparsers.add_parser(
        "diagnose",
        help="generate untested hypotheses and non-executable change cards",
    )
    diagnose.add_argument("contract", type=Path, help="path to a YAML contract")
    diagnose.add_argument("trajectory", type=Path, help="path to a YAML trajectory")
    diagnose.add_argument(
        "--output-dir",
        type=Path,
        help="write one JSON and Markdown file per change card",
    )
    diagnose.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the complete machine-readable change-card batch",
    )

    sandbox = subparsers.add_parser(
        "sandbox",
        help="verify a human-approved candidate without applying or releasing it",
    )
    sandbox.add_argument("contract", type=Path, help="path to a YAML contract")
    sandbox.add_argument("card", type=Path, help="path to a generated JSON change card")
    sandbox.add_argument(
        "trajectories",
        type=Path,
        nargs="+",
        help="one or more candidate test trajectories",
    )
    sandbox.add_argument("--candidate", required=True, help="candidate id from the change card")
    sandbox.add_argument("--approved-by", required=True, help="sandbox reviewer actor")
    sandbox.add_argument("--approved-at", required=True, help="RFC 3339 sandbox approval time")
    sandbox.add_argument("--output", type=Path, help="write the JSON sandbox report")
    sandbox.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the complete machine-readable report",
    )

    keygen = subparsers.add_parser(
        "review-keygen",
        help="generate a local Ed25519 review signing key pair",
    )
    keygen.add_argument("private_key", type=Path, help="new private PEM path")
    keygen.add_argument("public_key", type=Path, help="new public PEM path")

    review = subparsers.add_parser(
        "review",
        help="create a signed independent decision for a sandbox report",
    )
    review.add_argument("report", type=Path, help="path to a JSON sandbox report")
    review.add_argument(
        "--decision",
        required=True,
        choices=[item.value for item in ReviewDecision],
    )
    review.add_argument("--reviewer", required=True, help="independent reviewer actor")
    review.add_argument("--role", required=True, help="reviewer role")
    review.add_argument("--rationale", required=True, help="auditable decision rationale")
    review.add_argument("--reference", required=True, help="ticket or review reference")
    review.add_argument("--decided-at", required=True, help="RFC 3339 decision time")
    review.add_argument("--private-key", required=True, type=Path, help="Ed25519 private PEM")
    review.add_argument("--output", required=True, type=Path, help="signed review JSON path")
    review.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")

    verify_review = subparsers.add_parser(
        "verify-review",
        help="verify a signed release-review decision",
    )
    verify_review.add_argument("review", type=Path, help="signed review JSON path")
    verify_review.add_argument("--public-key", type=Path, help="public PEM (single-signature)")
    verify_review.add_argument(
        "--public-keys",
        action="append",
        default=[],
        metavar="actor=path",
        help=(
            "chain-mode mapping: one or more ``actor=path`` pairs for B2 "
            "hierarchical chains. Repeated for additional actors. Example: "
            "--public-keys head_of_ai_risk=head.pem --public-keys "
            "deputy_ai_governance=deputy.pem --public-keys "
            "claims_deputy_x=delegate.pem"
        ),
    )
    verify_review.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit machine-readable verification",
    )

    audit_record = subparsers.add_parser("audit-record", help="append an artifact to the audit ledger")
    audit_record.add_argument("ledger", type=Path, help="SQLite audit ledger")
    audit_record.add_argument("artifact", type=Path, help="change card, sandbox, or review JSON")
    audit_record.add_argument("--public-key", type=Path, help="required for signed reviews")

    audit_verify = subparsers.add_parser("audit-verify", help="verify the complete audit ledger")
    audit_verify.add_argument("ledger", type=Path, help="SQLite audit ledger")
    audit_verify.add_argument("--json", action="store_true", dest="as_json")

    audit_list = subparsers.add_parser("audit-list", help="list audit events")
    audit_list.add_argument("ledger", type=Path, help="SQLite audit ledger")
    audit_list.add_argument("--json", action="store_true", dest="as_json")

    audit_export = subparsers.add_parser(
        "audit-export",
        help=(
            "export a verified ledger as a portable bundle that a third party can "
            "re-verify offline (offline verifier page included in the site)"
        ),
    )
    audit_export.add_argument("ledger", type=Path, help="SQLite audit ledger")
    audit_export.add_argument(
        "--output", type=Path, help="write the bundle JSON to this path"
    )
    audit_export.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print the bundle JSON (also used together with --output)",
    )

    revoke_key = subparsers.add_parser("audit-revoke-key", help="append a signing-key revocation")
    revoke_key.add_argument("ledger", type=Path)
    revoke_key.add_argument("key_id")
    revoke_key.add_argument("--actor", required=True)
    revoke_key.add_argument("--reason", required=True)
    revoke_key.add_argument("--reference", required=True)
    revoke_key.add_argument("--revoked-at", required=True)

    revoke_review = subparsers.add_parser("audit-revoke-review", help="append a review revocation")
    revoke_review.add_argument("ledger", type=Path)
    revoke_review.add_argument("review_id")
    revoke_review.add_argument("--actor", required=True)
    revoke_review.add_argument("--reason", required=True)
    revoke_review.add_argument("--reference", required=True)
    revoke_review.add_argument("--revoked-at", required=True)

    agent_evaluate = subparsers.add_parser(
        "agent-evaluate",
        help="run the executable insurance agent and evaluate its telemetry",
    )
    agent_evaluate.add_argument("contract", type=Path)
    agent_evaluate.add_argument("adapter", type=Path)
    agent_evaluate.add_argument("agent", type=Path)
    agent_evaluate.add_argument("scenario", type=Path)
    agent_evaluate.add_argument("--output-dir", required=True, type=Path)
    agent_evaluate.add_argument("--json", action="store_true", dest="as_json")

    agent_sandbox_run = subparsers.add_parser(
        "agent-sandbox-run",
        help="run a domain-agnostic declarative agent sandbox and evaluate its telemetry",
    )
    agent_sandbox_run.add_argument("contract", type=Path)
    agent_sandbox_run.add_argument("adapter", type=Path)
    agent_sandbox_run.add_argument("sandbox_config", type=Path)
    agent_sandbox_run.add_argument("scenario", type=Path)
    agent_sandbox_run.add_argument("--output-dir", required=True, type=Path)
    agent_sandbox_run.add_argument("--json", action="store_true", dest="as_json")

    cohort_evaluate = subparsers.add_parser(
        "agent-cohort-evaluate",
        help="run a cross-product of executable agents and scenarios",
    )
    cohort_evaluate.add_argument("contract", type=Path)
    cohort_evaluate.add_argument("adapter", type=Path)
    cohort_evaluate.add_argument("cohort", type=Path)
    cohort_evaluate.add_argument("--output-dir", required=True, type=Path)
    cohort_evaluate.add_argument("--json", action="store_true", dest="as_json")

    clause_coverage = subparsers.add_parser(
        "clause-coverage",
        help="report which contract clauses have executed and been seen to fail",
    )
    clause_coverage.add_argument(
        "paths",
        type=Path,
        nargs="*",
        help="evaluation.json file(s) or directories to search recursively",
    )
    clause_coverage.add_argument(
        "--audit-csv",
        type=Path,
        help="build the ledger from a recorded audit snapshot instead of artifacts",
    )
    clause_coverage.add_argument(
        "--contract",
        type=Path,
        action="append",
        default=[],
        help="declare the expected clause corpus; repeatable",
    )
    clause_coverage.add_argument(
        "--require-all-triggered",
        action="store_true",
        help="fail when a declared clause was never executed",
    )
    clause_coverage.add_argument(
        "--require-all-failed",
        action="store_true",
        help="fail when a clause has never been observed to fail",
    )
    clause_coverage.add_argument(
        "--cards-dir",
        type=Path,
        help="write one Markdown detector card per clause into this directory",
    )
    clause_coverage.add_argument(
        "--cards-md",
        type=Path,
        help="write a single document holding a summary table and every card",
    )
    clause_coverage.add_argument("--json", action="store_true", dest="as_json")

    swarm_evaluate = subparsers.add_parser(
        "swarm-evaluate",
        help="evaluate a pair of agents over a mediated swarm channel",
    )
    swarm_evaluate.add_argument("contract", type=Path)
    swarm_evaluate.add_argument("adapter", type=Path)
    swarm_evaluate.add_argument("swarm", type=Path)
    swarm_evaluate.add_argument("--output-dir", required=True, type=Path)
    swarm_evaluate.add_argument(
        "--swarm-contract", type=Path, help="optional swarm contract v0.2 for a system verdict"
    )
    swarm_evaluate.add_argument(
        "--private-key", type=Path, help="optional Ed25519 key; signs report and evidence"
    )
    swarm_evaluate.add_argument(
        "--public-key", type=Path, help="public key paired with --private-key"
    )
    swarm_evaluate.add_argument("--json", action="store_true", dest="as_json")

    swarm_keygen = subparsers.add_parser(
        "swarm-keygen", help="generate Ed25519 keys for signing swarm reports"
    )
    swarm_keygen.add_argument("private_key", type=Path)
    swarm_keygen.add_argument("public_key", type=Path)

    swarm_verify = subparsers.add_parser(
        "swarm-verify", help="verify a signed swarm report and its channel/evaluation evidence"
    )
    swarm_verify.add_argument("report", type=Path)
    swarm_verify.add_argument("channel", type=Path)
    swarm_verify.add_argument("--public-key", required=True, type=Path)
    swarm_verify.add_argument(
        "--evidence-dir",
        required=True,
        type=Path,
        help="directory containing subject/ and peer/ evaluation.json",
    )
    swarm_verify.add_argument(
        "--swarm-contract", type=Path, help="optional contract used to check contract digest/ID"
    )
    swarm_verify.add_argument("--json", action="store_true", dest="as_json")

    social_experiment = subparsers.add_parser(
        "social-experiment",
        help=(
            "run the N-agent social experiments (bystander, Asch, cascade)"
            " over the mediated channel"
        ),
    )
    social_experiment.add_argument(
        "kind",
        nargs="?",
        choices=("bystander", "asch", "cascade"),
        help="which experiment to run; optional when --verify is used",
    )
    social_experiment.add_argument(
        "--trials", type=int, default=200, help="runs per size (default 200)"
    )
    social_experiment.add_argument(
        "--sizes",
        type=_parse_sizes,
        default=None,
        help="comma-separated group/majority sizes (default 1,2,3,4,5)",
    )
    social_experiment.add_argument(
        "--seed", help="deterministic seed (default: the experiment's own)"
    )
    social_experiment.add_argument(
        "--case",
        choices=("requires_review", "benign", "both"),
        default=None,
        help=(
            "cascade only: whether the case needs the escalation, or is the"
            " benign mirror (default requires_review)"
        ),
    )
    social_experiment.add_argument(
        "--variant",
        choices=(
            "both",
            "diffusion",
            "pluralistic_ignorance",
            "unanime",
            "con_disidente",
            "senal_ambigua",
            "senal_clara",
        ),
        default="both",
        help="which variant(s) to run; validated against the kind",
    )
    social_experiment.add_argument(
        "--output-dir", type=Path, help="write the sweeps and the signed samples here"
    )
    social_experiment.add_argument(
        "--private-key", type=Path, help="Ed25519 private key (PEM) used to sign the samples"
    )
    social_experiment.add_argument(
        "--public-key", type=Path, help="public key paired with --private-key"
    )
    social_experiment.add_argument(
        "--signed-at", help="RFC 3339 timestamp, for reproducible signatures"
    )
    social_experiment.add_argument(
        "--verify",
        type=Path,
        metavar="DIR",
        help="verify every signed report under DIR instead of running",
    )
    social_experiment.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the sweeps as a single JSON array",
    )

    boundary_run = subparsers.add_parser(
        "boundary-run",
        help="run a capability-only agent in the supervised process boundary",
    )
    boundary_run.add_argument("contract", type=Path)
    boundary_run.add_argument("plan", type=Path)
    boundary_run.add_argument("--private-key", required=True, type=Path)
    boundary_run.add_argument(
        "--payment-authorization-public-key",
        type=Path,
        help="trusted Ed25519 key for payment receipts; payments fail closed when absent",
    )
    boundary_run.add_argument("--output-dir", required=True, type=Path)
    boundary_run.add_argument("--timeout-seconds", type=float, default=10.0)
    boundary_run.add_argument(
        "--backend",
        choices=[item.value for item in BoundaryBackend],
        default=BoundaryBackend.python_process.value,
    )
    boundary_run.add_argument(
        "--docker-image",
        help="digest-pinned image, required for --backend docker",
    )
    boundary_run.add_argument("--docker-memory-mb", type=int, default=256)
    boundary_run.add_argument("--docker-cpus", type=float, default=0.5)
    boundary_run.add_argument("--docker-pids-limit", type=int, default=64)
    boundary_run.add_argument("--docker-tmpfs-mb", type=int, default=16)
    boundary_run.add_argument("--json", action="store_true", dest="as_json")

    boundary_verify = subparsers.add_parser(
        "boundary-verify",
        help="verify a boundary report event chain and Ed25519 signature",
    )
    boundary_verify.add_argument("report", type=Path)
    boundary_verify.add_argument("--public-key", required=True, type=Path)
    boundary_verify.add_argument("--json", action="store_true", dest="as_json")

    payment_authorize = subparsers.add_parser(
        "payment-authorize",
        help=(
            "sign a human authorization bound to one exact payment operation; "
            "the execution boundary executes a payment only against a receipt "
            "like this, verified with --payment-authorization-public-key"
        ),
    )
    payment_authorize.add_argument(
        "--private-key",
        required=True,
        type=Path,
        help="Ed25519 private key PEM of the approval authority",
    )
    payment_authorize.add_argument(
        "--authorization-id",
        required=True,
        dest="authorization_id",
        help="unique id of this approval (replay key; one execution each)",
    )
    payment_authorize.add_argument(
        "--execution-id",
        required=True,
        dest="execution_id",
        help="execution_id of the boundary plan the payment belongs to",
    )
    payment_authorize.add_argument(
        "--contract-id",
        required=True,
        dest="contract_id",
        help="contract_id the payment is authorized against",
    )
    payment_authorize.add_argument(
        "--candidate-version",
        required=True,
        dest="candidate_version",
        help="candidate_version the payment is authorized against",
    )
    payment_authorize.add_argument(
        "--operation-id",
        required=True,
        dest="operation_id",
        help="unique id of the exact payment operation",
    )
    payment_authorize.add_argument(
        "--amount-eur",
        required=True,
        dest="amount_eur",
        help="authorized amount in EUR, e.g. 12.34",
    )
    payment_authorize.add_argument(
        "--beneficiary-id",
        required=True,
        dest="beneficiary_id",
        help="beneficiary the amount may be sent to",
    )
    payment_authorize.add_argument(
        "--approved-by",
        required=True,
        dest="approved_by",
        help="identity of the human approver",
    )
    payment_authorize.add_argument(
        "--ttl-seconds",
        type=float,
        default=300.0,
        dest="ttl_seconds",
        help="how long the receipt stays valid (default: 300)",
    )
    payment_authorize.add_argument(
        "--output", type=Path, help="write the receipt here (default: stdout)"
    )
    payment_authorize.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON"
    )

    enforce = subparsers.add_parser(
        "enforce-envelope",
        help=(
            "evaluate a standing_approval_window envelope for a release "
            "intent and idempotently record release_denied audit events"
        ),
    )
    enforce.add_argument(
        "--ledger", required=True, type=Path, help="SQLite audit ledger path"
    )
    enforce.add_argument(
        "--contract",
        required=True,
        type=Path,
        help="v0.1.1 contract path (with standing_approval_window)",
    )
    enforce.add_argument(
        "--target-clause-id",
        required=True,
        help="clause id the release intent targets",
    )
    enforce.add_argument(
        "--current-envelope-usage",
        required=True,
        type=int,
        help="current envelope usage count (>=0)",
    )
    enforce.add_argument(
        "--now",
        required=True,
        help="RFC 3339 UTC enforcement-evaluation timestamp",
    )
    enforce.add_argument(
        "--review",
        type=Path,
        help="optional signed release review JSON (carries optional delegation_chain)",
    )
    enforce.add_argument(
        "--review-id",
        required=True,
        help="id uniquely identifying the release intent for ledger de-duplication",
    )
    enforce.add_argument(
        "--reference",
        required=True,
        help="ticket or human review reference (3-255 chars)",
    )
    enforce.add_argument(
        "--output",
        type=Path,
        help="write the JSON result to this path in addition to stdout",
    )
    enforce.add_argument(
        "--public-keys",
        action="append",
        default=[],
        metavar="actor=path",
        help=(
            "chain-mode mapping: one or more ``actor=path`` pairs for B2 "
            "hierarchical chains. Required when ``--review`` carries a "
            "``delegation_chain``; the orchestrator defensively calls "
            "verify_release_review so a forged chain cannot bypass "
            "auto_veto. Repeated for additional actors."
        ),
    )
    enforce.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the complete machine-readable result",
    )

    guard = subparsers.add_parser(
        "guard",
        help=(
            "screen tool calls against the enforcement posture derived from "
            "an evaluation (JSON or trajectory YAML) and a client tool policy"
        ),
    )
    guard.add_argument(
        "evaluation",
        type=Path,
        help="evaluation JSON or trajectory YAML produced by Integrity",
    )
    guard.add_argument(
        "tool",
        nargs="*",
        default=[],
        help="tool names to screen; omit when --policy is provided to screen it",
    )
    guard.add_argument(
        "--policy",
        required=True,
        type=Path,
        help="client tool policy YAML declaring sensitive_tools/allowlisted_tools",
    )
    guard.add_argument(
        "--sandbox-evidence",
        action="store_true",
        help=(
            "record that sandbox recovery evidence was declared for this "
            "evaluation; stateful autonomy transitions remain client-side "
            "(EnforcementAdapter.update)"
        ),
    )
    guard.add_argument(
        "--record-ledger",
        type=Path,
        help="verify-and-append a signed guard-decision receipt to this audit ledger",
    )
    guard.add_argument(
        "--private-key",
        type=Path,
        help="Ed25519 private key PEM for signing the guard decision (required with --record-ledger)",
    )
    guard.add_argument(
        "--public-key",
        type=Path,
        help="matching Ed25519 public key PEM (required with --record-ledger)",
    )
    guard.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a machine-readable result",
    )

    guard_server = subparsers.add_parser(
        "guard-server",
        help=(
            "run the MCP-style pre-tool-call guard server (line-delimited "
            "JSON-RPC over stdio) screening tool calls inline"
        ),
    )
    guard_server.add_argument(
        "evaluation",
        type=Path,
        help="evaluation JSON carrying recommended_autonomy + block_release",
    )
    guard_server.add_argument(
        "--policy",
        required=True,
        type=Path,
        help="client tool policy YAML declaring sensitive_tools/allowlisted_tools",
    )
    guard_server.add_argument(
        "--record-ledger",
        type=Path,
        help="sign and anchor every screened call to this audit ledger",
    )
    guard_server.add_argument(
        "--private-key",
        type=Path,
        help="Ed25519 private key PEM (required with --record-ledger)",
    )
    guard_server.add_argument(
        "--public-key",
        type=Path,
        help="matching Ed25519 public key PEM (required with --record-ledger)",
    )
    guard_server.add_argument(
        "--recovery-public-key",
        type=Path,
        help="Ed25519 trust key for signed recovery evidence (--recovery-evidence required too)",
    )
    guard_server.add_argument(
        "--recovery-evidence",
        type=Path,
        help="signed recovery-evidence envelope (guard-sign-recovery output)",
    )
    guard_sign = subparsers.add_parser(
        "guard-sign-evaluation",
        help=(
            "wrap an evaluation JSON in a signed, time-bounded snapshot the "
            "guard-server can authenticate with --evaluation-public-key"
        ),
    )
    guard_sign.add_argument("evaluation", type=Path, help="evaluation JSON/YAML to wrap")
    guard_sign.add_argument(
        "--private-key", required=True, type=Path, help="Ed25519 private key PEM"
    )
    guard_sign.add_argument(
        "--public-key", required=True, type=Path, help="matching Ed25519 public key PEM"
    )
    guard_sign.add_argument(
        "--ttl-seconds",
        type=float,
        default=900.0,
        help="how long the snapshot may steer the guard (default: 900)",
    )
    recovery_sign = subparsers.add_parser(
        "guard-sign-recovery",
        help=(
            "wrap a recovery claim in a signed envelope the guard-server "
            "verifies with --recovery-public-key to authorize relaxation "
            "(never above SUPERVISE)"
        ),
    )
    recovery_sign.add_argument(
        "target_snapshot_id",
        help="snapshot_id of the evaluation snapshot the recovery authorizes",
    )
    recovery_sign.add_argument(
        "target_autonomy",
        help="autonomy level the recovery authorizes (never above SUPERVISE)",
    )
    recovery_sign.add_argument(
        "recovery_run_id",
        help="identifier of the recovery run that was executed and passed",
    )
    recovery_sign.add_argument(
        "--private-key", required=True, type=Path, help="Ed25519 private key PEM"
    )
    recovery_sign.add_argument(
        "--public-key", required=True, type=Path, help="matching Ed25519 public key PEM"
    )
    recovery_sign.add_argument(
        "--ttl-seconds",
        type=float,
        default=3600.0,
        help="how long the evidence may authorize relaxation (default: 3600)",
    )
    recovery_sign.add_argument(
        "--output", type=Path, help="write the signed evidence here (default: stdout)"
    )
    recovery_sign.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON"
    )
    guard_sign.add_argument(
        "--output", type=Path, help="write the signed snapshot here (default: stdout)"
    )
    guard_sign.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON"
    )

    guard_server.add_argument(
        "--evaluation-public-key",
        type=Path,
        help=(
            "Ed25519 public key PEM the evaluation snapshot must be signed with; "
            "enables signed+time-bounded evaluation input (fail-closed on "
            "unsigned, altered, expired or foreign-signed snapshots)"
        ),
    )
    guard_server.add_argument(
        "--bootstrap-autonomy-from-evaluation",
        action="store_true",
        help=(
            "explicitly initialize an empty/missing durable autonomy ledger from the "
            "evaluation snapshot (never use to reset an existing ledger)"
        ),
    )

    readiness = subparsers.add_parser(
        "ai-act-readiness",
        help="build a longitudinal AI Act technical-evidence report",
    )
    readiness.add_argument("profile", type=Path, help="versioned AI system profile YAML")
    readiness.add_argument("obligations", type=Path, help="AI Act readiness map YAML")
    readiness.add_argument(
        "evaluations",
        type=Path,
        nargs="+",
        help="evaluation JSON files or directories to scan recursively",
    )
    readiness.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="write JSON, Markdown, CSV, and dashboard outputs here",
    )
    readiness.add_argument("--json", action="store_true", dest="as_json")

    readiness_ingest = subparsers.add_parser(
        "ai-act-ingest",
        help="incrementally store AI Act profile, map, and evaluation evidence",
    )
    readiness_ingest.add_argument("profile", type=Path, help="versioned system profile YAML")
    readiness_ingest.add_argument("obligations", type=Path, help="readiness map YAML")
    readiness_ingest.add_argument(
        "evaluations",
        type=Path,
        nargs="+",
        help="new evaluation JSON files or directories to scan recursively",
    )
    readiness_ingest.add_argument("--db", required=True, type=Path)
    readiness_ingest.add_argument("--json", action="store_true", dest="as_json")

    readiness_report = subparsers.add_parser(
        "ai-act-report",
        help="generate a readiness evidence pack from the longitudinal store",
    )
    readiness_report.add_argument("--db", required=True, type=Path)
    readiness_report.add_argument("--system-id", required=True)
    readiness_report.add_argument("--output-dir", required=True, type=Path)
    readiness_report.add_argument("--json", action="store_true", dest="as_json")

    readiness_systems = subparsers.add_parser(
        "ai-act-systems",
        help="list systems in the longitudinal readiness store",
    )
    readiness_systems.add_argument("--db", required=True, type=Path)
    readiness_systems.add_argument("--json", action="store_true", dest="as_json")

    cycle_keygen = subparsers.add_parser(
        "ai-act-cycle-keygen",
        help="generate a dedicated Ed25519 key pair for monitor-cycle receipts",
    )
    cycle_keygen.add_argument("private_key", type=Path)
    cycle_keygen.add_argument("public_key", type=Path)

    readiness_cycle = subparsers.add_parser(
        "ai-act-cycle",
        help="run one schedulable ingest, compare, alert, report, and audit cycle",
    )
    readiness_cycle.add_argument("profile", type=Path)
    readiness_cycle.add_argument("obligations", type=Path)
    readiness_cycle.add_argument("evaluations", type=Path, nargs="+")
    readiness_cycle.add_argument("--db", required=True, type=Path)
    readiness_cycle.add_argument("--output-dir", required=True, type=Path)
    readiness_cycle.add_argument("--audit-ledger", required=True, type=Path)
    readiness_cycle.add_argument("--private-key", required=True, type=Path)
    readiness_cycle.add_argument("--public-key", required=True, type=Path)
    readiness_cycle.add_argument(
        "--ran-at",
        help="fixed RFC 3339 cycle time for deterministic retries",
    )
    readiness_cycle.add_argument("--json", action="store_true", dest="as_json")

    cycle_verify = subparsers.add_parser(
        "ai-act-cycle-verify",
        help="verify a signed monitor-cycle receipt",
    )
    cycle_verify.add_argument("receipt", type=Path)
    cycle_verify.add_argument("--json", action="store_true", dest="as_json")

    lead_discovery = subparsers.add_parser(
        "discover-leads",
        help="qualify search results or directory exports into CRM-ready leads",
    )
    lead_discovery.add_argument("input", type=Path, help="CSV or JSON search-result export")
    lead_discovery.add_argument("--output", type=Path, help="write qualified leads as CRM CSV")
    lead_discovery.add_argument(
        "--min-score",
        type=int,
        default=7,
        help="minimum 0-12 score required for CRM export (default: 7)",
    )
    lead_discovery.add_argument("--json", action="store_true", dest="as_json")

    lead_radar = subparsers.add_parser(
        "lead-radar",
        help="crawl configured public sources and qualify possible leads",
    )
    lead_radar.add_argument("config", type=Path, help="YAML source configuration")
    lead_radar.add_argument("--raw-output", type=Path, help="write extracted raw results as CSV")
    lead_radar.add_argument("--crm-output", type=Path, help="write qualified leads as CRM CSV")
    lead_radar.add_argument(
        "--min-score",
        type=int,
        help="override config minimum 0-12 score required for CRM output",
    )
    lead_radar.add_argument("--json", action="store_true", dest="as_json")

    telemetry_dry_run = subparsers.add_parser(
        "telemetry-dry-run",
        help=(
            "dry-run validate a synthetic StrategyAI telemetry sample and report "
            "which signals and clauses are evaluable or indeterminate"
        ),
    )
    telemetry_dry_run.add_argument("input", type=Path, help="local synthetic export JSON")
    telemetry_dry_run_group = telemetry_dry_run.add_mutually_exclusive_group(required=True)
    telemetry_dry_run_group.add_argument(
        "--template", type=Path, help="declarative connector template YAML"
    )
    telemetry_dry_run_group.add_argument(
        "--connector", help="registered connector ID (e.g. strategyai-reference-v0.1)"
    )
    telemetry_dry_run.add_argument(
        "--contract", type=Path, help="optional contract YAML to check clause evaluability"
    )
    telemetry_dry_run.add_argument(
        "--adapter", type=Path, help="optional adapter YAML paired with --contract"
    )
    telemetry_dry_run.add_argument(
        "--candidate-version",
        default="0.1.0",
        help="candidate SemVer alias (default: 0.1.0)",
    )
    telemetry_dry_run.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )
    return parser


def _validate(path: Path, *, as_json: bool, normalized: bool) -> int:
    try:
        document = load_contract(path)
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "path": str(path),
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"INVALID: {path}", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    result = {
        "valid": True,
        "path": str(path),
        "contract_id": document.contract.id,
        "contract_version": document.contract.version,
        "clause_count": len(document.clauses),
        "clause_types": sorted({clause.type for clause in document.clauses}),
    }
    if normalized:
        result["contract"] = document.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

    if as_json or normalized:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"VALID: {path} "
            f"({result['contract_id']} v{result['contract_version']}; "
            f"{result['clause_count']} clauses)"
        )
    return 0


def _evaluate(
    contract_path: Path,
    trajectory_path: Path,
    *,
    as_json: bool,
    trajectory_trust_key: Path | None = None,
    trajectory_envelope: Path | None = None,
) -> int:
    if (trajectory_trust_key is None) != (trajectory_envelope is None):
        print(
            "EVALUATION INPUT INVALID: --trajectory-trust-key and --trajectory-envelope "
            "must be provided together",
            file=sys.stderr,
        )
        return 2

    envelope = None
    envelope_info = None
    if trajectory_envelope is not None:
        try:
            envelope = load_signed_trajectory(trajectory_envelope, trajectory_trust_key)
            trajectory = load_trajectory(trajectory_path)
        except (DSLValidationError, ValueError) as exc:
            _print_trajectory_rejection(trajectory_path, exc, as_json=as_json)
            return 2
        signed_payload = envelope.payload
        evaluated_payload = trajectory.model_dump(mode="json")
        if _canonical_json(signed_payload) != _canonical_json(evaluated_payload):
            _print_trajectory_rejection(
                trajectory_path,
                ValueError(
                    "the trajectory file does not exactly match the signed payload "
                    "(exact-payload binding failed)"
                ),
                as_json=as_json,
            )
            return 2
        envelope_info = {
            "verified": True,
            "producer": envelope.producer,
            "trajectory_id": envelope.trajectory_id,
            "contract_id": envelope.contract_id,
            "contract_version": envelope.contract_version,
            "signature_key_id": envelope.signature.key_id,
        }
    else:
        try:
            contract = load_contract(contract_path)
            trajectory = load_trajectory(trajectory_path)
        except DSLValidationError as exc:
            if as_json:
                print(
                    json.dumps(
                        {
                            "valid": False,
                            "contract": str(contract_path),
                            "trajectory": str(trajectory_path),
                            "issues": [
                                {"path": issue.path, "message": issue.message}
                                for issue in exc.issues
                            ],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print("EVALUATION INPUT INVALID", file=sys.stderr)
                for issue in exc.issues:
                    print(f"  - {issue}", file=sys.stderr)
            return 2

    try:
        contract = load_contract(contract_path)
        evaluation = evaluate_contract(contract, trajectory)
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "contract": str(contract_path),
                        "trajectory": str(trajectory_path),
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("EVALUATION INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    if as_json:
        document = evaluation.model_dump(mode="json")
        if envelope_info is not None:
            document["trajectory_signature"] = envelope_info
        print(json.dumps(document, indent=2, ensure_ascii=False))
    else:
        print(
            f"{evaluation.status.value.upper()}: {evaluation.trajectory_id} against "
            f"{evaluation.contract_id} v{evaluation.contract_version} "
            f"({evaluation.recommended_autonomy.value}; "
            f"block_release={str(evaluation.block_release).lower()})"
        )
        if envelope_info is not None:
            print(
                "  trajectory envelope verified: "
                f"producer={envelope_info['producer']}; "
                f"key_id={envelope_info['signature_key_id']}"
            )
        for result in evaluation.clause_results:
            print(f"  {result.status.value:16} {result.clause_id}: {result.reason}")
    return 0 if evaluation.status.value == "pass" else 1


def _canonical_json(value: object) -> str:
    """Canonical form used to bind an evaluated trajectory to a signed payload."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _print_trajectory_rejection(
    trajectory_path: Path,
    exc: Exception,
    *,
    as_json: bool,
) -> None:
    """Report a refused signed trajectory (fail-closed, nothing is evaluated)."""

    if as_json:
        print(
            json.dumps(
                {
                    "evaluated": False,
                    "trajectory": str(trajectory_path),
                    "refused": str(exc),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print("TRAJECTORY REFUSED: the signed trajectory is not trusted", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)


def _trajectory_sign(
    trajectory_path: Path,
    private_key: Path,
    public_key: Path,
    *,
    producer: str,
    output: Path | None,
    as_json: bool,
) -> int:
    """Wrap a canonical trajectory in a signed envelope (ledger Ed25519 scheme)."""

    try:
        trajectory = load_trajectory(trajectory_path)
        envelope = sign_trajectory_payload(
            trajectory.model_dump(mode="json"),
            private_key,
            public_key,
            producer=producer,
        )
    except (OSError, ValueError, DSLValidationError) as exc:
        print(f"TRAJECTORY ENVELOPE INVALID: {exc}", file=sys.stderr)
        return 2

    document = json.dumps(envelope.model_dump(mode="json"), indent=2) + "\n"
    if output is not None:
        output.write_text(document, encoding="utf-8")
    if as_json or output is None:
        sys.stdout.write(document)
    else:
        print(
            f"ENVELOPE: trajectory_id={envelope.trajectory_id}; "
            f"contract={envelope.contract_id} v{envelope.contract_version}; "
            f"producer={envelope.producer}"
        )
    return 0


def _system1_sensor(
    build_window: Callable[[], HarnessEventWindow],
    contract_path: Path,
    *,
    candidate_version: str,
    producer_private_key: Path,
    producer_public_key: Path,
    trust_key: Path,
    output: Path,
    ledger_path: Path,
    produced_by: str,
    emitted_at: str | None,
    as_json: bool,
) -> int:
    """Produce A2 evidence and append every refusal to the mandatory ledger."""

    from autopilot.system1_sensor import (
        System1SensorError,
        emit_harness_trajectory,
        record_system1_sensor_refusal,
    )
    try:
        ledger = AuditLedger(ledger_path)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"SYSTEM 1 SENSOR REFUSAL NOT RECORDED: ledger unavailable: {exc}", file=sys.stderr)
        return 2
    window = None
    try:
        window = build_window()
        emission = emit_harness_trajectory(
            window,
            contract_path=contract_path,
            candidate_version=candidate_version,
            producer_private_key=producer_private_key,
            producer_public_key=producer_public_key,
            trust_public_key=trust_key,
            output_path=output,
            ledger=ledger,
            produced_by=produced_by,
            emitted_at=emitted_at,
        )
    except (OSError, ValueError) as exc:
        reason_code = exc.reason_code if isinstance(exc, System1SensorError) else "invalid_event_window"
        if isinstance(exc, System1SensorError) and reason_code not in {
            "coverage_incomplete", "contract_not_accepted", "observation_not_in_contract",
            "signing_key_invalid", "output_already_exists", "emission_not_accepted",
            "invalid_event_window",
        }:
            reason_code = "emission_not_accepted"
        try:
            record_system1_sensor_refusal(
                ledger,
                reason_code=reason_code,
                reason=str(exc)[:4000],
                produced_by=produced_by,
                source_id=window.source_id if window else None,
                window_id=window.window_id if window else None,
            )
        except (OSError, RuntimeError, ValueError) as ledger_exc:
            print(f"SENSOR REFUSAL NOT RECORDED: {ledger_exc}", file=sys.stderr)
        print(f"SYSTEM 1 SENSOR REFUSED: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(emission.model_dump_json(indent=2))
    else:
        print(
            f"SYSTEM 1 TRAJECTORY EMITTED: {emission.trajectory_id}; "
            f"events={emission.event_count}; source={emission.source_id}/{emission.window_id}; "
            f"key={emission.producer_key_id}; output={output}"
        )
    return 0


def _system1_interchange(
    export_path: Path,
    contract_path: Path,
    *,
    produced_at: str,
    heartbeat_at: str | None,
    candidate_version: str,
    producer_private_key: Path,
    producer_public_key: Path,
    trust_key: Path,
    output: Path,
    ledger_path: Path,
    produced_by: str,
    emitted_at: str | None,
    as_json: bool,
) -> int:
    """Project a runtime-observed interchange export and emit it via the A2 sensor."""

    from autopilot.system1_interchange import interchange_to_harness_window

    def build_window() -> HarnessEventWindow:
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        return interchange_to_harness_window(
            payload,
            produced_at=produced_at,
            heartbeat_at=heartbeat_at,
        )

    return _system1_sensor(
        build_window,
        contract_path,
        candidate_version=candidate_version,
        producer_private_key=producer_private_key,
        producer_public_key=producer_public_key,
        trust_key=trust_key,
        output=output,
        ledger_path=ledger_path,
        produced_by=produced_by,
        emitted_at=emitted_at,
        as_json=as_json,
    )


def _declare_monitor_refusal(
    ledger: Path,
    *,
    reason_code: MonitorRefusalReason,
    reason: str,
    produced_by: str,
) -> None:
    """Declare a refused monitoring cycle in the ledger, never masking the refusal.

    The ledger is mandatory on ``monitor-loop``, so there is no path that skips
    the record: a cycle refusal that could not be recorded would be silent.
    """

    try:
        record_monitor_refusal(
            AuditLedger(ledger),
            reason_code=reason_code,
            reason=reason,
            produced_by=produced_by,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"REFUSAL NOT RECORDED: {exc}", file=sys.stderr)


def _monitor_loop(
    trajectory_path: Path,
    contract_path: Path,
    *,
    producer_private_key: Path,
    producer_public_key: Path,
    trust_key: Path,
    output_dir: Path,
    sensor_trust_key: Path | None,
    interval_seconds: float,
    max_cycles: int,
    snapshot_ttl_seconds: float,
    produced_by: str,
    previous_cycle: Path | None,
    ledger: Path,
    as_json: bool,
    drift_watch: bool,
    drift_baseline: Path | None,
    drift_authority_private_key: Path | None,
    drift_authority_public_key: Path | None,
    drift_learning_cycles: int,
    drift_window_cycles: int,
    drift_respond: bool = False,
    drift_budget_max_runs: int | None = None,
    drift_budget_window_seconds: float | None = None,
    health: bool = False,
) -> int:
    """Emit one or more scheduled continuous-monitoring cycles (System 1 v0.1, OP-5/OP-6)."""

    from autopilot.monitoring_loop import (
        load_previous_cycle,
        run_scheduled_monitoring_loop,
    )

    # Answering a signal without observing first would answer nothing.
    drift_watch = drift_watch or drift_respond

    if drift_watch and (
        drift_authority_private_key is None or drift_authority_public_key is None
    ):
        _declare_monitor_refusal(
            ledger,
            reason_code="parameters_invalid",
            reason=(
                "--drift-watch requires --drift-authority-private-key and "
                "--drift-authority-public-key to sign or verify the baseline"
            ),
            produced_by=produced_by,
        )
        print(
            "MONITOR CYCLE REFUSED [parameters_invalid]: --drift-watch requires "
            "--drift-authority-private-key and --drift-authority-public-key",
            file=sys.stderr,
        )
        return 2

    previous = None
    if previous_cycle is not None:
        try:
            previous = load_previous_cycle(previous_cycle)
        except (OSError, ValueError) as exc:
            _declare_monitor_refusal(
                ledger,
                reason_code="previous_cycle_invalid",
                reason=str(exc),
                produced_by=produced_by,
            )
            print(f"MONITOR CYCLE INVALID: {exc}", file=sys.stderr)
            return 2

    ledger_instance = AuditLedger(ledger)
    from autopilot.drift import DriftRefusalError
    from autopilot.drift_budget import DriftBudgetExhausted
    from autopilot.drift_watch import recover_pending, respond_to_watch, run_drift_watch

    watch_reports: list[DriftWatchReport] = []
    watch_answers: list[DriftResponse | DriftCoalescence] = []
    recovered: list[DriftResponse | DriftCoalescence] = []
    watch_refused = False

    def _recover() -> None:
        """Answer a signal a previous tick recorded but never got to answer.

        Runs before the tick emits anything: an unanswered signal means the
        guard is still judging against an older snapshot, and the crash window
        between ``drift_signal`` and the response record must not become a
        permanent hole in the evidence.
        """

        nonlocal watch_refused
        if not drift_respond:
            return
        budget = _drift_budget_policy(drift_budget_max_runs, drift_budget_window_seconds)
        try:
            answers = recover_pending(
                history_dir=output_dir,
                ledger=ledger_instance,
                producer_private_key=producer_private_key,
                producer_public_key=producer_public_key,
                contract_path=contract_path,
                trust_key_path=trust_key,
                sensor_trust_key_path=sensor_trust_key,
                output_dir=output_dir,
                interval_seconds=interval_seconds,
                snapshot_ttl_seconds=snapshot_ttl_seconds,
                budget=budget,
            )
        except DriftBudgetExhausted as exc:
            watch_refused = True
            print(f"DRIFT RECOVERY BUDGET REFUSED: {exc}", file=sys.stderr)
            return
        except DriftRefusalError as exc:
            watch_refused = True
            print(f"DRIFT RECOVERY REFUSED: {exc}", file=sys.stderr)
            return
        except (OSError, ValueError) as exc:
            watch_refused = True
            print(f"DRIFT RECOVERY REFUSED: {exc}", file=sys.stderr)
            return
        recovered.extend(answers)
        for answer in answers:
            print(_drift_response_line(answer, label="DRIFT RECOVERED"))

    def _watch(_cycle) -> None:
        """Run one drift-watch tick against the history just extended by ``_cycle``."""

        nonlocal watch_refused
        try:
            report = run_drift_watch(
                output_dir,
                # Validated above: --drift-watch refuses without both keys.
                authority_private_key=drift_authority_private_key,
                authority_public_key=drift_authority_public_key,
                baseline_path=drift_baseline,
                learning_cycles=drift_learning_cycles,
                window_cycles=drift_window_cycles,
                ledger=ledger_instance,
            )
        except DriftRefusalError as exc:
            watch_refused = True
            print(f"DRIFT WATCH REFUSED: {exc}", file=sys.stderr)
            return
        except (OSError, ValueError) as exc:
            watch_refused = True
            print(f"DRIFT WATCH REFUSED: {exc}", file=sys.stderr)
            return
        watch_reports.append(report)
        if not drift_respond:
            return
        try:
            answer = respond_to_watch(
                report,
                history_dir=output_dir,
                ledger=ledger_instance,
                producer_private_key=producer_private_key,
                producer_public_key=producer_public_key,
                contract_path=contract_path,
                trust_key_path=trust_key,
                sensor_trust_key_path=sensor_trust_key,
                output_dir=output_dir,
                interval_seconds=interval_seconds,
                snapshot_ttl_seconds=snapshot_ttl_seconds,
                budget=_drift_budget_policy(
                    drift_budget_max_runs, drift_budget_window_seconds
                ),
            )
        except DriftBudgetExhausted as exc:
            # Declared in the ledger before raising: no run, no fresh snapshot,
            # and the previous snapshot keeps aging towards quarantine (I-2).
            watch_refused = True
            print(f"DRIFT BUDGET REFUSED: {exc}", file=sys.stderr)
            return
        except DriftRefusalError as exc:
            watch_refused = True
            print(f"DRIFT RESPONSE REFUSED: {exc}", file=sys.stderr)
            return
        except (OSError, ValueError) as exc:
            watch_refused = True
            print(f"DRIFT RESPONSE REFUSED: {exc}", file=sys.stderr)
            return
        if answer is not None:
            watch_answers.append(answer)

    # Settle evidence a previous process left unanswered, then observe.
    _recover()

    try:
        cycles = run_scheduled_monitoring_loop(
            trajectory_source=trajectory_path,
            contract_path=contract_path,
            producer_private_key=producer_private_key,
            producer_public_key=producer_public_key,
            trust_key_path=trust_key,
            output_dir=output_dir,
            sensor_trust_key_path=sensor_trust_key,
            produced_by=produced_by,
            interval_seconds=interval_seconds,
            snapshot_ttl_seconds=snapshot_ttl_seconds,
            max_cycles=max_cycles,
            previous=previous,
            ledger=ledger_instance,
            after_cycle=_watch if drift_watch else None,
        )
    except MonitorRefusalError as exc:
        _declare_monitor_refusal(
            ledger,
            reason_code=exc.reason_code,
            reason=str(exc),
            produced_by=produced_by,
        )
        if exc.reason_code == "previous_cycle_invalid":
            print(f"MONITOR CYCLE INVALID: {exc}", file=sys.stderr)
        else:
            print(
                f"MONITOR CYCLE REFUSED [{exc.reason_code}]: {exc}",
                file=sys.stderr,
            )
        return 2
    except (OSError, ValueError) as exc:
        _declare_monitor_refusal(
            ledger,
            reason_code="cycle_not_accepted",
            reason=str(exc),
            produced_by=produced_by,
        )
        print(f"MONITOR CYCLE REFUSED: {exc}", file=sys.stderr)
        return 2

    health_report = (
        _sensor_health_report(output_dir, ledger=ledger, trust_key=trust_key) if health else None
    )
    if as_json:
        if drift_watch:
            print(
                json.dumps(
                    {
                        "cycles": [item.model_dump(mode="json") for item in cycles],
                        "drift_watch": [
                            item.model_dump(mode="json") for item in watch_reports
                        ],
                        "drift_recovered": [
                            item.model_dump(mode="json") for item in recovered
                        ],
                        "drift_responses": [
                            item.model_dump(mode="json") for item in watch_answers
                        ],
                        "health": (
                            health_report.model_dump(mode="json")
                            if health_report is not None
                            else None
                        ),
                    },
                    indent=2,
                )
            )
        elif health_report is not None:
            print(
                json.dumps(
                    {
                        "cycles": [item.model_dump(mode="json") for item in cycles],
                        "health": health_report.model_dump(mode="json"),
                    },
                    indent=2,
                )
            )
        elif len(cycles) == 1:
            print(cycles[0].model_dump_json(indent=2))
        else:
            print(json.dumps([item.model_dump(mode="json") for item in cycles], indent=2))
    else:
        for cycle in cycles:
            print(
                f"CYCLE: {cycle.cycle_id}; change={cycle.change.value}; "
                f"{cycle.evaluation_status.upper()} {cycle.contract_id} "
                f"v{cycle.contract_version} ({cycle.recommended_autonomy}; "
                f"block_release={str(cycle.block_release).lower()})"
            )
            print(
                f"  snapshot {cycle.snapshot_id} vigente hasta {cycle.snapshot_expires_at}; "
                f"salida: {output_dir}"
            )
        for report in watch_reports:
            print(_drift_watch_line(report))
        for answer in watch_answers:
            print(_drift_response_line(answer))
        if health_report is not None:
            print(health_report.line())
    return 2 if watch_refused else 0


def _declare_drift_refusal(
    ledger: Path,
    *,
    stage: Literal["check", "respond"],
    reason_code: DriftRefusalReason,
    reason: str,
    produced_by: str,
) -> None:
    """Declare a boundary refusal in the ledger, never masking the refusal itself.

    The ledger is mandatory on every drift command, so there is no path that
    skips the record: a refusal that could not be recorded would be silent.
    """

    try:
        record_drift_refusal(
            AuditLedger(ledger),
            stage=stage,
            reason_code=reason_code,
            reason=reason,
            produced_by=produced_by,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"REFUSAL NOT RECORDED: {exc}", file=sys.stderr)


def _drift_baseline(
    window_dir: Path,
    *,
    private_key: Path,
    public_key: Path,
    previous_path: Path | None,
    approved_by: str | None,
    produced_by: str,
    output: Path | None,
    as_json: bool,
) -> int:
    """Build a signed, versioned baseline from a learning window (System 1 OP-2)."""

    from autopilot.drift import (
        build_baseline,
        load_signed_baseline,
        load_window_from_dir,
        write_baseline,
    )

    try:
        window = load_window_from_dir(window_dir)
        previous = (
            load_signed_baseline(previous_path, public_key) if previous_path is not None else None
        )
        baseline = build_baseline(
            window,
            authority_private_key=private_key,
            authority_public_key=public_key,
            produced_by=produced_by,
            previous=previous,
            approved_by=approved_by,
        )
    except (OSError, ValueError) as exc:
        print(f"BASELINE REFUSED: {exc}", file=sys.stderr)
        return 2

    document = json.dumps(baseline.model_dump(mode="json"), indent=2) + "\n"
    if output is not None:
        write_baseline(baseline, output)
    if as_json or output is None:
        sys.stdout.write(document)
    else:
        payload = baseline.payload
        print(
            f"BASELINE: {payload.baseline_id} v{payload.baseline_version}; "
            f"{payload.contract_id} v{payload.contract_version}; "
            f"{payload.window_cycles} cycles; "
            f"supersedes={payload.supersedes_baseline_id or '-'}"
        )
    return 0


def _drift_check(
    baseline_path: Path,
    window_dir: Path,
    *,
    trust_key: Path,
    latest_baseline_id: str | None,
    ledger: Path,
    max_status_shift: float,
    max_autonomy_shift: float,
    max_change_shift: float,
    ignore_new_categories: bool,
    produced_by: str,
    as_json: bool,
) -> int:
    """Compare a window against a verified baseline and record the signal (OP-3)."""

    from autopilot.drift import (
        DriftTolerances,
        detect_drift,
        load_signed_baseline,
        load_window_from_dir,
    )

    try:
        baseline = load_signed_baseline(baseline_path, trust_key)
    except (OSError, ValueError) as exc:
        _declare_drift_refusal(
            ledger,
            stage="check",
            reason_code="baseline_not_accepted",
            reason=str(exc),
            produced_by=produced_by,
        )
        print(f"DRIFT CHECK REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        window = load_window_from_dir(window_dir)
    except (OSError, ValueError) as exc:
        _declare_drift_refusal(
            ledger,
            stage="check",
            reason_code="window_not_accepted",
            reason=str(exc),
            produced_by=produced_by,
        )
        print(f"DRIFT CHECK REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        tolerances = DriftTolerances(
            max_status_shift=max_status_shift,
            max_autonomy_shift=max_autonomy_shift,
            max_change_shift=max_change_shift,
            flag_new_categories=not ignore_new_categories,
        )
        ledger_instance = AuditLedger(ledger)
        signal = detect_drift(
            baseline,
            window,
            trust_key_path=trust_key,
            tolerances=tolerances,
            produced_by=produced_by,
            latest_baseline_id=latest_baseline_id,
            ledger=ledger_instance,
        )
    except (OSError, ValueError) as exc:
        print(f"DRIFT CHECK REFUSED: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(signal.model_dump_json(indent=2))
    else:
        print(
            f"SIGNAL: {signal.signal_id}; status={signal.status.value}; "
            f"baseline={signal.baseline_id} v{signal.baseline_version}; "
            f"cycles={signal.window_cycles}; recorded="
            f"{str(signal.status.value != 'clear').lower()}"
        )
        for reason in signal.reasons:
            print(f"  - {reason}")
    return 0


def _drift_budget_policy(
    max_runs: int | None, window_seconds: float | None
) -> "BudgetPolicy | None":
    """Declared budget for the automatic response, or the module defaults."""

    if max_runs is None and window_seconds is None:
        return None
    from autopilot.drift_budget import BudgetPolicy

    return BudgetPolicy(
        max_runs=max_runs if max_runs is not None else BudgetPolicy().max_runs,
        window_seconds=(
            window_seconds if window_seconds is not None else BudgetPolicy().window_seconds
        ),
    )


def _drift_response_line(
    answer: "DriftResponse | DriftCoalescence", *, label: str = "DRIFT RESPONSE"
) -> str:
    """Render the answer to a drift signal as one operator-readable line."""

    from autopilot.drift_budget import DriftCoalescence

    if isinstance(answer, DriftCoalescence):
        return (
            f"{label}: coalesced; signal {answer.signal_id} folded into "
            f"response {answer.response_id}; runs_spent=0; snapshot "
            f"{answer.snapshot_id} vigente hasta {answer.snapshot_expires_at}"
        )
    response = answer
    return (
        f"{label}: {response.response_id}; signal {response.signal_id}; "
        f"{response.evaluation_status.upper()} {response.contract_id} "
        f"v{response.contract_version} ({response.recommended_autonomy}; "
        f"block_release={str(response.block_release).lower()}); snapshot "
        f"{response.snapshot_id} vigente hasta {response.snapshot_expires_at}"
    )


def _sensor_health_report(
    output_dir: Path, *, ledger: Path, trust_key: Path | None
) -> "SensorHealthReport | None":
    """Build the one-line health report, or ``None`` when it cannot be read."""

    from autopilot.sensor_health import build_sensor_health

    if not ledger.is_file():
        # Opening an absent path would create an empty ledger: the report would
        # then say "nothing pending" about evidence that was never there, and
        # a read-only command would have written a file.
        print(f"SENSOR HEALTH UNAVAILABLE: ledger {ledger} does not exist", file=sys.stderr)
        return None
    try:
        return build_sensor_health(
            output_dir, ledger=AuditLedger(ledger), trust_key_path=trust_key
        )
    except (OSError, ValueError) as exc:
        print(f"SENSOR HEALTH UNAVAILABLE: {exc}", file=sys.stderr)
        return None


def _sensor_health(
    output_dir: Path,
    *,
    ledger: Path,
    trust_key: Path | None,
    checked_at: str | None,
    as_json: bool,
) -> int:
    """Report the sensor side in one line (read-only, exit 0/1/2)."""

    from autopilot.sensor_health import SensorHealthStatus, build_sensor_health

    if not ledger.is_file():
        print(f"SENSOR HEALTH UNAVAILABLE: ledger {ledger} does not exist", file=sys.stderr)
        return 2
    try:
        report = build_sensor_health(
            output_dir,
            ledger=AuditLedger(ledger),
            checked_at=checked_at,
            trust_key_path=trust_key,
        )
    except (OSError, ValueError) as exc:
        print(f"SENSOR HEALTH UNAVAILABLE: {exc}", file=sys.stderr)
        return 2

    print(report.model_dump_json(indent=2) if as_json else report.line())
    return {
        SensorHealthStatus.ok: 0,
        SensorHealthStatus.degraded: 1,
        SensorHealthStatus.critical: 2,
    }[report.status]


def _drift_watch_line(report: "DriftWatchReport") -> str:
    """Render one drift-watch tick as a single operator-readable line."""

    from autopilot.drift_watch import DriftWatchOutcome

    baseline = report.baseline_id or "-"
    if report.outcome is DriftWatchOutcome.insufficient_window:
        return f"DRIFT WATCH: insufficient_window; {report.detail}"
    if report.outcome is DriftWatchOutcome.baseline_created:
        return (
            f"DRIFT WATCH: baseline_created; {baseline} v{report.baseline_version}; "
            f"{report.detail}"
        )
    if report.signal is not None and report.signal.status.value != "clear":
        return (
            f"DRIFT WATCH: {report.outcome.value}; signal {report.signal.signal_id}; "
            f"baseline {baseline} v{report.baseline_version}; "
            f"cycles={report.signal.window_cycles}"
        )
    return (
        f"DRIFT WATCH: {report.outcome.value}; baseline {baseline} "
        f"v{report.baseline_version}; {report.detail}"
    )


def _drift_watch(
    history_dir: Path,
    *,
    authority_private_key: Path,
    authority_public_key: Path,
    trust_key: Path | None,
    baseline: Path | None,
    learning_cycles: int,
    window_cycles: int,
    latest_baseline_id: str | None,
    ledger: Path,
    produced_by: str,
    as_json: bool,
) -> int:
    """Chain the cycle history to OP-2/OP-3 and record the resulting signal."""

    from autopilot.drift import DriftRefusalError
    from autopilot.drift_watch import run_drift_watch

    try:
        report = run_drift_watch(
            history_dir,
            authority_private_key=authority_private_key,
            authority_public_key=authority_public_key,
            trust_key_path=trust_key,
            baseline_path=baseline,
            learning_cycles=learning_cycles,
            window_cycles=window_cycles,
            latest_baseline_id=latest_baseline_id,
            produced_by=produced_by,
            ledger=AuditLedger(ledger),
        )
    except DriftRefusalError as exc:
        print(f"DRIFT WATCH REFUSED: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        _declare_drift_refusal(
            ledger,
            stage="check",
            reason_code="window_not_accepted",
            reason=str(exc),
            produced_by=produced_by,
        )
        print(f"DRIFT WATCH REFUSED: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        print(_drift_watch_line(report))
        if report.signal is not None:
            for reason in report.signal.reasons:
                print(f"  - {reason}")
    return 0


def _drift_respond(
    signal_path: Path,
    trajectory_path: Path,
    contract_path: Path,
    *,
    producer_private_key: Path,
    producer_public_key: Path,
    trust_key: Path,
    output_dir: Path,
    previous_cycle: Path | None,
    ledger: Path,
    produced_by: str,
    as_json: bool,
    budget_max_runs: int | None,
    budget_window_seconds: float | None,
    sensor_trust_key: Path | None = None,
) -> int:
    """Trigger one deterministic evaluation run from a recorded drift signal."""

    from autopilot.drift import DriftSignal
    from autopilot.drift_budget import (
        DEFAULT_BUDGET_MAX_RUNS,
        DEFAULT_BUDGET_WINDOW_SECONDS,
        BudgetPolicy,
        DriftBudgetExhausted,
        DriftCoalescence,
    )
    from autopilot.drift_response import run_drift_response
    from autopilot.monitoring_loop import load_previous_cycle

    try:
        signal = DriftSignal.model_validate_json(signal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _declare_drift_refusal(
            ledger,
            stage="respond",
            reason_code="signal_not_accepted",
            reason=str(exc),
            produced_by=produced_by,
        )
        print(f"DRIFT SIGNAL INVALID: {exc}", file=sys.stderr)
        return 2
    previous = None
    if previous_cycle is not None:
        try:
            previous = load_previous_cycle(previous_cycle)
        except (OSError, ValueError) as exc:
            _declare_drift_refusal(
                ledger,
                stage="respond",
                reason_code="previous_cycle_invalid",
                reason=str(exc),
                produced_by=produced_by,
            )
            print(f"DRIFT RESPONSE REFUSED: {exc}", file=sys.stderr)
            return 2
    try:
        policy = BudgetPolicy(
            max_runs=(
                budget_max_runs if budget_max_runs is not None else DEFAULT_BUDGET_MAX_RUNS
            ),
            window_seconds=(
                budget_window_seconds
                if budget_window_seconds is not None
                else DEFAULT_BUDGET_WINDOW_SECONDS
            ),
        )
    except ValueError as exc:
        _declare_drift_refusal(
            ledger,
            stage="respond",
            reason_code="cost_control_unreadable",
            reason=f"declared budget policy is not readable: {exc}",
            produced_by=produced_by,
        )
        print(f"DRIFT RESPONSE REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        response = run_drift_response(
            signal,
            ledger=AuditLedger(ledger),
            trajectory_path=trajectory_path,
            contract_path=contract_path,
            producer_private_key=producer_private_key,
            producer_public_key=producer_public_key,
            trust_key_path=trust_key,
            output_dir=output_dir,
            sensor_trust_key_path=sensor_trust_key,
            produced_by=produced_by,
            previous=previous,
            budget=policy,
        )
    except DriftBudgetExhausted as exc:
        print(f"DRIFT BUDGET REFUSED: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"DRIFT RESPONSE REFUSED: {exc}", file=sys.stderr)
        return 2

    if isinstance(response, DriftCoalescence):
        if as_json:
            print(response.model_dump_json(indent=2))
        else:
            print(
                f"COALESCED: {response.coalescence_id}; signal={response.signal_id} "
                f"folds into response {response.response_id} "
                f"(episode {response.triggered_cycle_id}; shared cycles "
                f"{', '.join(response.shared_cycle_ids)}); no new run"
            )
            print(
                f"  the recorded snapshot {response.snapshot_id} remains valid until "
                f"{response.snapshot_expires_at}"
            )
        return 0

    if as_json:
        print(response.model_dump_json(indent=2))
    else:
        print(
            f"RESPONSE: {response.response_id}; signal={response.signal_id}; "
            f"cycle={response.triggered_cycle_id}; "
            f"{response.evaluation_status.upper()} {response.contract_id} "
            f"v{response.contract_version} ({response.recommended_autonomy}; "
            f"block_release={str(response.block_release).lower()})"
        )
        print(
            f"  snapshot {response.snapshot_id} vigente hasta "
            f"{response.snapshot_expires_at}; salida: {output_dir}"
        )
    return 0


def _adapt(
    contract_path: Path,
    adapter_path: Path,
    session_path: Path,
    *,
    as_json: bool,
    output: Path | None,
) -> int:
    try:
        contract = load_contract(contract_path)
        config = load_adapter_config(adapter_path)
        session = load_raw_session(session_path)
        trajectory = adapt_session(contract, config, session)
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("ADAPTER INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    payload = trajectory.model_dump(mode="json", by_alias=True, exclude_none=True)
    rendered = (
        json.dumps(payload, indent=2, ensure_ascii=False)
        if as_json
        else yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    )
    if output is None:
        print(rendered)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + ("" if rendered.endswith("\n") else "\n"), encoding="utf-8")
        print(f"WROTE: {output} ({len(trajectory.observations)} observations)")
    return 0


def _incidents(
    contract_path: Path,
    trajectory_path: Path,
    *,
    database: Path | None,
    as_json: bool,
) -> int:
    try:
        contract = load_contract(contract_path)
        trajectory = load_trajectory(trajectory_path)
        evaluation = evaluate_contract(contract, trajectory)
        batch = detect_incidents(contract, evaluation)
        registration = IncidentRegistry(database).register(batch) if database else None
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("INCIDENT INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    if as_json:
        payload = batch.model_dump(mode="json")
        payload["registration"] = (
            registration.model_dump(mode="json") if registration is not None else None
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"INCIDENTS: {batch.incident_count} detected for {batch.trajectory_id}"
        )
        for incident in batch.incidents:
            print(
                f"  {incident.severity.value:8} {incident.incident_id} "
                f"{incident.clause_id}: {incident.reason}"
            )
        if registration is not None:
            print(
                f"REGISTERED: {registration.inserted} new, "
                f"{registration.duplicates} duplicate(s) in {registration.registry_path}"
            )
    return 1 if batch.incident_count else 0


def _diagnose(
    contract_path: Path,
    trajectory_path: Path,
    *,
    output_dir: Path | None,
    as_json: bool,
) -> int:
    try:
        contract = load_contract(contract_path)
        trajectory = load_trajectory(trajectory_path)
        evaluation = evaluate_contract(contract, trajectory)
        incidents = detect_incidents(contract, evaluation)
        batch = generate_change_cards(contract, incidents)
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("DIAGNOSTIC INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for card in batch.cards:
            json_path = output_dir / f"{card.card_id}.json"
            markdown_path = output_dir / f"{card.card_id}.md"
            json_path.write_text(
                json.dumps(card.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            markdown_path.write_text(
                render_change_card_markdown(card),
                encoding="utf-8",
            )

    if as_json:
        print(json.dumps(batch.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(
            f"CHANGE CARDS: {batch.card_count} generated for {batch.trajectory_id} "
            "(hypotheses only; no changes applied)"
        )
        for card in batch.cards:
            print(
                f"  {card.card_id} {card.incident.clause_id}: "
                f"{len(card.hypotheses)} hypotheses, {len(card.candidates)} candidates"
            )
        if output_dir is not None:
            print(f"WROTE: {batch.card_count * 2} review files in {output_dir}")
    return 1 if batch.card_count else 0


def _sandbox(
    contract_path: Path,
    card_path: Path,
    trajectory_paths: list[Path],
    *,
    candidate_id: str,
    approved_by: str,
    approved_at: str,
    output: Path | None,
    as_json: bool,
) -> int:
    try:
        contract = load_contract(contract_path)
        card = load_change_card(card_path)
        trajectories = [load_trajectory(path) for path in trajectory_paths]
        try:
            approval = SandboxApproval(actor=approved_by, approved_at=approved_at)
        except ValidationError as exc:
            issues = [
                ValidationIssue(
                    ".".join(str(part) for part in error["loc"]),
                    error["msg"],
                )
                for error in exc.errors()
            ]
            raise DSLValidationError(issues) from exc
        report = run_candidate_sandbox(
            contract,
            card,
            candidate_id,
            approval,
            trajectories,
        )
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("SANDBOX INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    rendered = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")

    if as_json:
        print(rendered)
    else:
        print(
            f"SANDBOX {report.decision.value.upper()}: {report.report_id} "
            f"({len(report.runs)} run(s); candidate {report.candidate_version})"
        )
        for check in report.acceptance_checks:
            print(f"  {check.status.value:13} {check.check}: {check.reason}")
        print(
            "RELEASE: blocked; "
            + (
                "eligible for separate human review"
                if report.eligible_for_release_review
                else "not eligible for review"
            )
        )
        if output is not None:
            print(f"WROTE: {output}")
    return 0 if report.decision is SandboxStatus.passed else 1


def _review_keygen(private_key: Path, public_key: Path) -> int:
    try:
        key_id = generate_review_keypair(private_key, public_key)
    except DSLValidationError as exc:
        print("REVIEW KEY INPUT INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    print(f"REVIEW KEY GENERATED: {key_id}")
    print(f"  private: {private_key}")
    print(f"  public:  {public_key}")
    return 0


def _review(
    report_path: Path,
    *,
    decision: str,
    reviewer_actor: str,
    role: str,
    rationale: str,
    reference: str,
    decided_at: str,
    private_key: Path,
    output: Path,
    as_json: bool,
) -> int:
    if output.exists():
        issue = ValidationIssue("output", f"already exists: {output}")
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [{"path": issue.path, "message": issue.message}],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("REVIEW INPUT INVALID", file=sys.stderr)
            print(f"  - {issue}", file=sys.stderr)
        return 2
    try:
        report = load_sandbox_report(report_path)
        try:
            reviewer = ReviewerIdentity(actor=reviewer_actor, role=role)
        except ValidationError as exc:
            issues = [
                ValidationIssue(
                    ".".join(str(part) for part in error["loc"]),
                    error["msg"],
                )
                for error in exc.errors()
            ]
            raise DSLValidationError(issues) from exc
        review = create_release_review(
            report,
            decision=ReviewDecision(decision),
            reviewer=reviewer,
            rationale=rationale,
            reference=reference,
            decided_at=decided_at,
            private_key_path=private_key,
        )
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("REVIEW INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    rendered = json.dumps(review.model_dump(mode="json"), indent=2, ensure_ascii=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    if as_json:
        print(rendered)
    else:
        print(
            f"REVIEW SIGNED: {review.review_id} ({review.decision.value}; "
            f"key {review.signature.key_id})"
        )
        print(
            "STAGED RELEASE: "
            + ("authorized" if review.staged_release_authorized else "not authorized")
        )
        print("PRODUCTION DEPLOYMENT: blocked; separate authorization required")
        print(f"WROTE: {output}")
    return 0 if review.staged_release_authorized else 1


def _parse_public_keys(raws: list[str]) -> dict[str, Path]:
    """Parse a list of ``actor=path`` tokens into a dict[str, Path] mapping."""

    out: dict[str, Path] = {}
    for raw in raws:
        if "=" not in raw:
            raise DSLValidationError(
                [ValidationIssue("public-key", f"expected actor=path, got {raw!r}")]
            )
        actor, path_str = raw.split("=", 1)
        actor = actor.strip()
        if not actor:
            raise DSLValidationError(
                [ValidationIssue("public-key", f"missing actor in {raw!r}")]
            )
        if actor in out:
            raise DSLValidationError(
                [ValidationIssue("public-key", f"duplicate actor: {actor!r}")]
            )
        out[actor] = Path(path_str.strip())
    return out


def _verify_review(
    review_path: Path,
    public_key: Path | None,
    public_keys: list[str],
    *,
    as_json: bool,
) -> int:
    try:
        review = load_release_review(review_path)
        chain_present = bool(review.delegation_chain)
        if chain_present:
            mapping = _parse_public_keys(public_keys)
            if public_key is not None:
                raise DSLValidationError(
                    [
                        ValidationIssue(
                            "--public-key",
                            "must not be supplied when the review carries a "
                            "delegation_chain; pass --public-keys instead",
                        )
                    ]
                )
            if not mapping:
                raise DSLValidationError(
                    [
                        ValidationIssue(
                            "--public-keys",
                            "is required for chain-mode verification",
                        )
                    ]
                )
            verification = verify_release_review(review, mapping)
        else:
            if public_keys:
                raise DSLValidationError(
                    [
                        ValidationIssue(
                            "--public-keys",
                            "must not be supplied for single-signature reviews; "
                            "use --public-key",
                        )
                    ]
                )
            if public_key is None:
                raise DSLValidationError(
                    [
                        ValidationIssue(
                            "--public-key",
                            "is required for single-signature reviews",
                        )
                    ]
                )
            verification = verify_release_review(review, public_key)
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("REVIEW VERIFICATION INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(verification.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(
            f"SIGNATURE {'VALID' if verification.valid else 'INVALID'}: "
            f"{verification.review_id}"
        )
        print(
            "STAGED RELEASE: "
            + (
                "authorized"
                if verification.valid and verification.staged_release_authorized
                else "not authorized"
            )
        )
        print("PRODUCTION DEPLOYMENT: blocked")
        if verification.delegation_chain_present:
            print(
                f"DELEGATION CHAIN: "
                f"{verification.chain_actor_count} actor(s); "
                f"valid={str(verification.delegation_chain_valid).lower()}"
            )
        for problem in verification.problems:
            print(f"  - {problem}")
    return 0 if verification.valid else 1


def _audit_record(ledger_path: Path, artifact_path: Path, public_key: Path | None) -> int:
    try:
        ledger = AuditLedger(ledger_path)
        artifact = load_audit_artifact(artifact_path)
        if isinstance(artifact, ChangeCard):
            results = record_change_card(ledger, artifact)
        elif isinstance(artifact, SandboxReport):
            results = [record_sandbox_report(ledger, artifact)]
        elif isinstance(artifact, ReleaseReview):
            if public_key is None:
                raise DSLValidationError(
                    [ValidationIssue("public_key", "is required for a signed review")]
                )
            results = [record_release_review(ledger, artifact, public_key)]
        else:  # pragma: no cover
            raise AssertionError("unsupported audit artifact")
    except (DSLValidationError, RuntimeError) as exc:
        print("AUDIT INPUT INVALID", file=sys.stderr)
        if isinstance(exc, DSLValidationError):
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        else:
            print(f"  - {exc}", file=sys.stderr)
        return 2
    for result in results:
        event = result.event
        verb = "APPENDED" if result.inserted else "EXISTS"
        print(f"{verb}: #{event.sequence} {event.event_type.value} {event.event_id}")
    return 0


def _audit_verify(ledger_path: Path, *, as_json: bool) -> int:
    try:
        verification = verify_audit_ledger(AuditLedger(ledger_path))
    except RuntimeError as exc:
        print(f"AUDIT INPUT INVALID: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(verification.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(
            f"AUDIT {'VALID' if verification.valid else 'INVALID'}: "
            f"{verification.event_count} event(s); head {verification.head_hash}"
        )
        print(
            "ACTIVE STAGED REVIEWS: "
            + (", ".join(verification.active_staged_review_ids) or "none")
        )
        for problem in verification.problems:
            print(f"  - {problem}")
    return 0 if verification.valid else 1


def _audit_list(ledger_path: Path, *, as_json: bool) -> int:
    try:
        events = AuditLedger(ledger_path).events()
    except RuntimeError as exc:
        print(f"AUDIT INPUT INVALID: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps([item.model_dump(mode="json") for item in events], indent=2))
    else:
        for event in events:
            print(
                f"#{event.sequence:04d} {event.event_type.value:24} "
                f"{event.subject_id} {event.event_hash}"
            )
    return 0


def _audit_export(ledger_path: Path, output: Path | None, *, as_json: bool) -> int:
    try:
        bundle = export_ledger_bundle(AuditLedger(ledger_path))
    except DSLValidationError as exc:
        print("AUDIT EXPORT REFUSED", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"AUDIT INPUT INVALID: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    if output is None or as_json:
        print(payload, end="")
    if output is not None and not as_json:
        ledger = bundle["ledger"]
        print(
            f"EXPORTED: {ledger['event_count']} event(s); "
            f"head {ledger['head_hash']}"
        )
        print(f"BUNDLE: {output}")
    return 0


def _build_revocation(model, **values):
    try:
        return model(**values)
    except ValidationError as exc:
        issues = [
            ValidationIssue(
                ".".join(str(part) for part in error["loc"]),
                error["msg"],
            )
            for error in exc.errors()
        ]
        raise DSLValidationError(issues) from exc


def _audit_revoke_key(ledger_path: Path, key_id: str, **values) -> int:
    try:
        revocation = _build_revocation(KeyRevocation, key_id=key_id, **values)
        result = revoke_signing_key(AuditLedger(ledger_path), revocation)
    except DSLValidationError as exc:
        print("AUDIT REVOCATION INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    print(f"REVOKED KEY: {key_id} ({result.event.event_id})")
    return 0


def _audit_revoke_review(ledger_path: Path, review_id: str, **values) -> int:
    try:
        revocation = _build_revocation(ReviewRevocation, review_id=review_id, **values)
        result = revoke_release_review(AuditLedger(ledger_path), revocation)
    except DSLValidationError as exc:
        print("AUDIT REVOCATION INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    print(f"REVOKED REVIEW: {review_id} ({result.event.event_id})")
    return 0


def _agent_evaluate(
    contract_path: Path,
    adapter_path: Path,
    agent_path: Path,
    scenario_path: Path,
    output_dir: Path,
    *,
    as_json: bool,
) -> int:
    outputs = {
        "manifest": output_dir / "candidate_manifest.json",
        "raw_session": output_dir / "raw_session.yaml",
        "trajectory": output_dir / "trajectory.yaml",
        "evaluation": output_dir / "evaluation.json",
        "operational_trace": output_dir / "operational_trace.yaml",
        "operational_evaluation": output_dir / "operational_evaluation.json",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing:
        print("AGENT OUTPUT INVALID", file=sys.stderr)
        print("  - output files already exist: " + ", ".join(existing), file=sys.stderr)
        return 2
    try:
        contract = load_contract(contract_path)
        adapter = load_adapter_config(adapter_path)
        config = load_agent_config(agent_path)
        scenario = load_agent_scenario(scenario_path)
        bundle = run_agent_evaluation(contract, adapter, config, scenario)
    except DSLValidationError as exc:
        print("AGENT INPUT INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    except AgentExecutionError as exc:
        print(f"AGENT EXECUTION FAILED: {exc}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs["manifest"].write_text(
        bundle.agent_run.manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    raw_payload = bundle.agent_run.raw_session.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    outputs["raw_session"].write_text(
        yaml.safe_dump(raw_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    trajectory_payload = bundle.trajectory.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    outputs["trajectory"].write_text(
        yaml.safe_dump(trajectory_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    outputs["evaluation"].write_text(
        bundle.evaluation.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    if contract.operational_integrity is not None:
        operational_trace_payload = bundle.agent_run.operational_trace.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        outputs["operational_trace"].write_text(
            yaml.safe_dump(
                operational_trace_payload,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        outputs["operational_evaluation"].write_text(
            bundle.operational_evaluation.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    behavioural_status = bundle.evaluation.status.value
    operational_status = bundle.operational_evaluation.status.value
    if "fail" in {behavioural_status, operational_status}:
        overall_status = "fail"
    elif "indeterminate" in {behavioural_status, operational_status}:
        overall_status = "indeterminate"
    else:
        overall_status = behavioural_status
    if operational_status in {"fail", "indeterminate"}:
        recommended_autonomy = bundle.operational_evaluation.recommended_autonomy
    else:
        recommended_autonomy = bundle.evaluation.recommended_autonomy
    block_release = (
        bundle.evaluation.block_release or bundle.operational_evaluation.block_release
    )
    summary = {
        "status": overall_status,
        "behavioral_status": behavioural_status,
        "operational_status": operational_status,
        "trajectory_id": bundle.trajectory.trajectory_id,
        "candidate_version": bundle.agent_run.manifest.candidate_version,
        "candidate_digest": bundle.agent_run.manifest.candidate_digest,
        "manifest_id": bundle.agent_run.manifest.manifest_id,
        "recommended_autonomy": recommended_autonomy.value,
        "block_release": block_release,
        "output_dir": str(output_dir),
    }
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"AGENT {overall_status.upper()}: "
            f"{bundle.agent_run.manifest.agent_id} {bundle.agent_run.manifest.candidate_version}"
        )
        print(f"CANDIDATE: {bundle.agent_run.manifest.candidate_digest}")
        print(
            f"BEHAVIOURAL: {behavioural_status}; OPERATIONAL: {operational_status}"
        )
        print(
            f"AUTONOMY: {recommended_autonomy.value}; "
            f"block_release={str(block_release).lower()}"
        )
        print(f"WROTE: {output_dir}")
    return 0 if overall_status == "pass" else 1


def _agent_sandbox_run(
    contract_path: Path,
    adapter_path: Path,
    sandbox_config_path: Path,
    scenario_path: Path,
    output_dir: Path,
    *,
    as_json: bool,
) -> int:
    outputs = {
        "manifest": output_dir / "candidate_manifest.json",
        "raw_session": output_dir / "raw_session.yaml",
        "trajectory": output_dir / "trajectory.yaml",
        "evaluation": output_dir / "evaluation.json",
        "telemetry_interchange": output_dir / "telemetry_interchange.json",
        "operational_trace": output_dir / "operational_trace.yaml",
        "operational_evaluation": output_dir / "operational_evaluation.json",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing:
        print("SANDBOX OUTPUT INVALID", file=sys.stderr)
        print("  - output files already exist: " + ", ".join(existing), file=sys.stderr)
        return 2
    try:
        contract = load_contract(contract_path)
        adapter = load_adapter_config(adapter_path)
        sandbox_config = load_declarative_sandbox_config(sandbox_config_path)
        scenario = load_agent_scenario(scenario_path)
        sandbox_bundle = run_declarative_sandbox_evaluation(
            contract,
            adapter,
            sandbox_config,
            scenario,
        )
    except DSLValidationError as exc:
        print("SANDBOX INPUT INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    except AgentExecutionError as exc:
        print(f"SANDBOX EXECUTION FAILED: {exc}", file=sys.stderr)
        return 2

    bundle = sandbox_bundle.agent_evaluation
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs["manifest"].write_text(
        bundle.agent_run.manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    raw_payload = bundle.agent_run.raw_session.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    outputs["raw_session"].write_text(
        yaml.safe_dump(raw_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    trajectory_payload = bundle.trajectory.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    outputs["trajectory"].write_text(
        yaml.safe_dump(trajectory_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    outputs["evaluation"].write_text(
        bundle.evaluation.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    if sandbox_bundle.telemetry_interchange is not None:
        outputs["telemetry_interchange"].write_text(
            sandbox_bundle.telemetry_interchange.model_dump_json(
                indent=2, exclude_none=True
            )
            + "\n",
            encoding="utf-8",
        )
    if contract.operational_integrity is not None:
        operational_trace_payload = bundle.agent_run.operational_trace.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        outputs["operational_trace"].write_text(
            yaml.safe_dump(
                operational_trace_payload,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        outputs["operational_evaluation"].write_text(
            bundle.operational_evaluation.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    behavioural_status = bundle.evaluation.status.value
    operational_status = bundle.operational_evaluation.status.value
    if "fail" in {behavioural_status, operational_status}:
        overall_status = "fail"
    elif "indeterminate" in {behavioural_status, operational_status}:
        overall_status = "indeterminate"
    else:
        overall_status = behavioural_status
    if operational_status in {"fail", "indeterminate"}:
        recommended_autonomy = bundle.operational_evaluation.recommended_autonomy
    else:
        recommended_autonomy = bundle.evaluation.recommended_autonomy
    block_release = (
        bundle.evaluation.block_release or bundle.operational_evaluation.block_release
    )
    summary = {
        "sandbox_id": sandbox_bundle.sandbox_id,
        "status": overall_status,
        "behavioral_status": behavioural_status,
        "operational_status": operational_status,
        "trajectory_id": bundle.trajectory.trajectory_id,
        "candidate_version": bundle.agent_run.manifest.candidate_version,
        "candidate_digest": bundle.agent_run.manifest.candidate_digest,
        "manifest_id": bundle.agent_run.manifest.manifest_id,
        "recommended_autonomy": recommended_autonomy.value,
        "block_release": block_release,
        "output_dir": str(output_dir),
    }
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"SANDBOX {overall_status.upper()}: "
            f"{sandbox_bundle.sandbox_id} ({bundle.agent_run.manifest.agent_id} "
            f"{bundle.agent_run.manifest.candidate_version})"
        )
        print(f"CANDIDATE: {bundle.agent_run.manifest.candidate_digest}")
        print(
            f"BEHAVIOURAL: {behavioural_status}; OPERATIONAL: {operational_status}"
        )
        print(
            f"AUTONOMY: {recommended_autonomy.value}; "
            f"block_release={str(block_release).lower()}"
        )
        print(f"WROTE: {output_dir}")
    return 0 if overall_status == "pass" else 1


def _agent_cohort_evaluate(
    contract_path: Path,
    adapter_path: Path,
    cohort_path: Path,
    output_dir: Path,
    *,
    as_json: bool,
) -> int:
    report_path = output_dir / "cohort_report.json"
    if report_path.exists():
        print(f"COHORT OUTPUT INVALID: already exists: {report_path}", file=sys.stderr)
        return 2
    try:
        contract = load_contract(contract_path)
        adapter = load_adapter_config(adapter_path)
        cohort, base_dir = load_agent_cohort(cohort_path)
        execution = run_agent_cohort(contract, adapter, cohort, base_dir)
    except DSLValidationError as exc:
        print("COHORT INPUT INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    except AgentExecutionError as exc:
        print(f"COHORT EXECUTION FAILED: {exc}", file=sys.stderr)
        return 2

    expected_paths: list[Path] = []
    for artifact in execution.artifacts:
        run_dir = output_dir / artifact.summary.relative_output_dir
        expected_paths.extend(
            [
                run_dir / "candidate_manifest.json",
                run_dir / "raw_session.yaml",
                run_dir / "trajectory.yaml",
                run_dir / "evaluation.json",
            ]
        )
        if artifact.telemetry_interchange is not None:
            expected_paths.append(run_dir / "telemetry_interchange.json")
        if contract.operational_integrity is not None:
            expected_paths.extend(
                [
                    run_dir / "operational_trace.yaml",
                    run_dir / "operational_evaluation.json",
                ]
            )
    existing = [str(path) for path in expected_paths if path.exists()]
    if existing:
        print("COHORT OUTPUT INVALID", file=sys.stderr)
        print("  - run evidence already exists: " + ", ".join(existing), file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact in execution.artifacts:
        run_dir = output_dir / artifact.summary.relative_output_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        bundle = artifact.bundle
        (run_dir / "candidate_manifest.json").write_text(
            bundle.agent_run.manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (run_dir / "raw_session.yaml").write_text(
            yaml.safe_dump(
                bundle.agent_run.raw_session.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        (run_dir / "trajectory.yaml").write_text(
            yaml.safe_dump(
                bundle.trajectory.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        (run_dir / "evaluation.json").write_text(
            bundle.evaluation.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        if artifact.telemetry_interchange is not None:
            (run_dir / "telemetry_interchange.json").write_text(
                artifact.telemetry_interchange.model_dump_json(
                    indent=2, exclude_none=True
                )
                + "\n",
                encoding="utf-8",
            )
        if contract.operational_integrity is not None:
            (run_dir / "operational_trace.yaml").write_text(
                yaml.safe_dump(
                    bundle.agent_run.operational_trace.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    sort_keys=False,
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            (run_dir / "operational_evaluation.json").write_text(
                bundle.operational_evaluation.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
    report_path.write_text(execution.report.model_dump_json(indent=2) + "\n", encoding="utf-8")

    summary = {
        "dataset_id": execution.report.dataset_id,
        "agents": execution.report.agent_source_count,
        "scenarios": execution.report.scenario_source_count,
        "runs": execution.report.run_count,
        "pass": execution.report.pass_count,
        "fail": execution.report.fail_count,
        "indeterminate": execution.report.indeterminate_count,
        "technical_failures": len(execution.report.technical_failures),
        "output_dir": str(output_dir),
    }
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"COHORT COMPLETE: {summary['agents']} agents × {summary['scenarios']} scenarios "
            f"= {summary['runs']} runs"
        )
        print(
            f"RESULTS: {summary['pass']} pass, {summary['fail']} fail, "
            f"{summary['indeterminate']} indeterminate"
        )
        if summary["technical_failures"]:
            # Named, not counted away: an excluded episode is evidence about the
            # harness and the report has to say which one it was.
            print(f"TECHNICAL FAILURES (excluded): {summary['technical_failures']}")
        print(f"DATASET: {summary['dataset_id']}")
        print(f"WROTE: {output_dir}")
    return 0


def _swarm_keygen(private_key: Path, public_key: Path) -> int:
    try:
        key_id = generate_swarm_keypair(private_key, public_key)
    except (OSError, ValueError) as exc:
        print(f"SWARM KEYGEN FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"GENERATED: Ed25519 key_id={key_id}")
    print(f"  private: {private_key}")
    print(f"  public: {public_key}")
    return 0


def _swarm_verify(
    report_path: Path,
    channel_path: Path,
    public_key: Path,
    evidence_dir: Path,
    *,
    as_json: bool,
    swarm_contract_path: Path | None = None,
) -> int:
    try:
        report = load_swarm_report(report_path)
        channel = load_swarm_channel(channel_path)
        member_paths = load_swarm_member_evaluation_paths(evidence_dir, report)
        verification = verify_swarm_report(
            report,
            channel=channel,
            public_key_path=public_key,
            member_evaluation_paths=member_paths,
            swarm_contract_path=swarm_contract_path,
        )
    except (OSError, ValueError) as exc:
        print(f"SWARM VERIFICATION INVALID: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(verification.model_dump_json(indent=2))
    elif verification.valid:
        print(
            f"SWARM VERIFIED: {report.report_id}; "
            f"{report.channel_entries} channel entries; key={report.signature.key_id}"
        )
    else:
        print(f"SWARM VERIFICATION FAILED: {report.report_id}", file=sys.stderr)
        for problem in verification.problems:
            print(f"  - {problem}", file=sys.stderr)
    return 0 if verification.valid else 1


def _swarm_evaluate(
    contract_path: Path,
    adapter_path: Path,
    swarm_path: Path,
    output_dir: Path,
    *,
    as_json: bool,
    swarm_contract_path: Path | None = None,
    private_key: Path | None = None,
    public_key: Path | None = None,
) -> int:
    report_path = output_dir / "swarm_report.json"
    if (private_key is None) != (public_key is None):
        print("SWARM INPUT INVALID: --private-key and --public-key must be provided together", file=sys.stderr)
        return 2
    output_paths = [
        report_path,
        output_dir / "swarm_channel.json",
        output_dir / "subject" / "trajectory.yaml",
        output_dir / "subject" / "evaluation.json",
        output_dir / "peer" / "trajectory.yaml",
        output_dir / "peer" / "evaluation.json",
    ]
    existing = [str(path) for path in output_paths if path.exists()]
    if existing:
        print("SWARM OUTPUT INVALID: already exists: " + ", ".join(existing), file=sys.stderr)
        return 2
    try:
        contract = load_contract(contract_path)
        adapter = load_adapter_config(adapter_path)
        swarm, base_dir = load_swarm_pair(swarm_path)
        execution = run_swarm_pair(contract, adapter, swarm, base_dir)
        system_verdict = None
        swarm_contract = None
        if swarm_contract_path is not None:
            from autopilot.swarm_contract import load_swarm_contract
            from autopilot.swarm_evaluator import evaluate_swarm_contract

            swarm_contract = load_swarm_contract(swarm_contract_path)
            system_verdict = evaluate_swarm_contract(
                swarm_contract, execution.run, execution.report.members
            )
    except DSLValidationError as exc:
        print("SWARM INPUT INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    except AgentExecutionError as exc:
        print(f"SWARM EXECUTION FAILED: {exc}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    channel_document = json.dumps(
        [
            entry.model_dump(mode="json", exclude_none=private_key is not None)
            for entry in execution.run.channel
        ],
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    member_paths: dict[str, Path] = {}
    for prefix, bundle in (
        ("subject", execution.subject_paired),
        ("peer", execution.peer_paired),
    ):
        member_dir = output_dir / prefix
        member_dir.mkdir(exist_ok=True)
        trajectory_path = member_dir / "trajectory.yaml"
        evaluation_path = member_dir / "evaluation.json"
        trajectory_path.write_text(
            yaml.safe_dump(
                bundle.trajectory.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        evaluation_path.write_text(bundle.evaluation.model_dump_json(indent=2) + "\n", encoding="utf-8")
        member = execution.report.members[0 if prefix == "subject" else 1]
        member_paths[member.source_id] = evaluation_path

    report = execution.report
    if swarm_contract is not None and system_verdict is not None:
        contract_digest = hashlib.sha256(
            _canonical_json_bytes(swarm_contract.model_dump(mode="json"))
        ).hexdigest()
        report = report.model_copy(
            update={
                "swarm_contract_id": swarm_contract.swarm_contract_id,
                "contract_sha256": contract_digest,
                "system_verdict": system_verdict.model_dump(mode="json"),
            }
        )
    if private_key is not None and public_key is not None:
        report = sign_swarm_report(
            report,
            channel=execution.run,
            member_evaluation_paths=member_paths,
            private_key_path=private_key,
            public_key_path=public_key,
        )
    (output_dir / "swarm_report.json").write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "swarm_channel.json").write_text(channel_document, encoding="utf-8")
    summary = {
        "report_id": report.report_id,
        "swarm_id": report.swarm_id,
        "scenario_id": report.scenario_id,
        "channel_entries": report.channel_entries,
        "rounds_completed": report.rounds_completed,
        "delegation_rounds_used": report.delegation_rounds_used,
        "signed": report.signature is not None,
        "system_verdict": report.system_verdict,
        "members": [
            {
                "source_id": member.source_id,
                "isolated_status": member.isolated_status,
                "paired_status": member.paired_status,
                "contagion_clause_ids": member.contagion_clause_ids,
            }
            for member in report.members
        ],
        "contagion_detected": report.contagion_detected,
        "output_dir": str(output_dir),
    }
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"SWARM COMPLETE: {report.swarm_id} "
            f"({report.channel_entries} channel entries, "
            f"{report.delegation_rounds_used} delegation rounds)"
        )
        for member in report.members:
            print(
                f"  {member.source_id}: isolated {member.isolated_status.upper()} -> "
                f"paired {member.paired_status.upper()}"
            )
        if report.contagion_detected:
            print("CONTAGION DETECTED: " + ", ".join(report.contagion_clause_ids))
        else:
            print("CONTAGION: none")
        print(f"REPORT: {report.report_id}; signed={str(report.signature is not None).lower()}")
        print(f"WROTE: {output_dir}")
    return 0


def _social_progress(message: str, *, as_json: bool) -> None:
    """Report progress without ever contaminating a ``--json`` stdout.

    With ``--json`` the whole stdout must stay one parseable JSON document, so
    the per-artifact lines move to stderr; without it they stay in stdout next
    to the tables, which is where an operator reads them.
    """

    print(message, file=sys.stderr if as_json else sys.stdout)


def _social_variants(kind: str, variant: str) -> tuple[str, ...] | None:
    """The variants to run, or ``None`` when --variant does not apply to the kind."""

    available = _SOCIAL_VARIANTS[kind]
    if variant == "both":
        return available
    return (variant,) if variant in available else None


def _social_verify(directory: Path, public_key: Path | None, *, as_json: bool) -> int:
    if public_key is None:
        print(
            "SOCIAL VERIFICATION INVALID: --public-key is required with --verify",
            file=sys.stderr,
        )
        return 2
    result = verify_social_reports(
        directory,
        public_key_path=public_key,
        member_models={
            "bystander": BystanderMember,
            "asch": AschMember,
            "cascade": CascadeMember,
        },
    )
    if as_json:
        print(result.model_dump_json(indent=2))
    elif not result.valid:
        print("SOCIAL VERIFICATION FAILED", file=sys.stderr)
        for problem in result.problems:
            print(f"  - {problem}", file=sys.stderr)
    else:
        print(f"SOCIAL VERIFIED: {result.checked} signed report(s)")
    return 0 if result.valid else 1


def _social_experiment(
    kind: str | None,
    *,
    trials: int,
    sizes: tuple[int, ...] | None,
    seed: str | None,
    variant: str,
    case: str | None,
    output_dir: Path | None,
    private_key: Path | None,
    public_key: Path | None,
    signed_at: str | None,
    verify: Path | None,
    as_json: bool,
) -> int:
    """Run (and optionally sign) the N-agent social experiments, or verify them.

    The signed report binds the payload, the channel digest, the chain head and
    every member record under one Ed25519 signature, so a run is evidence and
    not just output.
    """

    if verify is not None:
        return _social_verify(verify, public_key, as_json=as_json)
    if kind is None:
        print(
            "SOCIAL EXPERIMENT INVALID: a kind (bystander|asch|cascade) is required"
            " unless --verify is used",
            file=sys.stderr,
        )
        return 2
    if trials < 1:
        print("SOCIAL EXPERIMENT INVALID: --trials must be at least 1", file=sys.stderr)
        return 2
    if (private_key is None) != (public_key is None):
        print(
            "SOCIAL EXPERIMENT INVALID: --private-key and --public-key must be provided together",
            file=sys.stderr,
        )
        return 2
    if private_key is not None and output_dir is None:
        print("SOCIAL EXPERIMENT INVALID: signing requires --output-dir", file=sys.stderr)
        return 2
    variants = _social_variants(kind, variant)
    if variants is None:
        print(
            f"SOCIAL EXPERIMENT INVALID: --variant {variant} does not apply to {kind}",
            file=sys.stderr,
        )
        return 2
    if case is not None and kind != "cascade":
        print(
            f"SOCIAL EXPERIMENT INVALID: --case {case} only applies to cascade",
            file=sys.stderr,
        )
        return 2
    # The cascade is run in both directions on demand; the other two experiments
    # have no case to choose, so they get the singleton below.
    if kind != "cascade":
        selected_cases: tuple[bool, ...] = (True,)
    elif case is None:
        selected_cases = (_CASCADE_CASES["requires_review"],)
    elif case == "both":
        selected_cases = tuple(_CASCADE_CASES.values())
    else:
        selected_cases = (_CASCADE_CASES[case],)

    selected_sizes = sizes or _DEFAULT_SOCIAL_SIZES
    payloads: list[dict[str, object]] = []

    # One entry per run actually requested, so the cascade's extra axis does not
    # turn the run/write/sign tail into a copy per branch.
    configurations: list[tuple] = []
    for selected in variants:
        if kind == "bystander":
            options: dict[str, object] = {"mechanism": selected}
            if seed is not None:
                options["seed"] = seed
            configurations.append(
                (
                    BystanderDesign(**options),
                    run_bystander_sweep,
                    format_bystander_sweep,
                    write_bystander_sweep,
                    sign_bystander_sweep_samples,
                )
            )
        elif kind == "asch":
            options = {"dissenter": selected == "con_disidente"}
            if seed is not None:
                options["seed"] = seed
            configurations.append(
                (
                    AschDesign(**options),
                    run_asch_sweep,
                    format_asch_sweep,
                    write_asch_sweep,
                    sign_asch_sweep_samples,
                )
            )
        else:
            # The variant is a signal-accuracy regime and the case is the
            # direction: one knob sets how much the group caps out at, the other
            # whether the failure of interest is a missed or a spurious escalation.
            for requires_review in selected_cases:
                options = {
                    "signal_accuracy": _CASCADE_VARIANTS[selected],
                    "case_requires_review": requires_review,
                }
                if seed is not None:
                    options["seed"] = seed
                configurations.append(
                    (
                        CascadeDesign(**options),
                        run_cascade_sweep,
                        format_cascade_sweep,
                        write_cascade_sweep,
                        sign_cascade_sweep_samples,
                    )
                )

    for design, run_sweep, render, write_sweep, sign_samples in configurations:
        sweep = run_sweep(design, selected_sizes, trials)
        payloads.append(sweep.model_dump(mode="json"))
        if not as_json:
            print(render(sweep))
        if output_dir is None:
            continue
        try:
            written = write_sweep(sweep, output_dir)
            if private_key is not None and public_key is not None:
                for report in sign_samples(
                    sweep,
                    design,
                    output_dir,
                    private_key_path=private_key,
                    public_key_path=public_key,
                    signed_at=signed_at,
                ):
                    _social_progress(f"SIGNED: {report.report_id}", as_json=as_json)
        except (OSError, ValueError) as exc:
            print(f"SOCIAL EXPERIMENT FAILED: {exc}", file=sys.stderr)
            return 2
        _social_progress(f"WROTE: {written}", as_json=as_json)
    if as_json:
        # One JSON array, whatever the number of variants: a caller can always
        # ``json.loads`` the whole stdout.
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
    return 0


def _boundary_run(
    contract_path: Path,
    plan_path: Path,
    private_key: Path,
    output_dir: Path,
    *,
    payment_authorization_public_key: Path | None,
    timeout_seconds: float,
    backend: str,
    docker_image: str | None,
    docker_memory_mb: int,
    docker_cpus: float,
    docker_pids_limit: int,
    docker_tmpfs_mb: int,
    as_json: bool,
) -> int:
    outputs = {
        "report": output_dir / "boundary_report.json",
        "trace": output_dir / "operational_trace.yaml",
        "evaluation": output_dir / "operational_evaluation.json",
    }
    existing = [str(path) for path in outputs.values() if path.exists()]
    if existing:
        print("BOUNDARY OUTPUT INVALID", file=sys.stderr)
        print("  - output files already exist: " + ", ".join(existing), file=sys.stderr)
        return 2
    if timeout_seconds <= 0 or timeout_seconds > 300:
        print("BOUNDARY INPUT INVALID", file=sys.stderr)
        print("  - timeout_seconds must be greater than 0 and at most 300", file=sys.stderr)
        return 2
    selected_backend = BoundaryBackend(backend)
    if selected_backend is BoundaryBackend.docker and docker_image is None:
        print("BOUNDARY INPUT INVALID", file=sys.stderr)
        print("  - --docker-image is required for --backend docker", file=sys.stderr)
        return 2
    if selected_backend is BoundaryBackend.python_process and docker_image is not None:
        print("BOUNDARY INPUT INVALID", file=sys.stderr)
        print("  - --docker-image requires --backend docker", file=sys.stderr)
        return 2
    try:
        docker_config = (
            DockerBoundaryConfig(
                image=docker_image,
                memory_mb=docker_memory_mb,
                cpus=docker_cpus,
                pids_limit=docker_pids_limit,
                tmpfs_mb=docker_tmpfs_mb,
            )
            if selected_backend is BoundaryBackend.docker
            else None
        )
        contract = load_contract(contract_path)
        plan = load_boundary_plan(plan_path)
        report = run_execution_boundary(
            contract,
            plan,
            private_key,
            timeout_seconds=timeout_seconds,
            backend=selected_backend,
            docker_config=docker_config,
            payment_authorization_public_key_path=payment_authorization_public_key,
        )
    except ValidationError as exc:
        print("BOUNDARY INPUT INVALID", file=sys.stderr)
        for issue in exc.errors(include_url=False):
            location = ".".join(str(item) for item in issue["loc"])
            print(f"  - docker.{location}: {issue['msg']}", file=sys.stderr)
        return 2
    except DSLValidationError as exc:
        print("BOUNDARY INPUT INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs["report"].write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    outputs["trace"].write_text(
        yaml.safe_dump(
            report.operational_trace.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    outputs["evaluation"].write_text(
        report.operational_evaluation.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "report_id": report.report_id,
        "execution_id": report.execution_id,
        "backend": report.boundary_name,
        "status": report.status.value,
        "operational_status": report.operational_evaluation.status.value,
        "terminated": report.terminated,
        "termination_reason": report.termination_reason,
        "executed_requests": report.executed_request_count,
        "blocked_requests": report.blocked_request_count,
        "chain_head": report.chain_head,
        "signing_key_id": report.signature.key_id,
        "output_dir": str(output_dir),
    }
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"BOUNDARY {report.status.value.upper()}: {report.execution_id}")
        print(
            f"EXECUTED: {report.executed_request_count}; "
            f"BLOCKED: {report.blocked_request_count}; terminated={report.terminated}"
        )
        print(f"REPORT: {report.report_id}; key={report.signature.key_id}")
        print(f"WROTE: {output_dir}")
    return 0 if report.status is BoundaryStatus.passed else 1


def _boundary_verify(report_path: Path, public_key: Path, *, as_json: bool) -> int:
    try:
        report = load_boundary_report(report_path)
        verification = verify_boundary_report(report, public_key)
    except DSLValidationError as exc:
        print("BOUNDARY VERIFICATION INVALID", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        return 2
    if as_json:
        print(verification.model_dump_json(indent=2))
    else:
        print(
            f"BOUNDARY SIGNATURE {'VALID' if verification.valid else 'INVALID'}: "
            f"{report.report_id}"
        )
        for problem in verification.problems:
            print(f"  - {problem}")
    return 0 if verification.valid else 1


def _payment_authorize(
    private_key: Path,
    *,
    authorization_id: str,
    execution_id: str,
    contract_id: str,
    candidate_version: str,
    operation_id: str,
    amount_eur: str,
    beneficiary_id: str,
    approved_by: str,
    ttl_seconds: float,
    output: Path | None,
    as_json: bool,
) -> int:
    """Sign one payment authorization receipt the execution boundary verifies."""

    from datetime import datetime, timedelta, timezone
    from decimal import Decimal, InvalidOperation

    from autopilot.execution_boundary import (
        PaymentAuthorizationPayload,
        sign_payment_authorization,
    )

    try:
        amount = Decimal(amount_eur)
    except (InvalidOperation, ValueError):
        print(
            "PAYMENT AUTHORIZATION INVALID: amount_eur is not a decimal number",
            file=sys.stderr,
        )
        return 2
    amount_minor = amount * 100
    if not amount.is_finite() or amount <= 0:
        print(
            "PAYMENT AUTHORIZATION INVALID: amount_eur must be positive",
            file=sys.stderr,
        )
        return 2
    if amount_minor != amount_minor.to_integral_value():
        print(
            "PAYMENT AUTHORIZATION INVALID: amount_eur has more than two decimals",
            file=sys.stderr,
        )
        return 2
    if ttl_seconds <= 0:
        print(
            "PAYMENT AUTHORIZATION INVALID: ttl_seconds must be greater than 0",
            file=sys.stderr,
        )
        return 2

    now = datetime.now(timezone.utc)
    try:
        payload = PaymentAuthorizationPayload(
            authorization_id=authorization_id,
            execution_id=execution_id,
            contract_id=contract_id,
            candidate_version=candidate_version,
            operation_id=operation_id,
            operation="claim_payment",
            amount_minor=int(amount_minor),
            currency="EUR",
            beneficiary_id=beneficiary_id,
            approved_by=approved_by,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        receipt = sign_payment_authorization(payload, private_key)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"PAYMENT AUTHORIZATION INVALID: {exc}", file=sys.stderr)
        return 2

    document = json.dumps(receipt.model_dump(mode="json"), indent=2) + "\n"
    if output is not None:
        output.write_text(document, encoding="utf-8")
    if as_json or output is None:
        sys.stdout.write(document)
    else:
        print(
            f"PAYMENT AUTHORIZATION: {payload.authorization_id}; "
            f"operation_id={payload.operation_id}; "
            f"amount_minor={payload.amount_minor}; "
            f"expires_at={payload.expires_at}"
        )
    return 0


def _load_evaluation_for_guard(evaluation_path: Path) -> dict:
    """Load an evaluation JSON or trajectory YAML for guard screening.

    Evaluation JSON must carry ``recommended_autonomy`` and
    ``block_release``. A YAML mapping with ``observations`` is recognized as
    a trajectory and rejected with guidance to evaluate it first.
    """

    suffix = evaluation_path.suffix.lower()
    with evaluation_path.open("r", encoding="utf-8") as handle:
        if suffix == ".json":
            payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError(f"{evaluation_path} must contain a JSON object")
            for field in ("recommended_autonomy", "block_release"):
                if field not in payload:
                    raise ValueError(
                        f"{evaluation_path} is missing required field '{field}'"
                    )
            return payload
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{evaluation_path} must contain a mapping")
    if "recommended_autonomy" in payload and "block_release" in payload:
        return payload
    if "observations" in payload:
        raise _GuardNeedsContract()
    raise ValueError(
        f"{evaluation_path} must be an evaluation JSON or a trajectory YAML"
    )


class _GuardNeedsContract(Exception):
    """Internal signal: the input is a trajectory and needs its contract."""


def _guard(
    evaluation_path: Path,
    policy_path: Path,
    *,
    tools: list[str],
    sandbox_evidence: bool,
    ledger: Path | None,
    private_key: Path | None,
    public_key: Path | None,
    as_json: bool,
) -> int:
    """Screen tool calls at the posture derived from an Integrity evaluation."""

    try:
        policy = load_tool_policy(policy_path)
        payload = _load_evaluation_for_guard(evaluation_path)
    except ToolPolicyFormatError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"TOOL POLICY INVALID: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"EVALUATION INPUT INVALID: {exc}", file=sys.stderr)
        return 2
    except _GuardNeedsContract:
        # Trajectory inputs are supported through ``evaluate`` first; keep the
        # surface honest and require the evaluation JSON explicitly.
        message = (
            "trajectory YAML detected; evaluate it first (behavioural-autopilot "
            "evaluate ...) and pass the evaluation JSON to guard"
        )
        if as_json:
            print(json.dumps({"error": message}, indent=2, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 2

    autonomy_value = payload["recommended_autonomy"]
    block_release = bool(payload["block_release"])
    adapter = EnforcementAdapter.from_evaluation(
        _GuardRecommendation(autonomy_value, block_release), policy
    )

    def _decision_payload(decision):
        return {
            "tool": decision.tool,
            "allowed": decision.allowed,
            "requires_approval": decision.requires_approval,
            "reason": decision.reason,
        }

    if tools:
        tool_decisions = [adapter.decide(tool) for tool in tools]
    else:
        declared = sorted(policy.sensitive_tools | policy.allowlisted_tools)
        tool_decisions = [adapter.decide(tool) for tool in declared]
    decisions = [_decision_payload(decision) for decision in tool_decisions]

    receipt = None
    if ledger is not None:
        if private_key is None or public_key is None:
            message = "--record-ledger requires --private-key and --public-key"
            if as_json:
                print(json.dumps({"error": message}, indent=2, ensure_ascii=False))
            else:
                print(message, file=sys.stderr)
            return 2
        payload = build_guard_decision_payload(
            adapter=adapter,
            evaluation_source=str(evaluation_path),
            policy_source=str(policy_path),
            decisions=tool_decisions,
            block_release=block_release,
            sandbox_evidence_declared=sandbox_evidence,
        )
        try:
            receipt = sign_guard_decision(payload, private_key, public_key)
            append_result = record_guard_decision(AuditLedger(ledger), receipt)
        except (ValueError, OSError) as exc:
            if as_json:
                print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
            else:
                print(f"GUARD LEDGER ERROR: {exc}", file=sys.stderr)
            return 2

    denied = [item for item in decisions if not item["allowed"]]
    result = {
        "guard": "behavioural-autopilot guard",
        "evaluation": str(evaluation_path),
        "policy": str(policy_path),
        "recommended_autonomy": adapter.autonomy.value,
        "posture": adapter.posture.value,
        "block_release": block_release,
        "sandbox_evidence_declared": sandbox_evidence,
        "decisions": decisions,
        "denied_count": len(denied),
    }
    if receipt is not None:
        result["audit"] = {
            "ledger": str(ledger),
            "decision_id": receipt.payload.decision_id,
            "signature_key_id": receipt.signature.key_id,
            "appended": append_result.inserted,
        }
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"POSTURE: {result['posture'].upper()} "
            f"(autonomy={result['recommended_autonomy']}; "
            f"block_release={str(block_release).lower()})"
        )
        for item in decisions:
            state = "ALLOW" if item["allowed"] else (
                "APPROVAL REQUIRED" if item["requires_approval"] else "DENY"
            )
            print(f"  {state:17} {item['tool']}: {item['reason']}")
        if receipt is not None:
            state = "APPENDED" if append_result.inserted else "ALREADY RECORDED"
            print(f"  LEDGER {state:9} {receipt.payload.decision_id}")
    return 0 if not denied else 1


class _GuardRecommendation:
    """Duck-typed stand-in exposing an evaluation recommendation to the adapter."""

    def __init__(self, recommended_autonomy: str, block_release: bool) -> None:
        self.recommended_autonomy = recommended_autonomy
        self.block_release = block_release


def _guard_sign_recovery(
    target_snapshot_id: str,
    target_autonomy: str,
    recovery_run_id: str,
    private_key: Path,
    public_key: Path,
    *,
    ttl_seconds: float = 3600.0,
    output: Path | None = None,
    as_json: bool = False,
) -> int:
    """Sign a recovery claim so a guard may relax exactly one autonomy level."""

    from autopilot.enforcement import (
        build_recovery_evidence,
        sign_recovery_evidence,
    )

    try:
        payload = build_recovery_evidence(
            target_snapshot_id=target_snapshot_id,
            target_autonomy=target_autonomy,
            recovery_run_id=recovery_run_id,
            ttl_seconds=ttl_seconds,
        )
        receipt = sign_recovery_evidence(payload, private_key, public_key)
    except (OSError, ValueError) as exc:
        print(f"RECOVERY EVIDENCE INVALID: {exc}", file=sys.stderr)
        return 2

    document = json.dumps(receipt.model_dump(mode="json"), indent=2) + "\n"
    if output is not None:
        output.write_text(document, encoding="utf-8")
    if as_json or output is None:
        sys.stdout.write(document)
    else:
        print(
            f"RECOVERY EVIDENCE: {payload.evidence_id}; "
            f"target_snapshot_id={payload.target_snapshot_id}; "
            f"target_autonomy={payload.target_autonomy.value}; "
            f"expires_at={payload.expires_at}"
        )
    return 0


def _guard_sign_evaluation(
    evaluation_path: Path,
    private_key: Path,
    public_key: Path,
    *,
    ttl_seconds: float = 900.0,
    output: Path | None = None,
    as_json: bool = False,
) -> int:
    """Wrap an evaluation in a signed, time-bounded snapshot for the guard."""

    from autopilot.enforcement import (
        build_evaluation_snapshot,
        sign_evaluation_snapshot,
    )
    from autopilot.guard_server import load_evaluation_document

    try:
        payload = load_evaluation_document(evaluation_path)
        snapshot = build_evaluation_snapshot(
            evaluation_source=str(evaluation_path),
            recommended_autonomy=payload["recommended_autonomy"],
            block_release=payload["block_release"],
            sandbox_evidence_declared=payload.get("sandbox_evidence") is True,
            ttl_seconds=ttl_seconds,
        )
        receipt = sign_evaluation_snapshot(snapshot, private_key, public_key)
    except (OSError, ValueError) as exc:
        print(f"EVALUATION SNAPSHOT INVALID: {exc}", file=sys.stderr)
        return 2

    document = json.dumps(receipt.model_dump(mode="json"), indent=2) + "\n"
    if output is not None:
        output.write_text(document, encoding="utf-8")
    if as_json or output is None:
        sys.stdout.write(document)
    else:
        print(
            f"SNAPSHOT: {snapshot.snapshot_id}; "
            f"autonomy={snapshot.recommended_autonomy.value}; "
            f"expires_at={snapshot.expires_at}"
        )
    return 0


def _guard_server(
    evaluation_path: Path,
    policy_path: Path,
    *,
    ledger: Path | None,
    private_key: Path | None,
    public_key: Path | None,
    evaluation_public_key: Path | None = None,
    recovery_public_key: Path | None = None,
    recovery_evidence: Path | None = None,
    bootstrap_autonomy_from_evaluation: bool = False,
    stdin=None,
    stdout=None,
) -> int:
    """Run the MCP-style pre-tool-call guard server over line-delimited stdio."""

    try:
        server = GuardServer(
            policy_path,
            evaluation_path,
            ledger_path=ledger,
            private_key_path=private_key,
            public_key_path=public_key,
            evaluation_public_key_path=evaluation_public_key,
            recovery_public_key_path=recovery_public_key,
            recovery_evidence_path=recovery_evidence,
            bootstrap_autonomy_from_evaluation=bootstrap_autonomy_from_evaluation,
        )
    except ToolPolicyFormatError as exc:
        print(f"TOOL POLICY INVALID: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"GUARD SERVER INPUT INVALID: {exc}", file=sys.stderr)
        return 2
    return serve_guard_server(server, stdin=stdin, stdout=stdout)


def _enforce_envelope(
    ledger_path: Path,
    contract_path: Path,
    target_clause_id: str,
    current_envelope_usage: int,
    now: str,
    review_path: Path | None,
    review_id: str,
    reference: str,
    *,
    output: Path | None,
    as_json: bool,
    public_keys: list[str] | None = None,
) -> int:
    """B2 envelope enforcement + idempotent ``release_denied`` audit.

    Companion CLI surface for
    :func:`autopilot.release_review.enforce_envelope_and_audit`. Captures the
    drift-guard ``DSLValidationError`` (raised when an attached
    ``delegation_chain.clause_id`` does not match the requested
    ``--target-clause-id``) and the contract / envelope / usage
    ``DSLValidationError`` raised by :func:`load_contract` and
    :func:`load_release_review`, returning exit ``2`` with structured
    issues rather than propagating a raw traceback. This is the B2
    carry-over (E) closure: every CLI subcommand that touches
    ``enforce_envelope_and_audit`` MUST funnel this exception through the
    same shape, so conductors get a uniform ``{"valid": false, "issues":
    [...]}`` JSON surface (text mode mirrors the same content on stderr).

    B2 carry-over (C) — trust-boundary closure: ``--public-keys`` (when
    the review carries a ``delegation_chain``) is REQUIRED. The parsed
    ``Mapping[actor, path]`` is forwarded to ``enforce_envelope_and_audit``
    which defensively calls ``verify_release_review`` so a forged chain
    cannot bypass the envelope ``auto_veto`` pathway.

    Exit codes:
    * ``0`` for in-envelope approve, chain-superseded approve, or
      ``manual_review`` (the conductor must decide explicitly).
    * ``1`` for ``auto_veto`` (one ``release_denied`` event appended unless
      a previous call with the same ``--review-id`` already wrote it).
    * ``2`` for any input-validity failure (``DSLValidationError``).
    """

    raw_public_keys = public_keys if public_keys is not None else []
    try:
        contract = load_contract(contract_path)
        review = load_release_review(review_path) if review_path is not None else None
        verifying_public_keys: dict[str, Path] | None
        verifying_public_keys = (
            _parse_public_keys(raw_public_keys) if raw_public_keys else None
        )
        result, attempted = enforce_envelope_and_audit(
            AuditLedger(ledger_path),
            contract=contract,
            target_clause_id=target_clause_id,
            current_envelope_usage=current_envelope_usage,
            now=now,
            review_id=review_id,
            reference=reference,
            review=review,
            verifying_public_keys=verifying_public_keys,
        )
    except DSLValidationError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "issues": [
                            {"path": issue.path, "message": issue.message}
                            for issue in exc.issues
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("ENVELOPE INPUT INVALID", file=sys.stderr)
            for issue in exc.issues:
                print(f"  - {issue}", file=sys.stderr)
        return 2

    payload = result.model_dump(mode="json")
    payload["review_id"] = review_id
    payload["reference"] = reference
    if attempted is not None:
        payload["audit_event"] = {
            "sequence": attempted.event.sequence,
            "event_type": attempted.event.event_type.value,
            "event_id": attempted.event.event_id,
            "inserted": attempted.inserted,
        }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")

    if as_json:
        print(rendered)
    else:
        if result.chain_override_active:
            headline = "OUT-OF-ENVELOPE (chain override active)"
        elif result.in_envelope:
            headline = "IN-ENVELOPE"
        elif result.recommended_decision == "manual_review":
            headline = "MANUAL-REVIEW (no envelope declared)"
        else:
            headline = f"OUT-OF-ENVELOPE ({result.recommended_decision})"
        print(
            f"ENVELOPE {headline}: decision={result.recommended_decision} "
            f"target={target_clause_id} review_id={review_id}"
        )
        for problem in result.problems:
            print(f"  - {problem}")
        if attempted is not None:
            verb = "APPENDED" if attempted.inserted else "EXISTS"
            print(
                f"AUDIT {verb}: #{attempted.event.sequence} "
                f"{attempted.event.event_type.value} {attempted.event.event_id}"
            )
        else:
            print("AUDIT: no event written")

    if result.recommended_decision == "auto_veto":
        return 1
    return 0


def _ai_act_readiness(
    profile_path: Path,
    obligations_path: Path,
    evaluation_paths: list[Path],
    output_dir: Path,
    *,
    as_json: bool,
) -> int:
    try:
        profile = load_system_profile(profile_path)
        obligations = load_obligation_map(obligations_path)
        evaluations = load_evaluations(evaluation_paths)
        report = build_longitudinal_report(profile, obligations, evaluations)
        outputs = write_report_bundle(report, output_dir)
    except (DSLValidationError, ValidationError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    payload = {
        "report_id": report.report_id,
        "system_id": report.system_id,
        "current_version": report.current_version,
        "current_autonomy": report.current_autonomy.value,
        "current_drift_index": report.current_drift_index,
        "current_coverage_percent": report.current_coverage_percent,
        "evaluation_count": len(evaluations),
        "outputs": outputs,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"AI ACT READINESS: {report.system_name} {report.current_version}; "
            f"autonomy={report.current_autonomy.value}; "
            f"drift={report.current_drift_index}/100; "
            f"coverage={report.current_coverage_percent}%"
        )
        print(f"DASHBOARD: {outputs['dashboard']}")
    return 0


def _ai_act_ingest(
    profile_path: Path,
    obligations_path: Path,
    evaluation_paths: list[Path],
    database: Path,
    *,
    as_json: bool,
) -> int:
    try:
        profile = load_system_profile(profile_path)
        obligations = load_obligation_map(obligations_path)
        evaluations = load_evaluations(evaluation_paths)
        summary = AIActLongitudinalStore(database).ingest(
            profile,
            obligations,
            evaluations,
        )
    except (ValidationError, ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(summary.model_dump_json(indent=2))
    else:
        print(
            f"AI ACT INGEST: {summary.system_id}; "
            f"inserted={summary.evaluations_inserted}; "
            f"duplicates={summary.evaluation_duplicates}; "
            f"profile_changed={str(summary.profile_changed).lower()}"
        )
    return 0


def _ai_act_report(
    database: Path,
    system_id: str,
    output_dir: Path,
    *,
    as_json: bool,
) -> int:
    try:
        snapshot = AIActLongitudinalStore(database).load_snapshot(system_id)
        report = build_longitudinal_report(
            snapshot.profile,
            snapshot.obligation_map,
            snapshot.evaluations,
        )
        outputs = write_report_bundle(report, output_dir)
    except (
        KeyError,
        ValidationError,
        ValueError,
        OSError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = {
        "report_id": report.report_id,
        "system_id": report.system_id,
        "evaluation_count": len(snapshot.evaluations),
        "current_version": report.current_version,
        "current_autonomy": report.current_autonomy.value,
        "current_drift_index": report.current_drift_index,
        "current_coverage_percent": report.current_coverage_percent,
        "outputs": outputs,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"AI ACT REPORT: {report.system_name} {report.current_version}; "
            f"evaluations={len(snapshot.evaluations)}; "
            f"autonomy={report.current_autonomy.value}; "
            f"drift={report.current_drift_index}/100"
        )
        print(f"DASHBOARD: {outputs['dashboard']}")
    return 0


def _ai_act_systems(database: Path, *, as_json: bool) -> int:
    try:
        systems = AIActLongitudinalStore(database).list_systems()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in systems],
                indent=2,
                ensure_ascii=False,
            )
        )
    elif not systems:
        print("AI ACT SYSTEMS: empty store")
    else:
        for item in systems:
            print(
                f"{item.system_id}: {item.name}; version={item.current_version}; "
                f"evaluations={item.evaluation_count}; "
                f"last_evaluated_at={item.last_evaluated_at or '-'}"
            )
    return 0


def _ai_act_cycle_keygen(private_key: Path, public_key: Path) -> int:
    try:
        key_id = generate_cycle_keypair(private_key, public_key)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"AI ACT CYCLE KEY: {key_id}")
    return 0


def _ai_act_cycle(
    profile_path: Path,
    obligations_path: Path,
    evaluation_paths: list[Path],
    database: Path,
    output_dir: Path,
    audit_ledger: Path,
    private_key: Path,
    public_key: Path,
    *,
    ran_at: str | None,
    as_json: bool,
) -> int:
    try:
        profile = load_system_profile(profile_path)
        obligations = load_obligation_map(obligations_path)
        result = run_monitor_cycle(
            store=AIActLongitudinalStore(database),
            profile=profile,
            obligation_map=obligations,
            evaluation_paths=evaluation_paths,
            output_dir=output_dir,
            audit_ledger_path=audit_ledger,
            private_key_path=private_key,
            public_key_path=public_key,
            ran_at=ran_at,
        )
    except (
        DSLValidationError,
        ValidationError,
        ValueError,
        OSError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = result.receipt.payload
    summary = {
        "cycle_id": payload.cycle_id,
        "system_id": payload.system_id,
        "change_kind": payload.change_kind.value,
        "state_changed": payload.state_changed,
        "evaluations_inserted": payload.ingestion.evaluations_inserted,
        "evaluation_duplicates": payload.ingestion.evaluation_duplicates,
        "alerts_emitted": len(payload.emitted_alerts),
        "report_written": payload.report_written,
        "audit_inserted": result.audit_inserted,
        "cycle_registered": result.cycle_registered,
        "receipt": str(output_dir / "cycle_receipt.json"),
    }
    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"AI ACT CYCLE: {payload.change_kind.value}; "
            f"inserted={payload.ingestion.evaluations_inserted}; "
            f"alerts={len(payload.emitted_alerts)}; "
            f"audit={'appended' if result.audit_inserted else 'exists'}"
        )
        print(f"RECEIPT: {summary['receipt']}")
    return 0


def _ai_act_cycle_verify(receipt_path: Path, *, as_json: bool) -> int:
    try:
        receipt = SignedMonitorCycleReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
        verification = verify_cycle_receipt(receipt)
    except (ValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(verification.model_dump_json(indent=2))
    else:
        print(
            f"AI ACT CYCLE RECEIPT: {'VALID' if verification.valid else 'INVALID'}; "
            f"key={receipt.signature.key_id}"
        )
        for problem in verification.problems:
            print(f"  - {problem}")
    return 0 if verification.valid else 1


def _discover_leads(
    input_path: Path,
    *,
    output: Path | None,
    min_score: int,
    as_json: bool,
) -> int:
    try:
        report = discover_leads(load_search_results(input_path), min_score=min_score)
        if output is not None:
            write_crm_csv(report.leads, output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(report.to_json())
    else:
        print(
            f"LEAD DISCOVERY: qualified={len(report.leads)}; "
            f"rejected={len(report.rejected)}; min_score={min_score}"
        )
        for lead in report.leads[:10]:
            print(f"  - {lead.account}: {lead.score.total}/12; {lead.segment}")
        if output is not None:
            print(f"CRM CSV: {output}")
    return 0


def _lead_radar(
    config_path: Path,
    *,
    raw_output: Path | None,
    crm_output: Path | None,
    min_score: int | None,
    as_json: bool,
) -> int:
    try:
        report = run_lead_radar(load_radar_config(config_path), min_score=min_score)
        if raw_output is not None:
            write_raw_results_csv(report.raw_results, raw_output)
        if crm_output is not None:
            write_crm_csv(report.discovery.leads, crm_output)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(report.to_json())
    else:
        print(
            f"LEAD RADAR: raw={len(report.raw_results)}; "
            f"qualified={len(report.discovery.leads)}; "
            f"rejected={len(report.discovery.rejected)}"
        )
        for lead in report.discovery.leads[:10]:
            print(f"  - {lead.account}: {lead.score.total}/12; {lead.segment}")
        if raw_output is not None:
            print(f"RAW CSV: {raw_output}")
        if crm_output is not None:
            print(f"CRM CSV: {crm_output}")
    return 0


def _clause_coverage(
    paths: Sequence[Path],
    contracts: Sequence[Path],
    *,
    audit_csv: Path | None,
    require_all_triggered: bool,
    require_all_failed: bool,
    cards_dir: Path | None,
    cards_md: Path | None,
    as_json: bool,
) -> int:
    if audit_csv is not None and paths:
        print(
            "ERROR: --audit-csv reads a recorded snapshot; drop the artifact paths",
            file=sys.stderr,
        )
        return 2
    if audit_csv is None and not paths:
        print(
            "ERROR: give evaluation artifacts or --audit-csv",
            file=sys.stderr,
        )
        return 2
    try:
        if audit_csv is not None:
            report = load_audit_csv(audit_csv)
            gate = gate_satisfied(
                never_triggered=report.never_triggered,
                never_failed=report.never_failed,
                expected_without_artifact=report.expected_without_artifact,
                require_all_triggered=require_all_triggered,
                require_all_failed=require_all_failed,
            )
            report = report.model_copy(update={"gate_passed": gate})
        else:
            declared = declared_clause_ids([load_contract(path) for path in contracts])
            report = collect_clause_coverage(
                paths,
                declared=declared,
                require_all_triggered=require_all_triggered,
                require_all_failed=require_all_failed,
            )
    except (ClauseCoverageInputError, DSLValidationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        sys.stdout.write(render_clause_coverage(report))
    if cards_dir is not None:
        written = write_detector_cards(report, cards_dir)
        if not as_json:
            print(f"DETECTOR CARDS: {len(written)} written to {cards_dir}")
    if cards_md is not None:
        cards_md.parent.mkdir(parents=True, exist_ok=True)
        cards_md.write_text(render_detector_cards_document(report), encoding="utf-8")
        if not as_json:
            print(f"DETECTOR CARDS DOCUMENT: {cards_md}")
    # A gate is only meaningful when one was asked for; otherwise the ledger is
    # a report and a missing clause is information, not a failure.
    if require_all_triggered or require_all_failed:
        return 0 if report.gate_passed else 1
    return 0


def _make_streams_writable() -> None:
    """Keep usage and error output alive on legacy charmap consoles.

    Windows installs in Spanish and other European locales default to a code
    page such as cp1252, which cannot encode every character the help text
    uses. Without an explicit error policy the very first command a user runs
    (``--help``) raises ``UnicodeEncodeError`` instead of printing usage. The
    declared encoding is kept; only unrepresentable characters degrade.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # captured stream (pytest, notebooks): nothing to do
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - non-reconfigurable stream
            continue


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""

    _make_streams_writable()
    args = _parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.path, as_json=args.as_json, normalized=args.normalized)
    if args.command == "evaluate":
        return _evaluate(
            args.contract,
            args.trajectory,
            as_json=args.as_json,
            trajectory_trust_key=args.trajectory_trust_key,
            trajectory_envelope=args.trajectory_envelope,
        )
    if args.command == "trajectory-sign":
        return _trajectory_sign(
            args.trajectory,
            args.private_key,
            args.public_key,
            producer=args.producer,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "system1-interchange":
        return _system1_interchange(
            args.export,
            args.contract,
            produced_at=args.produced_at,
            heartbeat_at=args.heartbeat_at,
            candidate_version=args.candidate_version,
            producer_private_key=args.producer_private_key,
            producer_public_key=args.producer_public_key,
            trust_key=args.trust_key,
            output=args.output,
            ledger_path=args.ledger,
            produced_by=args.produced_by,
            emitted_at=args.emitted_at,
            as_json=args.as_json,
        )
    if args.command == "system1-sensor":
        return _system1_sensor(
            lambda: load_harness_event_window(args.event_window),
            args.contract,
            candidate_version=args.candidate_version,
            producer_private_key=args.producer_private_key,
            producer_public_key=args.producer_public_key,
            trust_key=args.trust_key,
            output=args.output,
            ledger_path=args.ledger,
            produced_by=args.produced_by,
            emitted_at=args.emitted_at,
            as_json=args.as_json,
        )
    if args.command == "monitor-loop":
        return _monitor_loop(
            args.trajectory,
            args.contract,
            producer_private_key=args.producer_private_key,
            producer_public_key=args.producer_public_key,
            trust_key=args.trust_key,
            output_dir=args.output_dir,
            sensor_trust_key=args.sensor_trust_key,
            interval_seconds=args.interval_seconds,
            max_cycles=args.max_cycles,
            snapshot_ttl_seconds=args.snapshot_ttl_seconds,
            produced_by=args.produced_by,
            previous_cycle=args.previous_cycle,
            ledger=args.ledger,
            as_json=args.as_json,
            drift_watch=args.drift_watch,
            drift_baseline=args.drift_baseline,
            drift_authority_private_key=args.drift_authority_private_key,
            drift_authority_public_key=args.drift_authority_public_key,
            drift_learning_cycles=args.drift_learning_cycles,
            drift_window_cycles=args.drift_window_cycles,
            drift_respond=args.drift_respond,
            drift_budget_max_runs=args.drift_budget_max_runs,
            drift_budget_window_seconds=args.drift_budget_window_seconds,
            health=args.health,
        )
    if args.command == "sensor-health":
        return _sensor_health(
            args.output_dir,
            ledger=args.ledger,
            trust_key=args.trust_key,
            checked_at=args.checked_at,
            as_json=args.as_json,
        )
    if args.command == "drift-watch":
        return _drift_watch(
            args.history_dir,
            authority_private_key=args.authority_private_key,
            authority_public_key=args.authority_public_key,
            trust_key=args.trust_key,
            baseline=args.baseline,
            learning_cycles=args.learning_cycles,
            window_cycles=args.window_cycles,
            latest_baseline_id=args.latest_baseline_id,
            ledger=args.ledger,
            produced_by=args.produced_by,
            as_json=args.as_json,
        )
    if args.command == "drift-baseline":
        return _drift_baseline(
            args.window_dir,
            private_key=args.private_key,
            public_key=args.public_key,
            previous_path=args.previous,
            approved_by=args.approved_by,
            produced_by=args.produced_by,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "drift-check":
        return _drift_check(
            args.baseline,
            args.window_dir,
            trust_key=args.trust_key,
            latest_baseline_id=args.latest_baseline_id,
            ledger=args.ledger,
            max_status_shift=args.max_status_shift,
            max_autonomy_shift=args.max_autonomy_shift,
            max_change_shift=args.max_change_shift,
            ignore_new_categories=args.ignore_new_categories,
            produced_by=args.produced_by,
            as_json=args.as_json,
        )
    if args.command == "drift-respond":
        return _drift_respond(
            args.signal,
            args.trajectory,
            args.contract,
            producer_private_key=args.producer_private_key,
            producer_public_key=args.producer_public_key,
            trust_key=args.trust_key,
            output_dir=args.output_dir,
            previous_cycle=args.previous_cycle,
            ledger=args.ledger,
            produced_by=args.produced_by,
            as_json=args.as_json,
            budget_max_runs=args.budget_max_runs,
            budget_window_seconds=args.budget_window_seconds,
            sensor_trust_key=args.sensor_trust_key,
        )
    if args.command == "adapt":
        return _adapt(
            args.contract,
            args.adapter,
            args.session,
            as_json=args.as_json,
            output=args.output,
        )
    if args.command == "incidents":
        return _incidents(
            args.contract,
            args.trajectory,
            database=args.db,
            as_json=args.as_json,
        )
    if args.command == "diagnose":
        return _diagnose(
            args.contract,
            args.trajectory,
            output_dir=args.output_dir,
            as_json=args.as_json,
        )
    if args.command == "sandbox":
        return _sandbox(
            args.contract,
            args.card,
            args.trajectories,
            candidate_id=args.candidate,
            approved_by=args.approved_by,
            approved_at=args.approved_at,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "review-keygen":
        return _review_keygen(args.private_key, args.public_key)
    if args.command == "review":
        return _review(
            args.report,
            decision=args.decision,
            reviewer_actor=args.reviewer,
            role=args.role,
            rationale=args.rationale,
            reference=args.reference,
            decided_at=args.decided_at,
            private_key=args.private_key,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "verify-review":
        return _verify_review(
            args.review, args.public_key, args.public_keys, as_json=args.as_json
        )
    if args.command == "audit-record":
        return _audit_record(args.ledger, args.artifact, args.public_key)
    if args.command == "audit-verify":
        return _audit_verify(args.ledger, as_json=args.as_json)
    if args.command == "audit-list":
        return _audit_list(args.ledger, as_json=args.as_json)
    if args.command == "audit-export":
        return _audit_export(args.ledger, args.output, as_json=args.as_json)
    if args.command == "audit-revoke-key":
        return _audit_revoke_key(
            args.ledger,
            args.key_id,
            actor=args.actor,
            reason=args.reason,
            reference=args.reference,
            revoked_at=args.revoked_at,
        )
    if args.command == "audit-revoke-review":
        return _audit_revoke_review(
            args.ledger,
            args.review_id,
            actor=args.actor,
            reason=args.reason,
            reference=args.reference,
            revoked_at=args.revoked_at,
        )
    if args.command == "guard":
        return _guard(
            args.evaluation,
            args.policy,
            tools=args.tool,
            sandbox_evidence=args.sandbox_evidence,
            ledger=args.record_ledger,
            private_key=args.private_key,
            public_key=args.public_key,
            as_json=args.as_json,
        )
    if args.command == "guard-sign-evaluation":
        return _guard_sign_evaluation(
            args.evaluation,
            args.private_key,
            args.public_key,
            ttl_seconds=args.ttl_seconds,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "guard-sign-recovery":
        return _guard_sign_recovery(
            args.target_snapshot_id,
            args.target_autonomy,
            args.recovery_run_id,
            args.private_key,
            args.public_key,
            ttl_seconds=args.ttl_seconds,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "guard-server":
        return _guard_server(
            args.evaluation,
            args.policy,
            ledger=args.record_ledger,
            private_key=args.private_key,
            public_key=args.public_key,
            evaluation_public_key=args.evaluation_public_key,
            recovery_public_key=args.recovery_public_key,
            recovery_evidence=args.recovery_evidence,
            bootstrap_autonomy_from_evaluation=args.bootstrap_autonomy_from_evaluation,
        )
    if args.command == "agent-evaluate":
        return _agent_evaluate(
            args.contract,
            args.adapter,
            args.agent,
            args.scenario,
            args.output_dir,
            as_json=args.as_json,
        )
    if args.command == "agent-sandbox-run":
        return _agent_sandbox_run(
            args.contract,
            args.adapter,
            args.sandbox_config,
            args.scenario,
            args.output_dir,
            as_json=args.as_json,
        )
    if args.command == "agent-cohort-evaluate":
        return _agent_cohort_evaluate(
            args.contract,
            args.adapter,
            args.cohort,
            args.output_dir,
            as_json=args.as_json,
        )
    if args.command == "clause-coverage":
        return _clause_coverage(
            args.paths,
            args.contract,
            audit_csv=args.audit_csv,
            require_all_triggered=args.require_all_triggered,
            require_all_failed=args.require_all_failed,
            cards_dir=args.cards_dir,
            cards_md=args.cards_md,
            as_json=args.as_json,
        )
    if args.command == "swarm-evaluate":
        return _swarm_evaluate(
            args.contract,
            args.adapter,
            args.swarm,
            args.output_dir,
            as_json=args.as_json,
            swarm_contract_path=args.swarm_contract,
            private_key=args.private_key,
            public_key=args.public_key,
        )
    if args.command == "swarm-keygen":
        return _swarm_keygen(args.private_key, args.public_key)
    if args.command == "swarm-verify":
        return _swarm_verify(
            args.report,
            args.channel,
            args.public_key,
            args.evidence_dir,
            as_json=args.as_json,
            swarm_contract_path=args.swarm_contract,
        )
    if args.command == "social-experiment":
        return _social_experiment(
            args.kind,
            trials=args.trials,
            sizes=args.sizes,
            seed=args.seed,
            variant=args.variant,
            case=args.case,
            output_dir=args.output_dir,
            private_key=args.private_key,
            public_key=args.public_key,
            signed_at=args.signed_at,
            verify=args.verify,
            as_json=args.as_json,
        )
    if args.command == "boundary-run":
        return _boundary_run(
            args.contract,
            args.plan,
            args.private_key,
            args.output_dir,
            payment_authorization_public_key=args.payment_authorization_public_key,
            timeout_seconds=args.timeout_seconds,
            backend=args.backend,
            docker_image=args.docker_image,
            docker_memory_mb=args.docker_memory_mb,
            docker_cpus=args.docker_cpus,
            docker_pids_limit=args.docker_pids_limit,
            docker_tmpfs_mb=args.docker_tmpfs_mb,
            as_json=args.as_json,
        )
    if args.command == "boundary-verify":
        return _boundary_verify(
            args.report,
            args.public_key,
            as_json=args.as_json,
        )
    if args.command == "payment-authorize":
        return _payment_authorize(
            args.private_key,
            authorization_id=args.authorization_id,
            execution_id=args.execution_id,
            contract_id=args.contract_id,
            candidate_version=args.candidate_version,
            operation_id=args.operation_id,
            amount_eur=args.amount_eur,
            beneficiary_id=args.beneficiary_id,
            approved_by=args.approved_by,
            ttl_seconds=args.ttl_seconds,
            output=args.output,
            as_json=args.as_json,
        )
    if args.command == "enforce-envelope":
        return _enforce_envelope(
            args.ledger,
            args.contract,
            args.target_clause_id,
            args.current_envelope_usage,
            args.now,
            args.review,
            args.review_id,
            args.reference,
            output=args.output,
            as_json=args.as_json,
            public_keys=args.public_keys,
        )
    if args.command == "ai-act-readiness":
        return _ai_act_readiness(
            args.profile,
            args.obligations,
            args.evaluations,
            args.output_dir,
            as_json=args.as_json,
        )
    if args.command == "ai-act-ingest":
        return _ai_act_ingest(
            args.profile,
            args.obligations,
            args.evaluations,
            args.db,
            as_json=args.as_json,
        )
    if args.command == "ai-act-report":
        return _ai_act_report(
            args.db,
            args.system_id,
            args.output_dir,
            as_json=args.as_json,
        )
    if args.command == "ai-act-systems":
        return _ai_act_systems(args.db, as_json=args.as_json)
    if args.command == "ai-act-cycle-keygen":
        return _ai_act_cycle_keygen(args.private_key, args.public_key)
    if args.command == "ai-act-cycle":
        return _ai_act_cycle(
            args.profile,
            args.obligations,
            args.evaluations,
            args.db,
            args.output_dir,
            args.audit_ledger,
            args.private_key,
            args.public_key,
            ran_at=args.ran_at,
            as_json=args.as_json,
        )
    if args.command == "ai-act-cycle-verify":
        return _ai_act_cycle_verify(args.receipt, as_json=args.as_json)
    if args.command == "discover-leads":
        return _discover_leads(
            args.input,
            output=args.output,
            min_score=args.min_score,
            as_json=args.as_json,
        )
    if args.command == "lead-radar":
        return _lead_radar(
            args.config,
            raw_output=args.raw_output,
            crm_output=args.crm_output,
            min_score=args.min_score,
            as_json=args.as_json,
        )
    if args.command == "telemetry-dry-run":
        forwarded: list[str] = [str(args.input)]
        if args.template is not None:
            forwarded.extend(["--template", str(args.template)])
        if args.connector is not None:
            forwarded.extend(["--connector", args.connector])
        if args.contract is not None:
            forwarded.extend(["--contract", str(args.contract)])
        if args.adapter is not None:
            forwarded.extend(["--adapter", str(args.adapter)])
        forwarded.extend(["--candidate-version", args.candidate_version])
        if args.as_json:
            forwarded.append("--json")
        return _telemetry_dry_run_main(forwarded)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
