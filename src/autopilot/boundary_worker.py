"""Minimal JSON-lines worker used by the supervised execution boundary.

The worker owns no tools. It can only request capabilities from its parent over
stdin/stdout. The parent is the reference monitor and may deny or terminate it.
"""

from __future__ import annotations

import json
import sys


def _read() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("protocol message must be an object")
    return value


def _write(value: dict) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        start = _read()
        if start.get("type") != "start":
            raise ValueError("first protocol message must be start")
        mode = start.get("worker_mode", "cooperative")
        requests = start.get("requests")
        if not isinstance(requests, list):
            raise ValueError("start.requests must be a list")

        for sequence, request in enumerate(requests, start=1):
            _write({"type": "request", "sequence": sequence, "request": request})
            response = _read()
            if response.get("type") != "decision" or response.get("sequence") != sequence:
                raise ValueError("invalid supervisor decision")
            if response.get("terminate") is True:
                return 20
            if response.get("stop") is True and mode == "cooperative":
                _write({"type": "complete", "reason": "stop_obeyed"})
                return 0

        _write({"type": "complete", "reason": "plan_exhausted"})
        return 0
    except (EOFError, json.JSONDecodeError, ValueError) as exc:
        _write({"type": "protocol_error", "reason": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
