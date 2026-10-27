"""Offline import of synthetic StrategyAI sandbox telemetry into ``RawSession``.

This module reads local JSON only. It has no network client or endpoint support.
The input's synthetic/redaction flags are producer attestations, not proof; the
operator must approve the file before importing it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from autopilot.adapter import RawEvent, RawMessage, RawSession, RawToolCall
from autopilot.trajectory import parse_timestamp

SCHEMA_VERSION = "strategyai-sandbox-export-0.1"
IMPORTER_VERSION = "0.1.0"
MAX_INPUT_BYTES = 2_000_000
MAX_EVENTS = 1_000
MAX_MESSAGE_CHARS = 16_000
_ALIAS = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_RUN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)

_TOP_LEVEL_KEYS = {"schema_version", "classification", "redaction_reviewed", "run", "events"}
_RUN_KEYS = {
    "run_id", "scenario_id", "agent_alias", "agent_version", "brain_config_alias",
    "started_at", "ended_at", "complete",
}
_COMMON_EVENT_KEYS = {"event_id", "sequence", "occurred_at", "kind"}
_ALLOWED_KINDS = {"message", "tool_call", "tool_result", "control_event"}
_TOOL_VALUE_KEYS = {
    "resource_alias", "queue_alias", "queue", "amount_bucket", "approval_state", "decision", "scope",
}
_CONTROL_VALUE_KEYS = {
    "role_alias", "resource_alias", "decision", "approval_state",
    "licensed_adjuster_approval",
}
_ALLOWED_DECISIONS = {"allow", "deny", "unknown", "unavailable"}
_ALLOWED_OUTCOMES = {"success", "error", "blocked"}
_ALLOWED_AMOUNT_BUCKETS = {"lt_100", "100_500", "500_plus", "unknown"}
_ALLOWED_APPROVAL_STATES = {"approved", "not_approved", "unknown"}


class TelemetryImportError(ValueError):
    """Safe, value-free diagnostic for a rejected local telemetry export."""


def _fail(path: str, reason: str) -> None:
    raise TelemetryImportError(f"{path}: {reason}")


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _exact_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required)
    if missing:
        _fail(path, "missing required field(s): " + ", ".join(missing))
    if extra:
        # Never include untrusted values in diagnostics.
        _fail(path, "unsupported field(s): " + ", ".join(extra))


def _string(value: Any, path: str, *, max_length: int = 255) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        _fail(path, f"must be a non-empty string of at most {max_length} characters")
    return value


def _check_redacted_text(value: str, path: str) -> None:
    if any(pattern.search(value) for pattern in _SENSITIVE_TEXT_PATTERNS):
        _fail(path, "appears to contain a secret or personal identifier; redact before import")


def _alias(value: Any, path: str, *, pattern: re.Pattern[str] = _ALIAS) -> str:
    result = _string(value, path, max_length=128)
    if not pattern.fullmatch(result):
        _fail(path, "must be a simple synthetic alias")
    return result


def _scalar_map(value: Any, path: str, allowed_keys: set[str]) -> dict[str, bool | int | float | str]:
    mapping = _object(value, path)
    unsupported = sorted(set(mapping) - allowed_keys)
    if unsupported:
        _fail(path, "unsupported attribute(s): " + ", ".join(unsupported))
    result: dict[str, bool | int | float | str] = {}
    for key, item in mapping.items():
        if type(item) not in {bool, int, float, str}:
            _fail(f"{path}.{key}", "must be a scalar value")
        if type(item) is float and not math.isfinite(item):
            _fail(f"{path}.{key}", "must be a finite number")
        if type(item) is str and (not item or len(item) > 80):
            _fail(f"{path}.{key}", "must be a non-empty short alias/value")
        if type(item) is str:
            _check_redacted_text(item, f"{path}.{key}")
        if key in {"resource_alias", "queue_alias", "queue", "role_alias"} and type(item) is str:
            if not _ALIAS.fullmatch(item):
                _fail(f"{path}.{key}", "must be a synthetic alias")
        if key == "decision" and item not in _ALLOWED_DECISIONS:
            _fail(f"{path}.{key}", "must be an allowlisted decision category")
        if key == "scope" and item != "intake_only":
            _fail(f"{path}.{key}", "must be the intake_only scope category")
        if key == "outcome" and item not in _ALLOWED_OUTCOMES:
            _fail(f"{path}.{key}", "must be an allowlisted outcome category")
        if key == "amount_bucket" and item not in _ALLOWED_AMOUNT_BUCKETS:
            _fail(f"{path}.{key}", "must be an allowlisted amount bucket")
        if key == "approval_state" and item not in _ALLOWED_APPROVAL_STATES and type(item) is not bool:
            _fail(f"{path}.{key}", "must be an allowlisted approval category")
        if key == "licensed_adjuster_approval" and type(item) is not bool:
            _fail(f"{path}.{key}", "must be a boolean")
        result[key] = item
    return result


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TelemetryImportError(f"input: duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise TelemetryImportError(f"input: invalid JSON numeric constant: {value}")


def parse_export(payload: Any, *, contract_id: str, contract_version: str,
                 candidate_version: str) -> tuple[RawSession, dict[str, Any]]:
    """Validate an already-decoded export and produce a ``RawSession`` + report."""
    root = _object(payload, "export")
    _exact_keys(root, _TOP_LEVEL_KEYS, "export")
    if root["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", "unsupported schema version")
    if root["classification"] != "synthetic":
        _fail("classification", "only synthetic test data may be imported")
    if root["redaction_reviewed"] is not True:
        _fail("redaction_reviewed", "must be explicitly true before import")

    run = _object(root["run"], "run")
    _exact_keys(run, _RUN_KEYS, "run")
    run_id = _string(run["run_id"], "run.run_id", max_length=128)
    if not _RUN_ID.fullmatch(run_id):
        _fail("run.run_id", "must be a synthetic run alias")
    scenario_id = _alias(run["scenario_id"], "run.scenario_id")
    agent_alias = _alias(run["agent_alias"], "run.agent_alias")
    agent_version = _alias(run["agent_version"], "run.agent_version")
    brain_config_alias = _alias(run["brain_config_alias"], "run.brain_config_alias")
    if type(run["complete"]) is not bool:
        _fail("run.complete", "must be a boolean")
    started_at = _string(run["started_at"], "run.started_at")
    ended_at = _string(run["ended_at"], "run.ended_at")
    try:
        start_time = parse_timestamp(started_at)
        end_time = parse_timestamp(ended_at)
    except ValueError:
        _fail("run", "timestamps must be RFC 3339 with an explicit timezone")
    if end_time < start_time:
        _fail("run", "ended_at must not precede started_at")

    events = root["events"]
    if not isinstance(events, list) or not events:
        _fail("events", "must be a non-empty array")
    if len(events) > MAX_EVENTS:
        _fail("events", f"exceeds the limit of {MAX_EVENTS}")

    ids: set[str] = set()
    calls: dict[str, tuple[int, str, str, str, dict[str, Any]]] = {}
    results: dict[str, tuple[int, str, dict[str, Any]]] = {}
    messages: list[RawMessage] = []
    raw_events: list[RawEvent] = []
    parsed_times = []
    counts = {kind: 0 for kind in sorted(_ALLOWED_KINDS)}
    for index, raw_event in enumerate(events):
        path = f"events[{index}]"
        event = _object(raw_event, path)
        kind = event.get("kind")
        if type(kind) is not str or kind not in _ALLOWED_KINDS:
            _fail(f"{path}.kind", "unsupported event kind")
        required = _COMMON_EVENT_KEYS | {
            "message": {"role", "content"},
            "tool_call": {"call_id", "tool_name", "arguments"},
            "tool_result": {"call_id", "outcome", "result"},
            "control_event": {"name", "attributes"},
        }[kind]
        _exact_keys(event, required, path)
        event_id = _string(event["event_id"], f"{path}.event_id", max_length=255)
        if event_id in ids:
            _fail(f"{path}.event_id", "duplicate event id")
        ids.add(event_id)
        sequence = event["sequence"]
        if type(sequence) is not int or sequence != index + 1:
            _fail(f"{path}.sequence", "must be contiguous, strictly increasing from 1")
        occurred_at = _string(event["occurred_at"], f"{path}.occurred_at")
        try:
            moment = parse_timestamp(occurred_at)
        except ValueError:
            _fail(f"{path}.occurred_at", "must be RFC 3339 with an explicit timezone")
        if moment < start_time or moment > end_time:
            _fail(f"{path}.occurred_at", "must fall within the run interval")
        if parsed_times and moment < parsed_times[-1]:
            _fail(f"{path}.occurred_at", "events must be chronological by sequence")
        parsed_times.append(moment)
        counts[kind] += 1

        if kind == "message":
            role = event["role"]
            if type(role) is not str or role not in {"user", "assistant", "tool"}:
                _fail(f"{path}.role", "must be user, assistant, or tool")
            content = _string(event["content"], f"{path}.content", max_length=MAX_MESSAGE_CHARS)
            _check_redacted_text(content, f"{path}.content")
            messages.append(RawMessage(ref=event_id, role=role, observed_at=occurred_at,
                                       content=content, metadata={}))
        elif kind == "tool_call":
            call_id = _alias(event["call_id"], f"{path}.call_id")
            if call_id in calls:
                _fail(f"{path}.call_id", "duplicate tool call id")
            name = _string(event["tool_name"], f"{path}.tool_name", max_length=80)
            if not _TOOL_NAME.fullmatch(name):
                _fail(f"{path}.tool_name", "must be a snake_case tool alias")
            arguments = _scalar_map(event["arguments"], f"{path}.arguments", _TOOL_VALUE_KEYS)
            calls[call_id] = (index, event_id, occurred_at, name, arguments)
        elif kind == "tool_result":
            call_id = _alias(event["call_id"], f"{path}.call_id")
            if call_id not in calls or call_id in results:
                _fail(f"{path}.call_id", "result must match one earlier tool call exactly once")
            outcome = event["outcome"]
            if type(outcome) is not str or outcome not in _ALLOWED_OUTCOMES:
                _fail(f"{path}.outcome", "must be success, error, or blocked")
            result = _scalar_map(event["result"], f"{path}.result", _TOOL_VALUE_KEYS)
            results[call_id] = (index, outcome, result)
        else:
            name = _string(event["name"], f"{path}.name", max_length=80)
            if not _TOOL_NAME.fullmatch(name):
                _fail(f"{path}.name", "must be a snake_case event alias")
            attributes = _scalar_map(event["attributes"], f"{path}.attributes", _CONTROL_VALUE_KEYS)
            raw_events.append(RawEvent(ref=event_id, name=name, observed_at=occurred_at,
                                       payload=attributes))

    if set(calls) != set(results):
        _fail("events", "every tool call must have exactly one result")
    for call_id, (call_index, _, _, _, _) in calls.items():
        result_index = results[call_id][0]
        if result_index <= call_index:
            _fail("events", "every tool result must follow its tool call")
    raw_tool_calls: list[RawToolCall] = []
    for call_id, (_, ref, occurred_at, name, arguments) in calls.items():
        _, outcome, result = results[call_id]
        raw_tool_calls.append(
            RawToolCall(
                ref=ref,
                name=name,
                observed_at=occurred_at,
                arguments=arguments,
                result=result,
                success=(outcome == "success"),
            )
        )

    try:
        session = RawSession(
            session_id=run_id,
            contract_id=contract_id,
            contract_version=contract_version,
            candidate_version=candidate_version,
            complete=run["complete"],
            evaluated_at=ended_at,
            messages=messages,
            tool_calls=raw_tool_calls,
            events=raw_events,
        )
    except (ValidationError, ValueError):
        # Model diagnostics may contain rejected values; expose only a generic message.
        _fail("raw_session", "mapped export does not satisfy the canonical RawSession schema")

    report = {
        "schema_version": "strategyai-telemetry-import-report-0.1",
        "importer_version": IMPORTER_VERSION,
        "source_schema_version": SCHEMA_VERSION,
        "status": "accepted_complete" if session.complete else "accepted_incomplete",
        "run_id": run_id,
        "scenario_id": scenario_id,
        "agent_alias": agent_alias,
        "agent_version_alias": agent_version,
        "brain_config_alias": brain_config_alias,
        "run_complete_claimed": run["complete"],
        "run_complete_effective": session.complete,
        "event_count": len(events),
        "events_by_kind": {
            "conversation_message": counts["message"],
            "tool_invocation": counts["tool_call"],
            "tool_completion": counts["tool_result"],
            "control_signal": counts["control_event"],
        },
        "message_count": len(messages),
        "tool_call_count": len(raw_tool_calls),
        "control_event_count": len(raw_events),
        "dropped_fields_by_name": [],
        "limitations": [
            "Synthetic/redaction flags are producer attestations, not a DLP scan or source authentication.",
            "Importing telemetry does not prove internal permission enforcement or completeness beyond the supplied export.",
        ],
    }
    return session, report


def import_file(input_path: Path, output_path: Path, *, contract_id: str,
                contract_version: str, candidate_version: str) -> dict[str, Any]:
    """Import local JSON, writing RawSession JSON and a non-sensitive report sidecar."""
    if input_path.resolve() == output_path.resolve():
        raise TelemetryImportError("paths: input and output must be different")
    report_path = output_path.with_suffix(output_path.suffix + ".report.json")
    if output_path.exists() or report_path.exists():
        raise TelemetryImportError("output: destination or report already exists; refusing to overwrite")
    try:
        raw = input_path.read_bytes()
    except OSError:
        raise TelemetryImportError("input: cannot read local file") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise TelemetryImportError(f"input: exceeds the {MAX_INPUT_BYTES}-byte limit")
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_json_object_no_duplicates,
                             parse_constant=_reject_json_constant)
    except TelemetryImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise TelemetryImportError("input: invalid UTF-8 JSON or excessively nested structure") from None

    session, report = parse_export(payload, contract_id=contract_id,
                                   contract_version=contract_version,
                                   candidate_version=candidate_version)
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(session.model_dump_json(indent=2, exclude_none=True) + "\n")
        with report_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        # Do not attempt cleanup of an output that may have pre-existed due to a race.
        raise TelemetryImportError("output: could not create new output artifact(s)") from None
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import synthetic StrategyAI sandbox JSON locally (no network access).")
    parser.add_argument("input", type=Path, help="local synthetic export JSON")
    parser.add_argument("--output", required=True, type=Path, help="new RawSession JSON output")
    parser.add_argument("--contract-id", required=True, help="locally assigned contract identifier")
    parser.add_argument("--contract-version", required=True, help="contract SemVer")
    parser.add_argument("--candidate-version", required=True, help="pre-agreed evaluation alias SemVer")
    args = parser.parse_args(argv)
    try:
        report = import_file(args.input, args.output, contract_id=args.contract_id,
                             contract_version=args.contract_version,
                             candidate_version=args.candidate_version)
    except TelemetryImportError as exc:
        print(f"TELEMETRY IMPORT REJECTED: {exc}", file=sys.stderr)
        return 2
    report_path = args.output.with_suffix(args.output.suffix + ".report.json")
    print(f"IMPORTED {report['status']}: {report['event_count']} events -> {args.output}")
    print(f"REPORT: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
