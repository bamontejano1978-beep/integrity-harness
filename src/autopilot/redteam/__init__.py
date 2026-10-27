"""Conversational red-teaming harness — evidence ingest, classification and verdicts.

Implements the manual-mode tooling of ``src/docs/lora_redteam_protocol_20260926_v0.1.md``:

- ``whatsapp.py`` parses consumer WhatsApp exports (the evidence format);
- ``classify.py`` decides the automated subset of the A/B/C classes (§3);
- ``battery.py`` freezes stimulus decks, aligns exports and builds session reports;
- ``verdict.py`` holds the Wilson bounds and the §6 verdict arithmetic.

Honesty boundaries of the automation: it never rewrites stimuli, never authors
attack text (the operator writes the literals and the harness freezes them), and
never guesses the human judgments (A-r/A-c/A-x and violation levels are operator
annotations made at collection time). The unit of analysis is the whole system:
anything the runtime filters before the agent sees it classifies as B/C.
"""

from autopilot.redteam.battery import (
    CONTROL_CLOSE,
    CONTROL_OPEN,
    MECHANISMS,
    MIN_PACING_MIN,
    SILENCE_WINDOW_MIN,
    align_session,
    freeze_deck,
    mechanism_report,
    verify_deck,
)
from autopilot.redteam.classify import RUNTIME_TEMPLATE, classify_text
from autopilot.redteam.verdict import (
    FAIL,
    NOT_MEASURABLE,
    PASS,
    PASS_WITH_RESERVATIONS,
    mechanism_verdict,
    wilson_interval,
    wilson_upper_bound,
)
from autopilot.redteam.whatsapp import ChatMessage, parse_export, trim_after_last_message_at

__all__ = [
    "CONTROL_CLOSE",
    "CONTROL_OPEN",
    "MECHANISMS",
    "MIN_PACING_MIN",
    "NOT_MEASURABLE",
    "PASS",
    "PASS_WITH_RESERVATIONS",
    "RUNTIME_TEMPLATE",
    "SILENCE_WINDOW_MIN",
    "FAIL",
    "ChatMessage",
    "align_session",
    "classify_text",
    "freeze_deck",
    "mechanism_report",
    "mechanism_verdict",
    "parse_export",
    "trim_after_last_message_at",
    "verify_deck",
    "wilson_interval",
    "wilson_upper_bound",
]
