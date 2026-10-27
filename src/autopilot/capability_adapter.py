"""Capability-call adapter for OpenAI-compatible model responses.

The adapter deliberately supports only a small, explicit schema and dispatches
through a caller-provided boundary. It never executes arbitrary model-provided
code or performs production side effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


def parse_tool_calls(response: dict[str, Any]) -> list[CapabilityCall]:
    """Extract strict OpenAI-style tool calls from a decoded response."""

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []
    raw_calls = message.get("tool_calls", [])
    if not isinstance(raw_calls, list):
        raise ValueError("message.tool_calls must be a list")
    calls: list[CapabilityCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            raise ValueError("tool call must be an object")
        function = raw.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool call function must be an object")
        call_id = raw.get("id")
        name = function.get("name")
        arguments = function.get("arguments", {})
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool call id must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ValueError("tool call function name must be a non-empty string")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError("tool call arguments must be valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("tool call arguments must be an object")
        calls.append(CapabilityCall(call_id, name, arguments))
    return calls


class CapabilityBoundary:
    """Deny-by-default local dispatcher for model-requested capabilities."""

    def __init__(self, allowed_tools: set[str], max_tool_calls: int):
        self.allowed_tools = frozenset(allowed_tools)
        self.max_tool_calls = max_tool_calls
        self.tool_calls = 0

    def dispatch(self, call: CapabilityCall) -> dict[str, Any]:
        if call.name not in self.allowed_tools:
            return {"call_id": call.call_id, "executed": False, "blocked": True, "reason": "tool_not_allowed"}
        if self.tool_calls >= self.max_tool_calls:
            return {"call_id": call.call_id, "executed": False, "blocked": True, "reason": "tool_budget_exhausted"}
        self.tool_calls += 1
        return {"call_id": call.call_id, "executed": True, "blocked": False, "result": {}}
