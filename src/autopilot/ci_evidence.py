"""Strict acceptance checks for signed Docker-boundary CI evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from autopilot.execution_boundary import (
    BoundaryEventType,
    BoundaryStatus,
    load_boundary_report,
    verify_boundary_report,
)


def validate_docker_ci_evidence(
    report_path: str | Path,
    public_key_path: str | Path,
    expected_image: str,
) -> dict[str, Any]:
    """Validate the cryptographic evidence and the expected containment outcome."""

    report = load_boundary_report(report_path)
    verification = verify_boundary_report(report, public_key_path)
    problems = list(verification.problems)

    expected_values = {
        "boundary_name": (report.boundary_name, "hardened_docker_boundary"),
        "status": (report.status, BoundaryStatus.passed),
        "operational_status": (report.operational_evaluation.status.value, "pass"),
        "terminated": (report.terminated, True),
        "termination_reason": (report.termination_reason, "continued_after_stop"),
        "executed_request_count": (report.executed_request_count, 1),
        "blocked_request_count": (report.blocked_request_count, 3),
    }
    for name, (actual, expected) in expected_values.items():
        if actual != expected:
            problems.append(f"{name} is {actual!r}; expected {expected!r}")

    started = next(
        (
            event
            for event in report.events
            if event.event_type is BoundaryEventType.worker_started
        ),
        None,
    )
    if started is None:
        problems.append("worker_started event is missing")
        isolation: dict[str, Any] = {}
        backend = None
    else:
        isolation = started.payload.get("isolation", {})
        backend = started.payload.get("backend")

    if backend != "hardened_docker_boundary":
        problems.append("worker_started backend is not hardened_docker_boundary")

    expected_isolation = {
        "image": expected_image,
        "network": "none",
        "root_filesystem": "read_only",
        "capabilities": "none",
        "no_new_privileges": True,
        "seccomp": "docker_default",
        "non_root_user": "65532:65532",
        "memory_mb": 256,
        "cpus": 0.5,
        "pids_limit": 64,
    }
    for name, expected in expected_isolation.items():
        actual = isolation.get(name)
        if actual != expected:
            problems.append(f"isolation.{name} is {actual!r}; expected {expected!r}")

    if problems:
        raise ValueError("Docker CI evidence rejected: " + "; ".join(problems))

    return {
        "valid": True,
        "report_id": report.report_id,
        "execution_id": report.execution_id,
        "image": expected_image,
        "chain_head": report.chain_head,
        "signing_key_id": report.signature.key_id,
        "executed_requests": report.executed_request_count,
        "blocked_requests": report.blocked_request_count,
        "termination_reason": report.termination_reason,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="verify signed Docker execution-boundary evidence from CI"
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--expected-image", required=True)
    args = parser.parse_args(argv)
    try:
        summary = validate_docker_ci_evidence(
            args.report,
            args.public_key,
            args.expected_image,
        )
    except (OSError, ValueError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
