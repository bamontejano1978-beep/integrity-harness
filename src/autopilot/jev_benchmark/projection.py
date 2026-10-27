"""The classifier-visible projection — one definition, shared by everything.

Before 23 Sep 2026 the benchmark handed sensors a projection containing only
``structured_view`` + ``natural_view``. Both are pure functions of
(domain, stratum class, boundary side, pressure), so 4,000 working cases
collapsed to **20 distinct state texts**, 100% of ambiguous cases shared their
exact text with a decidable one, and the pre-registered §6 criteria (defer the
undecidable without losing detection) were unsatisfiable for any model. The
defect and its measurements: ``src/docs/projection_validity_finding_20260923_v0.1.md``.

A benchmark may hide **labels** from the classifier. It must never hide the
**evidence**, which is the input the classifier is asked to judge. This module
makes that distinction executable:

- what enters the projection: the two views **and the audited trajectory**
  (observations with their attributes and timestamps, plus the recovery
  context — what a production guard actually sees);
- what never enters it: ground truth, arbiter rationale, stratum, mechanism,
  contract/clause identity, ``x_benchmark``;
- one renderer for the state text, used by both adapters and by the fine-tune
  dataset builder, so training and certification cannot drift apart again.

``PROJECTION_VERSION`` travels inside the observable, which means the replay
digest (``replay._view_digest``) changes whenever the projection changes:
transcripts recorded against an older projection are void by construction
rather than silently trusted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

PROJECTION_VERSION = "phase-a-0.2-evidence"

_RECOVERY_FIELDS = (
    "prior_failures",
    "human_approval",
    "consecutive_passing_runs",
    "cis",
    "rollback_available",
)


def _jsonable(value: Any) -> Any:
    """JSON-safe projection of pydantic models, enums and containers."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    value_attr = getattr(value, "value", None)  # enums
    if isinstance(value_attr, (str, int, float, bool)):
        return value_attr
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def evidence_projection(case: Any) -> dict[str, Any]:  # noqa: ANN401 - protocol boundary
    """The audited trajectory as evidence, with its log status and horizon.

    Exposes everything the deterministic arbiter reasons over — and that a
    production guard knows about an incident:

    - ``observations`` (kind, name, timestamp, attributes);
    - ``recovery`` (prior failures, human approval, consecutive passing runs,
      CIS, rollback availability);
    - ``complete`` — whether the log covers the whole audited window;
    - ``evaluated_at`` — the horizon the audited window closes at.

    ``complete``/``evaluated_at`` are not bookkeeping to be hidden: they are
    the *decisive* evidence in this benchmark. A log that ends early makes the
    absence of an action uninformative, which is exactly what separates a
    genuinely undecidable case from a violation — measured the hard way on 23
    Sep 2026 (``src/docs/projection_validity_finding_20260923_v0.1.md``):
    omitting them left clear positives and ambiguous cases projecting to the
    same text, and no classifier can be asked to separate identical inputs.

    Kept out on purpose: identifiers (``trajectory_id``, ``contract_id``,
    ``candidate_version``) — provenance, and the contract id is the
    benchmark's own scaffolding.
    """

    trajectory = getattr(case, "trajectory", None)
    if trajectory is None:
        return {
            "complete": None,
            "evaluated_at": "",
            "observations": [],
            "recovery": {},
        }
    observations: list[dict[str, Any]] = []
    for observation in getattr(trajectory, "observations", []) or []:
        observations.append(
            {
                "ref": str(getattr(observation, "ref", "")),
                "kind": _jsonable(getattr(observation, "kind", "")),
                "name": str(getattr(observation, "name", "")),
                "observed_at": str(getattr(observation, "observed_at", "")),
                "attributes": _jsonable(getattr(observation, "attributes", {}) or {}),
            }
        )
    recovery = _jsonable(getattr(trajectory, "recovery", None) or {})
    if not isinstance(recovery, dict):
        recovery = {}
    complete = getattr(trajectory, "complete", None)
    return {
        "complete": bool(complete) if complete is not None else None,
        "evaluated_at": str(getattr(trajectory, "evaluated_at", "")),
        "observations": observations,
        "recovery": recovery,
    }


def build_observable(case: Any) -> dict[str, Any]:  # noqa: ANN401 - protocol boundary
    """The protocol projection: handle + views + evidence (never labels)."""

    return {
        "case_id": getattr(case, "case_id", ""),
        "projection_version": PROJECTION_VERSION,
        "structured_view": _jsonable(getattr(case, "structured_view", {}) or {}),
        "natural_view": _jsonable(getattr(case, "natural_view", []) or []),
        "evidence": evidence_projection(case),
    }


# ---------------------------------------------------------------------------
# Rendering (shared by the Jev adapter, the Laya adapter and the trainer)
# ---------------------------------------------------------------------------


def _render_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_structured(structured: Mapping[str, Any]) -> str:
    """Compact deterministic rendering of the structured view."""

    lines: list[str] = []
    for key, value in structured.items():
        if isinstance(value, Mapping):
            rendered = ", ".join(f"{k}={_render_value(v)}" for k, v in value.items())
        elif isinstance(value, (list, tuple)):
            rendered = ", ".join(_render_value(item) for item in value)
        else:
            rendered = _render_value(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


def render_conversation(natural: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"{message.get('role', '?')}: {message.get('text', '')}" for message in natural
    )


def render_evidence(evidence: Mapping[str, Any]) -> str:
    """Deterministic rendering of the audited trajectory.

    Every observation is rendered with its kind, name, timestamp and
    attributes (sorted by key), so two cases project to the same text only if
    their evidence is identical.
    """

    lines = ["Recorded evidence (the audited trajectory):"]
    complete = evidence.get("complete")
    if complete is None:
        status = "unspecified"
    else:
        status = "complete" if complete else "incomplete"
    evaluated_at = str(evidence.get("evaluated_at", "") or "")
    lines.append(
        f"Log status: {status}"
        + (f" (audited window evaluated at {evaluated_at})" if evaluated_at else "")
    )
    observations = list(evidence.get("observations") or [])
    if not observations:
        lines.append("- no observations recorded")
    for observation in observations:
        attributes = observation.get("attributes") or {}
        if isinstance(attributes, Mapping) and attributes:
            rendered = ", ".join(
                f"{key}={_render_value(attributes[key])}" for key in sorted(attributes)
            )
        else:
            rendered = "no attributes"
        lines.append(
            f"- {observation.get('ref', '')} [{observation.get('kind', '')}] "
            f"{observation.get('name', '')} at {observation.get('observed_at', '')}: "
            f"{rendered}"
        )
    recovery = evidence.get("recovery") or {}
    if isinstance(recovery, Mapping):
        ordered_keys = [key for key in _RECOVERY_FIELDS if key in recovery]
        ordered_keys += sorted(key for key in recovery if key not in _RECOVERY_FIELDS)
        rendered_recovery = ", ".join(
            f"{key}={_render_value(recovery[key])}" for key in ordered_keys
        )
    else:  # pragma: no cover - defensive
        rendered_recovery = _render_value(recovery)
    lines.append(f"Recovery context: {rendered_recovery or 'none'}")
    return "\n".join(lines)


def render_state(observable: Mapping[str, Any], *, instructions: str = "") -> str:
    """The classifier-visible state text: views, evidence, conversation, task."""

    sections = [
        "Structured event view:\n"
        + render_structured(observable.get("structured_view", {}) or {}),
        render_evidence(observable.get("evidence", {}) or {}),
        "Conversation:\n"
        + render_conversation(observable.get("natural_view", []) or []),
    ]
    text = "\n\n".join(sections)
    if instructions:
        text += "\n\n" + instructions
    return text


__all__ = [
    "PROJECTION_VERSION",
    "build_observable",
    "evidence_projection",
    "render_conversation",
    "render_evidence",
    "render_state",
    "render_structured",
]
