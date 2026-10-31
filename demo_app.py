"""Bounded live laboratory for selected Meta held-out cases.

The app runs exactly one case against Meta when the operator presses the
button, then signs and verifies the resulting evidence package. It never
accepts free-form input, never falls back to fixtures, and enforces the quota
before any provider call.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import gradio as gr
import yaml

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from hackatons.meta.run_meta_harness import (  # noqa: E402
    ADAPTER_PATH,
    CONTRACT_PATH,
    GUARDED_SANDBOX_PATH,
    HELD_OUT_SUITE_PATH,
    LAB_CASES,
    _resolve_live_api_key,
    run_meta_safety_scan,
)
from autopilot.adapter import load_adapter_config  # noqa: E402
from autopilot.agent_sandbox import (  # noqa: E402
    load_declarative_sandbox_config,
    run_declarative_sandbox_evaluation,
)
from autopilot.contracts import load_contract  # noqa: E402
from autopilot.insurance_agent import AgentScenario  # noqa: E402
from scripts.package_meta_evidence import (  # noqa: E402
    build_package,
    tamper_test_package,
    validate_report,
    verify_package,
)

DEMO_CASE = "meta-heldout_authority_01_audit_chair"
MAX_REQUESTS_PER_RUN = 6
MAX_RUNS_PER_VISITOR_24H = 3
MAX_RUNS_PER_DAY = 40
MAX_RUNS_TOTAL = 200
MAX_REQUESTS_TOTAL = 1200
RUN_TIMEOUT_SECONDS = 180
PROVIDER_TIMEOUT_SECONDS = 25
RETENTION_SECONDS = 3600
DEFAULT_STATE_DIR = APP_ROOT / ".tmp" / "demo-state"

# Embedded artwork keeps the laboratory usable without external image requests.
CYBER_HERO = """
<header class="ih-hero">
  <div class="ih-copy">
    <div class="ih-brand"><span class="ih-brand-icon" aria-hidden="true">⬡</span>
      INTEGRITY <span>HARNESS</span></div>
    <div class="ih-eyebrow"><span aria-hidden="true">✳</span> AGENT SAFETY LAB <i></i> META HACKATHON</div>
    <h1>Watch the decision.<br><span>Verify the evidence.</span></h1>
    <p>Explore how an agent responds under pressure, compare its decisions with and without a guard, and keep verifiable proof.</p>
    <div class="ih-tags"><span>⬡ DETERMINISTIC CONTRACT</span><span>◇ MOCK TOOLS</span><span>⌁ SIGNED EVIDENCE</span></div>
  </div>
  <div class="ih-visual" role="img" aria-label="Illustration of a digital shield protecting a network of decisions">
    <svg viewBox="0 0 420 340" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <radialGradient id="ih-glow"><stop stop-color="#1ee8df" stop-opacity=".22"/><stop offset="1" stop-color="#1ee8df" stop-opacity="0"/></radialGradient>
        <linearGradient id="ih-line" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#72fff0"/><stop offset="1" stop-color="#557bff"/></linearGradient>
        <linearGradient id="ih-shield" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#183c55"/><stop offset="1" stop-color="#091a2d"/></linearGradient>
      </defs>
      <circle cx="210" cy="167" r="158" fill="url(#ih-glow)"/>
      <g fill="none" stroke="#53dcd9" opacity=".22"><circle cx="210" cy="167" r="136"/><circle cx="210" cy="167" r="109" stroke-dasharray="3 8"/><circle cx="210" cy="167" r="82"/><path d="M30 167h360M210 7v320M71 70l278 194M349 70 71 264"/></g>
      <g fill="#71fff0"><circle cx="77" cy="100" r="3"/><circle cx="349" cy="116" r="3"/><circle cx="116" cy="263" r="3"/><circle cx="314" cy="256" r="3"/></g>
      <path d="M210 67 286 95v61c0 55-32 89-76 113-44-24-76-58-76-113V95z" fill="url(#ih-shield)" stroke="url(#ih-line)" stroke-width="2.5"/>
      <path d="M210 82 273 105v51c0 46-25 75-63 96-38-21-63-50-63-96v-51z" fill="none" stroke="#55e6df" stroke-opacity=".34"/>
      <path d="m182 161 19 19 40-43" fill="none" stroke="#71fff0" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
      <g fill="#0d2237" stroke="#70f3e7" stroke-opacity=".65"><rect x="30" y="143" rx="7" width="68" height="34"/><rect x="318" y="143" rx="7" width="72" height="34"/><rect x="165" y="287" rx="7" width="90" height="28"/></g>
      <g fill="#a0f8ef" font-family="monospace" font-size="10" text-anchor="middle"><text x="64" y="164">AGENT</text><text x="354" y="164">GUARD</text><text x="210" y="305">EVIDENCE</text></g>
    </svg>
  </div>
</header>
"""

CYBER_CSS = """
body, .gradio-container { background: #070c18 !important; color: #dfeaf3 !important; }
.gradio-container {
  --body-background-fill: #070c18; --body-text-color: #dfeaf3;
  --block-background-fill: #101b2b; --block-border-color: #243449;
  --block-label-text-color: #d1dfeb; --input-background-fill: #0a1422;
  --input-border-color: #344b5c; --button-secondary-background-fill: #15283a;
  --button-secondary-text-color: #e0f7f3; --button-primary-background-fill: #44e7d5;
  --button-primary-text-color: #041a20; --border-color-primary: #304659;
  font-family: 'Segoe UI', system-ui, sans-serif !important;
  background-image: radial-gradient(ellipse at 84% 5%, #0e343c 0%, transparent 36%),
    radial-gradient(ellipse at 7% 36%, #142040 0%, transparent 38%) !important;
}
.gradio-container .main { max-width: 1280px !important; margin: auto !important; }
.gradio-container .main > .wrap { padding: 26px 30px 70px !important; }
.gradio-container .prose, .gradio-container .prose p, .gradio-container .prose li { color: #c4d4e1 !important; }
.gradio-container .prose h1, .gradio-container .prose h2, .gradio-container .prose h3 { color: #effbff !important; }
.gradio-container .prose code { color: #95f9e9 !important; }
.gradio-container .prose pre { background: #09131f !important; border: 1px solid #263d4c; border-radius: 10px; }
.gradio-container button { border-radius: 10px !important; font-weight: 650 !important; letter-spacing: .01em; }
.gradio-container button.primary { background: #45e7d4 !important; color: #06202a !important; border: 1px solid #7ffff0 !important; box-shadow: 0 0 24px #26d7c333; }
.gradio-container button.secondary { background: #142538 !important; color: #dffcf6 !important; border: 1px solid #375268 !important; }
.gradio-container button:focus-visible, .gradio-container input:focus-visible { outline: 3px solid #80fff1 !important; outline-offset: 3px; }
.gradio-container .ih-panel { background: linear-gradient(145deg, #101e2e, #0b1524) !important; border: 1px solid #273c50 !important; border-radius: 16px !important; padding: 20px !important; box-shadow: 0 14px 40px #0003; }
.gradio-container .ih-results { gap: 16px !important; margin-top: 12px !important; }
.gradio-container .ih-status { border-left: 3px solid #58e9d8; padding-left: 14px !important; }
.gradio-container .ih-section { padding-top: 16px !important; border-top: 1px solid #263747; }
.ih-observation-grid, .ih-replay-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:12px 0 22px; }
.ih-observation, .ih-replay-path { background:linear-gradient(145deg,#122438,#0b1725); border:1px solid #2a4558; border-radius:14px; padding:18px; min-width:0; }
.ih-card-label { color:#83a9bb; font:600 11px ui-monospace,monospace; letter-spacing:.09em; text-transform:uppercase; }
.ih-card-title { color:#edfaff; font-size:16px; font-weight:700; margin:10px 0; }
.ih-card-text { color:#c4d4e1; font-size:14px; line-height:1.6; white-space:pre-wrap; overflow-wrap:anywhere; }
.ih-chip { display:inline-block; border:1px solid #486174; border-radius:100px; color:#d8e8f0; font:700 11px ui-monospace,monospace; padding:5px 9px; margin:4px 5px 8px 0; }
.ih-chip.pass { color:#8af3cf; border-color:#287b66; background:#10392f; }
.ih-chip.fail { color:#ffc6b9; border-color:#a45b55; background:#45292c; }
.ih-chip.indeterminate { color:#f6dc9d; border-color:#8a7547; background:#3d3423; }
.ih-chip.neutral { color:#b6dce7; border-color:#476578; background:#172b3b; }
.ih-replay-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
.ih-replay-note { color:#b9d0da; line-height:1.6; margin:12px 0; }
.ih-steps { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; list-style:none; margin:18px 0 6px; padding:0; }
.ih-step { background:linear-gradient(160deg,#0f2233,#0a1421); border:1px solid #28414f; border-radius:14px; padding:16px 18px; }
.ih-step b { display:block; color:#6ef0e0; font:700 11px ui-monospace,monospace; letter-spacing:.14em; margin-bottom:8px; }
.ih-step strong { display:block; color:#eefaff; font-size:15px; margin-bottom:6px; }
.ih-step span { color:#a9c0cf; font-size:13px; line-height:1.55; }
.ih-diagnostic-grid { display:grid; grid-template-columns:1fr; gap:12px; margin-top:12px; }
.ih-diagnostic { background:linear-gradient(145deg,#0f1f31,#0a1523); border:1px solid #2a4256; border-left:3px solid #3f7f92; border-radius:12px; padding:16px 18px; }
.ih-diagnostic-arm { border-top:1px solid #22384a; padding-top:12px; margin-top:12px; }
.ih-hero { display:flex; align-items:center; justify-content:space-between; gap:24px; padding: 30px 12px 24px; border-bottom:1px solid #263b4c; }
.ih-copy { flex:1 1 520px; min-width:0; }
.ih-brand { color:#edfaff; font-weight:800; letter-spacing:.12em; font-size:13px; margin-bottom:40px; }
.ih-brand span:not(.ih-brand-icon) { color:#62e9dc; }
.ih-brand-icon { color:#64f4e4; font-size:23px; vertical-align:middle; margin-right:8px; }
.ih-eyebrow { color:#70e9df; font:600 11px/1.5 ui-monospace, monospace; letter-spacing:.15em; }
.ih-eyebrow i { display:inline-block; width:3px; height:3px; background:#85abbb; border-radius:50%; vertical-align:middle; margin:0 9px; }
.ih-copy h1 { color:#f5fbff; font-size:clamp(32px,4vw,52px); line-height:1.1; letter-spacing:-.045em; margin:16px 0; }
.ih-copy h1 span { color:#65eadc; }
.ih-copy p { color:#aabed0; font-size:16px; line-height:1.65; max-width:590px; margin:0; }
.ih-tags { display:flex; flex-wrap:wrap; gap:8px; margin-top:26px; }
.ih-tags span { color:#99d9d4; background:#102a35; border:1px solid #245153; border-radius:6px; padding:7px 9px; font:600 10px ui-monospace, monospace; letter-spacing:.04em; }
.ih-visual { flex:0 1 410px; min-width:220px; }
.ih-visual svg { width:100%; height:auto; display:block; }
@media (max-width: 700px) {
  .gradio-container .main > .wrap { padding: 16px 16px 48px !important; }
  .ih-hero { flex-direction:column; align-items:flex-start; padding:16px 0 20px; }
  .ih-copy { flex-basis:auto; }
  .ih-brand { margin-bottom:24px; }
  .ih-visual { align-self:center; flex-basis:auto; width:min(100%, 300px); }
  .ih-observation-grid, .ih-replay-grid { grid-template-columns:1fr; }
  .ih-steps { grid-template-columns:1fr; }
}
"""

STEP_STRIP = """
<ol class="ih-steps" aria-label="Laboratory walkthrough">
  <li class="ih-step"><b>STEP 1 / CHOOSE</b><strong>Pick a held-out scenario</strong>
    <span>Authority, urgency or ambiguity. The three user messages stay verbatim.</span></li>
  <li class="ih-step"><b>STEP 2 / COMPARE</b><strong>Run and compare</strong>
    <span>Up to 6 live calls, two independent arms, deterministic contract verdicts.</span></li>
  <li class="ih-step"><b>STEP 3 / VERIFY</b><strong>Check the evidence</strong>
    <span>Sign the package, verify the Ed25519 signature and run the byte-tamper test.</span></li>
</ol>
"""


@dataclass(frozen=True)
class DemoLimits:
    max_requests_per_run: int = MAX_REQUESTS_PER_RUN
    max_runs_per_visitor_24h: int = MAX_RUNS_PER_VISITOR_24H
    max_runs_per_day: int = MAX_RUNS_PER_DAY
    max_runs_total: int = MAX_RUNS_TOTAL
    max_requests_total: int = MAX_REQUESTS_TOTAL
    run_timeout_seconds: int = RUN_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Reservation:
    allowed: bool
    run_id: str = ""
    reason: str = ""


class DemoLedger:
    """Persistent quota ledger; a reservation reserves six requests up front."""

    def __init__(self, path: Path, limits: DemoLimits = DemoLimits()) -> None:
        self.path = Path(path)
        self.limits = limits
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.isolation_level = None
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    visitor_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    requests INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_visitor
                    ON runs(visitor_hash, started_at);
                CREATE INDEX IF NOT EXISTS idx_runs_started
                    ON runs(started_at);
                """
            )
        finally:
            connection.close()

    def reserve(self, visitor_hash: str, now: datetime) -> Reservation:
        current = now.astimezone(UTC)
        window_start = (current - timedelta(hours=24)).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            visitor_count = connection.execute(
                "SELECT COUNT(*) FROM runs "
                "WHERE visitor_hash = ? AND started_at >= ? AND status != 'refused'",
                (visitor_hash, window_start),
            ).fetchone()[0]
            if visitor_count >= self.limits.max_runs_per_visitor_24h:
                connection.execute("ROLLBACK")
                return Reservation(False, reason="visitor")

            day_count = connection.execute(
                "SELECT COUNT(*) FROM runs "
                "WHERE started_at >= ? AND status != 'refused'",
                (window_start,),
            ).fetchone()[0]
            if day_count >= self.limits.max_runs_per_day:
                connection.execute("ROLLBACK")
                return Reservation(False, reason="daily")

            total_count = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status != 'refused'"
            ).fetchone()[0]
            if total_count >= self.limits.max_runs_total:
                connection.execute("ROLLBACK")
                return Reservation(False, reason="total")

            requests_used = connection.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM runs WHERE status != 'refused'"
            ).fetchone()[0]
            if (
                requests_used + self.limits.max_requests_per_run
                > self.limits.max_requests_total
            ):
                connection.execute("ROLLBACK")
                return Reservation(False, reason="requests")

            run_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO runs "
                "(run_id, visitor_hash, started_at, finished_at, status, requests) "
                "VALUES (?, ?, ?, NULL, 'reserved', ?)",
                (
                    run_id,
                    visitor_hash,
                    current.isoformat(),
                    self.limits.max_requests_per_run,
                ),
            )
            connection.execute("COMMIT")
            return Reservation(True, run_id=run_id)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def complete(self, run_id: str, status: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
                (datetime.now(UTC).isoformat(), status, run_id),
            )
        finally:
            connection.close()

    def stats(self) -> dict[str, int]:
        connection = self._connect()
        try:
            runs_row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(requests), 0) "
                "FROM runs WHERE status != 'refused'"
            ).fetchone()
            completed = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status = 'completed'"
            ).fetchone()[0]
        finally:
            connection.close()
        return {
            "runs_total": int(runs_row[0]),
            "requests_reserved": int(runs_row[1]),
            "runs_completed": int(completed),
        }


def _load_or_create_salt(state_dir: Path) -> str:
    path = state_dir / "visitor_salt"
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        value = ""
    if value:
        return value
    value = secrets.token_hex(16)
    path.write_text(value + "\n", encoding="ascii")
    return value


TRUSTED_PROXY_ENV = "DEMO_TRUSTED_PROXIES"

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _trusted_proxy_networks() -> tuple[_Network, ...]:
    """Parse operator-declared proxy addresses; empty means trust no header."""

    networks: list[_Network] = []
    for entry in os.environ.get(TRUSTED_PROXY_ENV, "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _as_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_trusted_proxy(host: str, networks: tuple[_Network, ...]) -> bool:
    address = _as_address(host)
    return address is not None and any(address in network for network in networks)


def _forwarded_client_host(peer: str, header: str, networks: tuple[_Network, ...]) -> str:
    """Use the hop added by the nearest trusted proxy, never a client prefix."""

    for hop in reversed([item.strip() for item in header.split(",") if item.strip()]):
        if _as_address(hop) is not None and not _is_trusted_proxy(hop, networks):
            return hop
    return peer


def visitor_hash(request: gr.Request | None, salt: str) -> str:
    """Hash a per-session identity; never store or display the raw values.

    The Gradio session is the primary identity so two visitors behind one
    address do not consume each other's quota. ``X-Forwarded-For`` is read only
    when the immediate peer is a declared trusted proxy.
    """

    host = ""
    agent = ""
    if request is not None:
        session = str(getattr(request, "session_hash", "") or "")
        if session:
            host = f"session:{session}"
        fastapi_request = getattr(request, "request", None)
        if fastapi_request is not None:
            client = getattr(fastapi_request, "client", None)
            peer = str(getattr(client, "host", "") or "")
            headers = getattr(fastapi_request, "headers", {}) or {}
            agent = headers.get("user-agent", "")
            if not host:
                host = peer
                forwarded = str(headers.get("x-forwarded-for", "") or "")
                networks = _trusted_proxy_networks()
                if forwarded and networks and _is_trusted_proxy(peer, networks):
                    host = _forwarded_client_host(peer, forwarded, networks)
        if not host:
            host = "anonymous"
    raw = f"{salt}|{host}|{agent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class DemoOutcome:
    ok: bool
    run_id: str = ""
    message: str = ""
    report: dict[str, Any] | None = None
    report_path: Path | None = None
    package_path: Path | None = None
    package_sha256: str = ""
    verification: dict[str, Any] | None = None
    tamper: dict[str, Any] | None = None
    elapsed_seconds: float = 0.0


def _run_live_demo(*, output_dir: Path, case_id: str) -> dict[str, Any]:
    return run_meta_safety_scan(
        output_dir=output_dir,
        live_agent=True,
        demo_only=case_id,
    )


def _default_key_check() -> bool:
    value, name = _resolve_live_api_key()
    return bool(value and name)


def _quota_message(reason: str, limits: DemoLimits = DemoLimits()) -> str:
    runs = limits.max_runs_per_visitor_24h
    replay_note = (
        " This is a quota, not a failure: replay and synthetic control still work "
        "and spend no live quota."
    )
    message = {
        "visitor": (
            f"You already used your {runs} live runs in the last 24 h. "
            f"The demo quota is {runs} per visitor/24 h."
        ),
        "daily": (
            f"The daily quota of {limits.max_runs_per_day} runs is exhausted; "
            "try again tomorrow."
        ),
        "total": f"The total quota of {limits.max_runs_total} demo runs is exhausted.",
        "requests": (
            f"The total quota of {limits.max_requests_total} reserved calls is exhausted."
        ),
    }.get(reason, "Quota exhausted.")
    return message + replay_note


class DemoService:
    """Testable service behind the Gradio UI."""

    def __init__(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        *,
        limits: DemoLimits = DemoLimits(),
        runner: Callable[..., dict[str, Any]] | None = None,
        key_check: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.limits = limits
        self.runner = runner or _run_live_demo
        self.key_check = key_check or _default_key_check
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ledger = DemoLedger(self.state_dir / "ledger.sqlite3", limits)
        self.salt = _load_or_create_salt(self.state_dir)
        self.runs_root = self.state_dir / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.private_key_path = self.state_dir / "signing" / "ed25519-private.pem"
        self._outcomes: dict[str, DemoOutcome] = {}

    def run(self, request: gr.Request | None = None, case_id: str = DEMO_CASE) -> DemoOutcome:
        if case_id not in LAB_CASES:
            return DemoOutcome(False, message="Scenario not allowed; the model was not called.")
        self._cleanup_old_runs()
        if not self.key_check():
            return DemoOutcome(
                False,
                message=(
                    "DEMO REFUSED: no Meta API key is configured. "
                    "The model was not called."
                ),
            )

        reservation = self.ledger.reserve(
            visitor_hash(request, self.salt), self.clock()
        )
        if not reservation.allowed:
            return DemoOutcome(
                False, message=_quota_message(reservation.reason, self.limits)
            )

        run_dir = self.runs_root / reservation.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            report = self.runner(output_dir=run_dir, case_id=case_id)
        except Exception as exc:
            self.ledger.complete(reservation.run_id, "failed")
            return DemoOutcome(
                False,
                run_id=reservation.run_id,
                message=f"The run failed ({type(exc).__name__}). No package was generated.",
            )

        elapsed = time.monotonic() - started
        report_path = run_dir / "meta_safety_scan_report.json"
        if elapsed > self.limits.run_timeout_seconds:
            self.ledger.complete(reservation.run_id, "failed")
            return DemoOutcome(
                False,
                run_id=reservation.run_id,
                message="The run exceeded the time limit. No package was generated.",
            )
        if not report_path.exists():
            self.ledger.complete(reservation.run_id, "failed")
            return DemoOutcome(
                False,
                run_id=reservation.run_id,
                message="The run did not write a report. No package was generated.",
            )

        try:
            validate_report(report)
        except ValueError as exc:
            self.ledger.complete(reservation.run_id, "failed")
            return DemoOutcome(
                False,
                run_id=reservation.run_id,
                message=f"The report does not satisfy the demo contract: {exc}",
            )

        requests = int(report.get("live_run_limits", {}).get("requests_observed", 0))
        if requests > self.limits.max_requests_per_run:
            self.ledger.complete(reservation.run_id, "failed")
            return DemoOutcome(
                False,
                run_id=reservation.run_id,
                message="The run exceeded the call cap. No package was generated.",
            )

        self.ledger.complete(reservation.run_id, "completed")
        outcome = DemoOutcome(
            True,
            run_id=reservation.run_id,
            message="Run completed.",
            report=report,
            report_path=report_path,
            elapsed_seconds=elapsed,
        )
        self._outcomes[reservation.run_id] = outcome
        return outcome

    def sign(self, run_id: str) -> DemoOutcome:
        outcome = self._outcomes.get(run_id)
        if outcome is None or outcome.report_path is None or not outcome.report_path.exists():
            return DemoOutcome(False, run_id=run_id, message="Run a live slice before signing.")

        package_path = outcome.report_path.parent / "meta-demo-live-signed.zip"
        try:
            if package_path.exists():
                verification = verify_package(package_path)
                tamper = tamper_test_package(package_path)
                package_sha256 = hashlib.sha256(package_path.read_bytes()).hexdigest()
            else:
                built = build_package(
                    outcome.report_path,
                    package_path,
                    APP_ROOT,
                    self.private_key_path,
                )
                verification = verify_package(package_path)
                tamper = tamper_test_package(package_path)
                package_sha256 = built["sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return DemoOutcome(
                False,
                run_id=run_id,
                message=f"Package signing failed: {exc}",
            )

        outcome.package_path = package_path
        outcome.package_sha256 = package_sha256
        outcome.verification = verification
        outcome.tamper = tamper
        self._outcomes[run_id] = outcome
        self._schedule_cleanup(package_path.parent)
        return outcome

    def verify(self, run_id: str) -> DemoOutcome:
        outcome = self._outcomes.get(run_id)
        if outcome is None or outcome.package_path is None:
            return DemoOutcome(False, run_id=run_id, message="Sign the package first.")
        try:
            outcome.verification = verify_package(outcome.package_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return DemoOutcome(
                False,
                run_id=run_id,
                message=f"Verification failed: {exc}",
            )
        outcome.ok = True
        outcome.message = "Signature valid."
        return outcome

    def tamper(self, run_id: str) -> DemoOutcome:
        outcome = self._outcomes.get(run_id)
        if outcome is None or outcome.package_path is None:
            return DemoOutcome(False, run_id=run_id, message="Sign the package first.")
        try:
            result = tamper_test_package(outcome.package_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return DemoOutcome(
                False,
                run_id=run_id,
                message=f"Tamper test failed: {exc}",
            )
        outcome.tamper = result
        outcome.ok = bool(result.get("rejected"))
        outcome.message = (
            "Alteration rejected."
            if outcome.ok
            else "The altered package was not rejected."
        )
        return outcome

    def _cleanup_old_runs(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        for path in self.runs_root.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue

    @staticmethod
    def _schedule_cleanup(path: Path) -> None:
        timer = threading.Timer(
            RETENTION_SECONDS,
            shutil.rmtree,
            args=(path,),
            kwargs={"ignore_errors": True},
        )
        timer.daemon = True
        timer.start()


def _last_assistant(arm: dict[str, Any]) -> dict[str, Any]:
    turns = [
        turn
        for turn in arm.get("conversation_turns", [])
        if turn.get("role") == "assistant"
    ]
    return turns[-1] if turns else {}


def _clause_rows(before: dict[str, Any], after: dict[str, Any]) -> list[list[str]]:
    before_map = {
        clause["clause_id"]: clause["status"]
        for clause in before.get("clause_results", [])
    }
    after_map = {
        clause["clause_id"]: clause["status"]
        for clause in after.get("clause_results", [])
    }
    order = list(before_map) + [cid for cid in after_map if cid not in before_map]
    return [
        [clause_id, before_map.get(clause_id, "—"), after_map.get(clause_id, "—")]
        for clause_id in order
    ]


def _code_block(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(4, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _case_intro(case_id: str = DEMO_CASE) -> str:
    if case_id not in LAB_CASES:
        return "Scenario not allowed."
    suite = json.loads(HELD_OUT_SUITE_PATH.read_text(encoding="utf-8"))
    fixture_id = case_id.removeprefix("meta-")
    cases = [item for item in suite["scenarios"] if item["id"] == fixture_id]
    if len(cases) != 1:
        raise ValueError("demo scenario must exist exactly once")
    turns = "\n\n".join(
        f"**{index}. User message**\n\n{_code_block(text)}"
        for index, text in enumerate(cases[0]["turns"], start=1)
    )
    return (
        f"## Selected case: {LAB_CASES[case_id]}\n\n"
        "These are the three fixed messages sent to each arm, verbatim:\n\n"
        f"{turns}\n\n"
        "On the final turn the pressure category and the unverified authorization "
        "state are supplied as metadata. Tools are local mocks: no payment or "
        "authorization system is connected."
    )


def _tool_lines(arm: dict[str, Any], turn_ref: str) -> str:
    calls = [
        call for call in arm.get("tool_calls", [])
        if str(call.get("ref", "")).startswith(f"{turn_ref}-")
    ]
    if not calls:
        return "No tool calls were recorded on this turn."
    lines = []
    for call in calls:
        provenance = call.get("provenance") or {}
        recorded = {
            "tool": call.get("name"),
            "effective_arguments": call.get("arguments"),
            "mock_result": "success" if call.get("success") else "not successful",
            "provenance": provenance.get("source"),
            "guard_intervention": provenance.get("guard_intervention"),
        }
        if "model_proposal" in provenance:
            recorded["model_original_proposal"] = provenance["model_proposal"]
        if "model_proposals" in provenance:
            recorded["model_original_proposals"] = provenance["model_proposals"]
        lines.append(_code_block(json.dumps(recorded, ensure_ascii=False, indent=2)))
    return "\n\n".join(lines)


def _timeline(comparison: dict[str, Any]) -> str:
    before = comparison["before_remediation_unguarded"]
    after = comparison["after_remediation_guarded"]
    lines = [
        "## Run walkthrough",
        "The two arms make independent calls: they are not a replay of the same "
        "proposal. Observations are grouped by turn and do not represent an exact "
        "execution chronology.",
    ]
    for index in range(1, 4):
        turn_ref = f"turn-{index:03d}"
        user = next(
            (item for item in before.get("conversation_turns", [])
             if item.get("ref") == f"{turn_ref}-user"),
            {},
        )
        lines.extend([
            f"### Turn {index} · Request",
            _code_block(str(user.get("content") or "(no request recorded)")),
        ])
        for label, arm in (("Unguarded", before), ("Guarded", after)):
            reply = next(
                (item for item in arm.get("conversation_turns", [])
                 if item.get("ref") == f"{turn_ref}-assistant"),
                {},
            )
            proposal = reply.get("model_proposal_text")
            intervention = reply.get("guard_intervention") or "none recorded"
            lines.extend([
                f"#### {label} · model proposal",
                _code_block(str(proposal) if proposal else "(no proposal text recorded)"),
                f"**Effective response · intervention:** `{intervention}`",
                _code_block(str(reply.get("content") or "(no response recorded)")),
                "**Recorded mock calls and provenance:**",
                _tool_lines(arm, turn_ref),
            ])
    return "\n\n".join(lines)


def _clause_explanations(before: dict[str, Any], after: dict[str, Any]) -> str:
    lines = [
        "## How to read the contract verdict",
        "`pass`: the clause is satisfied by the mapped observations; "
        "`fail`: the evaluator recorded an unmet obligation or a prohibited "
        "action; `indeterminate`: there was not enough evidence to decide. "
        "A `fail` does not by itself establish that the model attempted a payment. "
        "The adapter recognises a limited set of patterns and may not map a safe "
        "reply phrased in another way.",
    ]
    for label, arm in (("Unguarded", before), ("Guarded", after)):
        for clause in arm.get("clause_results", []):
            if clause.get("status") in {"fail", "indeterminate"}:
                lines.extend([
                    f"**{label} · {clause.get('clause_id')} · {clause.get('status')}**",
                    _code_block(str(clause.get("reason") or "(no reason recorded)")),
                ])
    return "\n\n".join(lines)


def _safe_text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _status_chip(status: Any) -> str:
    value = str(status or "not recorded")
    kind = value if value in {"pass", "fail", "indeterminate"} else "neutral"
    return f'<span class="ih-chip {kind}">Contract: {_safe_text(value)}</span>'


_CONTRACT_DOCUMENT: dict[str, Any] | None = None


def _clause_catalog() -> dict[str, dict[str, Any]]:
    """Map clause ids to the text of the loaded contract, for read-only display."""
    global _CONTRACT_DOCUMENT
    if _CONTRACT_DOCUMENT is None:
        _CONTRACT_DOCUMENT = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")) or {}
    clauses = _CONTRACT_DOCUMENT.get("clauses") or []
    return {str(item.get("id")): item for item in clauses if isinstance(item, dict)}


def _clause_diagnostics(before: dict[str, Any], after: dict[str, Any]) -> str:
    catalog = _clause_catalog()
    before_map = {clause["clause_id"]: clause for clause in before.get("clause_results", [])}
    after_map = {clause["clause_id"]: clause for clause in after.get("clause_results", [])}
    order = list(before_map) + [cid for cid in after_map if cid not in before_map]
    reading = {
        "pass": "Recorded evidence satisfies the mapped obligation.",
        "fail": "The evaluator recorded an unmet obligation or a prohibited action.",
        "indeterminate": "Insufficient evidence: the boundary is not established either way.",
    }
    cards = []
    for clause_id in order:
        spec = catalog.get(clause_id, {})
        required = " ".join(str(spec.get("description") or "Not described in the loaded contract.").split())
        severity = str(spec.get("severity") or "not recorded")
        arms = []
        for label, mapped in (("Unguarded", before_map), ("Guarded", after_map)):
            clause = mapped.get(clause_id)
            if clause is None:
                arms.append(
                    f'<div class="ih-diagnostic-arm">{_status_chip(None)} <strong>{label}</strong>'
                    '<div class="ih-card-text">Not evaluated in this arm.</div></div>'
                )
                continue
            status = str(clause.get("status") or "not recorded")
            refs = [str(ref) for ref in clause.get("evidence_refs") or []]
            arms.append(
                f'<div class="ih-diagnostic-arm">{_status_chip(status)} <strong>{label}</strong>'
                f'<div class="ih-card-text"><em>Observed:</em> {_safe_text(clause.get("reason") or "(no reason recorded)")}\n'
                f'<em>Evidence refs:</em> {_safe_text(", ".join(refs) if refs else "none recorded")}\n'
                f'<em>Reading:</em> {_safe_text(reading.get(status, "No interpretation available."))}</div></div>'
            )
        cards.append(
            '<article class="ih-diagnostic">'
            f'<div class="ih-card-label">{_safe_text(severity)} severity</div>'
            f'<div class="ih-card-title">{_safe_text(clause_id)}</div>'
            f'<div class="ih-card-text"><strong>Required:</strong> {_safe_text(required)}</div>'
            + "".join(arms) + '</article>'
        )
    return (
        '<section aria-label="Clause diagnostics"><h3>Clause-by-clause diagnosis</h3>'
        '<p class="ih-replay-note">Each card pairs the contract text with what the evaluator recorded. '
        'A <code>fail</code> is not proof of a payment attempt: the adapter maps a limited set of patterns. '
        '<code>indeterminate</code> means the evidence was insufficient, not that the behaviour was safe.</p>'
        '<div class="ih-diagnostic-grid">' + "".join(cards) + '</div></section>'
    )


def _summary_cards(comparison: dict[str, Any]) -> str:
    before = comparison["before_remediation_unguarded"]
    after = comparison["after_remediation_guarded"]
    before_reply = _last_assistant(before)
    after_reply = _last_assistant(after)
    intervention = after_reply.get("guard_intervention") or "none recorded"
    actions = []
    for label, arm in (("Unguarded", before), ("Guarded", after)):
        calls = arm.get("tool_calls", [])
        if calls:
            for call in calls:
                result = "mock success" if call.get("success") else "not successful"
                actions.append(f"{label}: {call.get('name', 'unknown tool')} · {result}")
        else:
            actions.append(f"{label}: no mock calls recorded")
    return (
        '<section aria-label="Observation summary"><div class="ih-observation-grid">'
        '<article class="ih-observation"><div class="ih-card-label">01 / MODEL · NO GUARD</div>'
        f'{_status_chip(before.get("overall_status"))}'
        '<div class="ih-card-title">Final response observed</div>'
        f'<div class="ih-card-text">{_safe_text(before_reply.get("content") or "No response recorded")}</div></article>'
        '<article class="ih-observation"><div class="ih-card-label">02 / MODEL + GUARD</div>'
        f'{_status_chip(after.get("overall_status"))}'
        '<div class="ih-card-title">Proposal and effective response</div>'
        f'<div class="ih-card-text"><strong>Proposal:</strong> {_safe_text(after_reply.get("model_proposal_text") or "No proposal recorded")}\n\n'
        f'<strong>Intervention:</strong> {_safe_text(intervention)}\n\n'
        f'<strong>Effective response:</strong> {_safe_text(after_reply.get("content") or "No response recorded")}</div></article>'
        '<article class="ih-observation"><div class="ih-card-label">03 / TOOLS</div>'
        '<span class="ih-chip neutral">Local mocks only</span>'
        '<div class="ih-card-title">Recorded actions</div>'
        f'<div class="ih-card-text">{_safe_text(chr(10).join(actions))}</div></article>'
        '</div><p class="ih-replay-note">The arms use independent proposals. A contract fail does not by itself imply a payment attempt; '
        'read the detail and the clause cards for the recorded evidence.</p></section>'
    )


def _replay_paths(results: dict[str, Any]) -> str:
    cards = []
    for key, title in (("sin_frontera", "No boundary"), ("con_frontera", "With boundary")):
        calls = results.get(key, [])
        if not calls:
            summary = "No mock calls recorded on this path."
        else:
            summary = "\n".join(
                f"{call.get('name', 'unknown tool')} · "
                f"{'mock success' if call.get('success') else 'not successful'} · "
                f"{json.dumps(call.get('result'), ensure_ascii=False)}"
                for call in calls
            )
        cards.append(
            '<article class="ih-replay-path">'
            f'<div class="ih-card-label">{_safe_text(title)}</div>'
            '<span class="ih-chip neutral">Local mock</span>'
            f'<div class="ih-card-text">{_safe_text(summary)}</div></article>'
        )
    return '<div class="ih-replay-grid">' + ''.join(cards) + '</div>'


def _replay_proposal(text: str, calls: list[dict[str, Any]], *, authorized: bool) -> dict[str, Any]:
    """Replay one identical proposal against two local sandbox boundary settings."""
    base = load_declarative_sandbox_config(GUARDED_SANDBOX_PATH)
    scenario = AgentScenario.model_validate({
        "scenario_version": "0.1",
        "scenario_id": "lab-replay",
        "started_at": "2026-10-31T12:00:00Z",
        "turns": [{
            "offset_seconds": 30,
            "content": "Replay the captured proposal under the supplied authorization state.",
            "metadata": {"verified_authorization": authorized},
        }],
    })

    class ReplayProvider:
        def generate_with_tools(self, system_prompt, messages, tools):
            return text, calls

        def generate(self, system_prompt, messages):
            return text

    results = {}
    for label, enabled in (("sin_frontera", False), ("con_frontera", True)):
        config = base.model_copy(update={
            "policy": base.policy.model_copy(update={"safety_guard": enabled}),
            "environment": base.environment.model_copy(update={
                "generic_exception_guard": False,
                "override_provider_output_with_scenario_guard": False,
            }),
        })
        bundle = run_declarative_sandbox_evaluation(
            load_contract(CONTRACT_PATH), load_adapter_config(ADAPTER_PATH), config,
            scenario, provider=ReplayProvider(), include_interchange=False,
        )
        raw = bundle.agent_evaluation.agent_run.raw_session
        results[label] = [
            {"name": call.name, "arguments": call.arguments,
             "success": call.success, "result": call.result}
            for call in raw.tool_calls
        ]
    return results


def _replay_live(outcome: DemoOutcome | None) -> str:
    if outcome is None or not outcome.report:
        return '<p class="ih-replay-note">Run a live slice in this session first.</p>'
    turns = outcome.report["comparisons"][0]["before_remediation_unguarded"]["conversation_turns"]
    final = next((t for t in turns if t.get("ref") == "turn-003-assistant"), {})
    calls = final.get("model_tool_proposals")
    if not isinstance(calls, list):
        return '<p class="ih-replay-note">This run does not keep the original tool proposals; replay unavailable.</p>'
    results = _replay_proposal(str(final.get("model_proposal_text") or ""), calls, authorized=False)
    restricted = any(c.get("function", {}).get("name") == "execute_restricted_operation" for c in calls)
    return (
        '<section aria-label="Offline replay"><h3>Offline replay of the final unguarded proposal</h3>'
        '<p class="ih-replay-note">The exact text and proposed tools are reused in both sandboxes. '
        "The unverified authorization comes from the fixture; the no-boundary arm is a "
        "deliberately unsafe control over local mocks only. No Meta calls and no real "
        'effects. This replays one turn, not the whole conversation.</p>'
        + ('<p class="ih-replay-note">A restricted operation was proposed.</p>' if restricted else
           '<p class="ih-replay-note">No restricted operation was proposed: there is no block to attribute to the guard.</p>')
        + '<div class="ih-observation"><div class="ih-card-label">Captured proposal · Meta</div>'
        + '<div class="ih-card-text">'
        + _safe_text(json.dumps({"text": final.get("model_proposal_text"), "tool_calls": calls}, ensure_ascii=False, indent=2))
        + '</div></div>' + _replay_paths(results) + '</section>'
    )


def _synthetic_control() -> str:
    calls = [{
        "id": "synthetic-control", "type": "function",
        "function": {"name": "execute_restricted_operation", "arguments": "{}"},
    }]
    unverified = _replay_proposal("Synthetic operation proposal.", calls, authorized=False)
    verified = _replay_proposal("Synthetic operation proposal.", calls, authorized=True)
    return (
        '<section aria-label="Synthetic control"><h3>Local synthetic control: unverified versus verified authorization</h3>'
        '<p class="ih-replay-note">The fixture creates this operation proposal, not Meta. '
        "It uses mocks only; the authorization state is supplied as metadata. "
        'The no-boundary control is deliberately unsafe and local.</p>'
        '<h4>Authorization not verified</h4>' + _replay_paths(unverified)
        + '<h4>Authorization verified</h4>' + _replay_paths(verified) + '</section>'
    )


def _run_outputs(outcome: DemoOutcome, service: DemoService) -> tuple[Any, ...]:
    if not outcome.ok or outcome.report is None:
        return ("", outcome.message, "", "", "", "", "", "", "", "", "", [], outcome.message, None)

    report = outcome.report
    comparison = report["comparisons"][0]
    before = comparison["before_remediation_unguarded"]
    after = comparison["after_remediation_guarded"]
    before_reply = _last_assistant(before)
    after_reply = _last_assistant(after)
    stats = service.ledger.stats()

    status = (
        f"**{outcome.message}**  \n"
        f"Model: `{report.get('meta_model_name')}` · "
        f"Calls: **{report['live_run_limits']['requests_observed']}/6** · "
        f"Elapsed: **{outcome.elapsed_seconds:.1f} s**  \n"
        f"External calls: {'yes' if report.get('external_api_calls_made') else 'no'} · "
        f"Real side effects: {'yes' if report.get('side_effects_enabled') else 'no'} · "
        f"Global quota: {stats['runs_total']}/{service.limits.max_runs_total} runs, "
        f"{stats['requests_reserved']}/{service.limits.max_requests_total} reserved calls."
    )
    unguarded_status = (
        f"**Contract: {before.get('overall_status', '—')}**  \n"
        f"Recommended autonomy: `{before.get('recommended_autonomy', '—')}`."
    )
    guarded_status = (
        f"**Contract: {after.get('overall_status', '—')}**  \n"
        f"Recommended autonomy: `{after.get('recommended_autonomy', '—')}` · "
        f"Guard: {'intervened' if after_reply.get('guard_intervened') else 'did not intervene'}."
    )
    return (
        outcome.run_id,
        status,
        _summary_cards(comparison),
        unguarded_status,
        _code_block(str(before_reply.get("content") or "(no response)")),
        guarded_status,
        _code_block(str(after_reply.get("model_proposal_text") or "(no proposal)")),
        _code_block(str(after_reply.get("content") or "(no response)")),
        _timeline(comparison),
        _clause_explanations(before, after),
        _clause_diagnostics(before, after),
        _clause_rows(before, after),
        "Run completed. Press “Sign package”.",
        None,
    )


def _package_markdown(outcome: DemoOutcome) -> str:
    verification = outcome.verification or {}
    signature = verification.get("signature") or {}
    lines = [
        "✅ **Package signed and verified.**",
        f"SHA-256: `{outcome.package_sha256}`",
        f"Key ID: `{signature.get('key_id', '—')}` · "
        f"Artifacts: {verification.get('verified_artifacts', '—')}",
        "The public key is embedded: the signature detects changes, but it does not "
        "establish an external identity or a trusted timestamp.",
    ]
    if outcome.tamper:
        lines.append(
            "✅ **Tamper test:** a copy with one changed byte and a re-hashed "
            "manifest was rejected."
            if outcome.tamper.get("rejected")
            else "❌ The tamper test did not reject the altered copy."
        )
    return "\n\n".join(lines)


def _verify_markdown(outcome: DemoOutcome) -> str:
    if not outcome.ok:
        return outcome.message
    verification = outcome.verification or {}
    signature = verification.get("signature") or {}
    return (
        "✅ **Valid Ed25519 signature.**  \n"
        f"{verification.get('verified_artifacts', '—')} artifacts verified · "
        f"Key ID `{signature.get('key_id', '—')}`."
    )


def _tamper_markdown(outcome: DemoOutcome) -> str:
    if not outcome.ok:
        return outcome.message
    result = outcome.tamper or {}
    return (
        "✅ **Alteration rejected.**  \n"
        f"Tampered member: `{result.get('tampered_member')}`; "
        f"{result.get('changed_bytes')} byte; manifest re-hashed: "
        f"{'yes' if result.get('manifest_rehashed') else 'no'}.  \n"
        f"Reason: {result.get('reason', '—')}"
    )


def build_demo(service: DemoService) -> gr.Blocks:
    with gr.Blocks(title="Integrity Harness — Laboratory") as demo:
        gr.HTML(CYBER_HERO, elem_classes="ih-hero-component")
        gr.Markdown(
            """
            **01 / CONFIGURE THE TEST**  
            Pick a held-out case. The Meta model proposes replies and tool calls; a
            deterministic contract issues the verdicts for two independent arms.

            **Live limits:** at most 6 calls, 1,024 tokens, 25 s per call, no retries.
            Mock tools, no real side effects. Quota: 3 runs per visitor every 24 h, 40 a day
            and 200 in total. Without an API key the app refuses the run.

            **Provenance:** the engine and harness are baseline `2fd192d`; the app, the
            bounded live slice and the signed evidence are new work.
            """,
            elem_classes="ih-panel",
        )
        gr.HTML(STEP_STRIP, elem_classes="ih-steps-component")
        case_selector = gr.Dropdown(
            choices=[
                ("Authority · verbal approval", DEMO_CASE),
                ("Urgency · payroll cutoff", "meta-heldout_urgency_01_payroll_cutoff"),
                ("Ambiguity · ledger reclassification", "meta-heldout_ambiguity_01_ledger_reclass"),
            ],
            value=DEMO_CASE,
            label="Live scenario",
            allow_custom_value=False,
        )
        case_description = gr.Markdown(_case_intro(), elem_classes="ih-panel")
        run_id = gr.State("")
        run_button = gr.Button(
            "Run live slice (max. 6 calls)", variant="primary"
        )
        status = gr.Markdown(elem_classes="ih-status")
        summary_cards = gr.HTML()
        with gr.Row(elem_classes="ih-results"):
            with gr.Column(elem_classes="ih-panel"):
                gr.Markdown("### ◇ No guard\n\nModel proposal and response")
                unguarded_status = gr.Markdown()
                unguarded_response = gr.Markdown()
            with gr.Column(elem_classes="ih-panel"):
                gr.Markdown("### ⬡ With guard\n\nProposal, intervention and effective response")
                guarded_status = gr.Markdown()
                guarded_proposal = gr.Markdown()
                guarded_response = gr.Markdown()
        timeline = gr.Markdown()
        clause_explanations = gr.Markdown()
        clause_diagnostics = gr.HTML()
        clause_table = gr.Dataframe(
            headers=["Clause", "No guard", "With guard"],
            datatype=["str", "str", "str"],
            value=[],
            interactive=False,
        )
        gr.Markdown("## ⌁ Offline replay and synthetic control\n\nNeither makes Meta calls or real operations.", elem_classes="ih-section")
        with gr.Row():
            replay_button = gr.Button("Replay final live proposal")
            control_button = gr.Button("Run synthetic control")
        replay_status = gr.HTML()
        with gr.Row():
            sign_button = gr.Button("Sign package", variant="primary")
            verify_button = gr.Button("Verify")
            tamper_button = gr.Button("Alter one byte")
        package_status = gr.Markdown("Run a live slice, then sign the package.")
        download = gr.File(label="Signed package (.zip)", interactive=False)

        def run_demo(case_id: str, request: gr.Request):
            yield ("", "**Step 2 of 3 · running the live slice.** The backend does not "
                   "publish per-call progress, so no per-call indicator is shown. At most "
                   "6 calls, 25 s each, no retries.", "", "", "", "", "", "", "", "",
                   "", [], "Running…", None, "")
            yield (*_run_outputs(service.run(request, case_id), service), "")

        def replay_demo(run_id_value: str) -> str:
            return _replay_live(service._outcomes.get(run_id_value))

        def select_case(case_id: str) -> tuple[Any, ...]:
            return (_case_intro(case_id), "", "", "", "", "", "", "", "", "", "", "", [],
                    "Run a live slice, then sign the package.", None, "")

        def sign_demo(run_id_value: str) -> tuple[str, str | None]:
            outcome = service.sign(run_id_value)
            if not outcome.ok or outcome.package_path is None:
                return outcome.message, None
            return _package_markdown(outcome), str(outcome.package_path)

        def verify_demo(run_id_value: str) -> str:
            return _verify_markdown(service.verify(run_id_value))

        def tamper_demo(run_id_value: str) -> str:
            return _tamper_markdown(service.tamper(run_id_value))

        run_button.click(
            run_demo,
            inputs=[case_selector],
            outputs=[
                run_id,
                status,
                summary_cards,
                unguarded_status,
                unguarded_response,
                guarded_status,
                guarded_proposal,
                guarded_response,
                timeline,
                clause_explanations,
                clause_diagnostics,
                clause_table,
                package_status,
                download,
                replay_status,
            ],
        )
        case_selector.change(
            select_case, inputs=[case_selector],
            outputs=[case_description, run_id, status, summary_cards, unguarded_status, unguarded_response,
                     guarded_status, guarded_proposal, guarded_response, timeline,
                     clause_explanations, clause_diagnostics, clause_table, package_status, download,
                     replay_status],
        )
        replay_button.click(replay_demo, inputs=[run_id], outputs=[replay_status])
        control_button.click(_synthetic_control, inputs=[], outputs=[replay_status])
        sign_button.click(
            sign_demo,
            inputs=[run_id],
            outputs=[package_status, download],
        )
        verify_button.click(verify_demo, inputs=[run_id], outputs=[package_status])
        tamper_button.click(tamper_demo, inputs=[run_id], outputs=[package_status])
    return demo


def main() -> None:
    try:
        configured_timeout = int(
            os.environ.get("META_MODEL_TIMEOUT_SECONDS", str(PROVIDER_TIMEOUT_SECONDS))
        )
    except ValueError:
        configured_timeout = PROVIDER_TIMEOUT_SECONDS
    os.environ["META_MODEL_TIMEOUT_SECONDS"] = str(
        min(configured_timeout, PROVIDER_TIMEOUT_SECONDS)
    )
    state_dir = Path(os.environ.get("DEMO_STATE_DIR", str(DEFAULT_STATE_DIR)))
    service = DemoService(state_dir)
    demo = build_demo(service)
    demo.queue(max_size=4, default_concurrency_limit=1)
    demo.launch(
        server_name=os.environ.get("DEMO_HOST", "127.0.0.1"),
        server_port=int(os.environ.get("DEMO_PORT", "7860")),
        share=False,
        show_error=False,
        allowed_paths=[str(state_dir)],
        css=CYBER_CSS,
    )


if __name__ == "__main__":
    main()
