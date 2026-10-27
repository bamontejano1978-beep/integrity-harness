"""E1 remediation — clause semantics in the classifier-visible state.

The formal run and the E2 diagnostic closed every decision-side remediation:
the confidence scalar cannot express "cannot decide" (E3 sweep) and the
evidence-sufficiency Noul carries no information beyond confidence (E2,
rho=0.946). E1 changes what the model sees instead: the audited clause's
normative semantics enter the state, exactly as the production guard would
screen tool-calls against a known contract.

Anchoring: each case carries its own ``domain`` and ``contract.clause_id``
(clause ids are reused across the four domain contracts with different
structured rules, so the case's contract — what the production guard knows —
is the authoritative source).

What enters the state (normative semantics only):
- clause id, type, sanitized description (first sentence + any sentence that
  states the rule);
- the structured rule (prohibited action+conditions / if-then / time limit);
- ``patterns_to_avoid`` (prohibition examples).

What NEVER enters the state (benchmark scaffolding and answers):
- ``x_benchmark`` (including ``ambiguous_conditions`` — quoting the frozen
  ambiguity would leak the experiment's frame into the sensor);
- sentences describing benchmark strata ("clear negatives…", "Constraint-
  reframing cases…") — they describe the dataset, not the rule;
- ground truth, rationale, arbiter labels, case ids.

Failure containment: a missing contract file or clause yields an empty
string, never an exception — a clause-aware run degrades to E2-parity
behaviour instead of crashing the pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from autopilot.jev_benchmark.generator import DEFAULT_CONTRACTS_DIR, DOMAIN_SPECS

# Sentences that describe the benchmark's construction rather than the rule.
_SCAFFOLD_MARKERS = (
    "clear negatives",
    "constraint-reframing cases",
    "assumption-based cases",
    "pre-compliance pattern",
    "escalation avoidance",
    "x_benchmark",
    "benchmark",
)

_RAW_DESCRIPTION_CACHE: dict[tuple[str, str], str] = {}
_CONTRACT_CACHE: dict[str, Any] = {}


def _sanitize_description(description: str) -> str:
    """Keep rule-stating sentences; drop benchmark-scaffolding sentences.

    The first sentence is always kept (it carries the normative rule); each
    following sentence is kept only when it does not reference the benchmark
    frame.
    """

    sentences = re.split(r"(?<=[.!?])\s+", description.strip())
    kept: list[str] = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        if index > 0 and any(marker in lowered for marker in _SCAFFOLD_MARKERS):
            continue
        kept.append(sentence.strip())
    return " ".join(kept)


def _raw_description(clause_id: str, domain: str, contracts_dir: Path) -> str:
    """The clause description as written in the domain's contract YAML."""

    key = (clause_id, domain)
    if key in _RAW_DESCRIPTION_CACHE:
        return _RAW_DESCRIPTION_CACHE[key]
    description = ""
    try:
        document = yaml.safe_load(
            (contracts_dir / DOMAIN_SPECS[domain]["file"]).read_text(encoding="utf-8")
        )
        for raw in document.get("clauses", []):
            if raw.get("id") == clause_id:
                description = str(raw.get("description", ""))
                break
    except (OSError, yaml.YAMLError, KeyError):
        description = ""
    _RAW_DESCRIPTION_CACHE[key] = description
    return description


def _clause_from_contract(clause_id: str, domain: str, contracts_dir: Path):
    """The typed clause model for a case's contract and clause id."""

    cache_key = domain
    if cache_key not in _CONTRACT_CACHE:
        from autopilot.contracts import load_contract

        try:
            _CONTRACT_CACHE[cache_key] = load_contract(
                contracts_dir / DOMAIN_SPECS[domain]["file"]
            )
        except (OSError, ValueError, KeyError):
            _CONTRACT_CACHE[cache_key] = None
    contract = _CONTRACT_CACHE[cache_key]
    if contract is None:
        return None
    for clause in contract.clauses:
        if getattr(clause, "id", "") == clause_id:
            return clause
    return None


def _clause_to_dict(clause: Any, domain: str, contracts_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": getattr(clause, "id", ""),
        "type": getattr(clause, "type", ""),
    }
    description = _sanitize_description(
        _raw_description(getattr(clause, "id", ""), domain, contracts_dir)
    )
    if description:
        data["description"] = description
    # ``if_`` exists because ``if`` is reserved; expose it as ``if``.
    for attr, key in (
        ("prohibition", "prohibition"),
        ("if_", "if"),
        ("then", "then"),
        ("requirement", "requirement"),
        ("trigger", "trigger"),
        ("window", "window"),
    ):
        value = getattr(clause, attr, None)
        if value is not None:
            data[key] = _jsonable(value)
    avoid = getattr(clause, "patterns_to_avoid", None)
    if avoid:
        data["patterns_to_avoid"] = list(avoid)
    return data


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)  # pydantic v2 models
    if callable(model_dump):
        return _jsonable(model_dump())
    value_attr = getattr(value, "value", None)  # enums
    if isinstance(value_attr, (str, int, float, bool)):
        return value_attr
    return str(value)


def _render_clause_block(clause: dict[str, Any]) -> str:
    """Deterministic text block: the normative rule the model must apply."""

    lines = [
        "APPLICABLE CONTRACT CLAUSE (normative rule to apply to the events below):",
        f"- clause {clause.get('id', '')} ({clause.get('type', '')}): "
        f"{clause.get('description', '')}",
    ]
    prohibition = clause.get("prohibition")
    if isinstance(prohibition, dict):
        lines.append(
            f"- PROHIBITED: {prohibition.get('action')} when "
            f"{prohibition.get('conditions')}"
        )
    if isinstance(clause.get("if"), dict) and isinstance(clause.get("then"), dict):
        condition, consequence = clause["if"], clause["then"]
        lines.append(
            f"- OBLIGATION: if {condition.get('event')} "
            f"({condition.get('conditions') or 'no extra conditions'}), then "
            f"{consequence.get('action')} within "
            f"{consequence.get('within_seconds')}s"
        )
    if isinstance(clause.get("trigger"), dict):
        trigger = clause["trigger"]
        requirement = clause.get("requirement")
        requirement = requirement if isinstance(requirement, dict) else {}
        lines.append(
            f"- OBLIGATION: if {trigger.get('event')}, then "
            f"{requirement.get('action')} within "
            f"{requirement.get('within_seconds', clause.get('window'))}s"
        )
    if "window" in clause:
        lines.append(f"- TIME LIMIT: {clause['window']}")
    avoid = clause.get("patterns_to_avoid")
    if avoid:
        lines.append("- Prohibited examples:")
        for pattern in avoid:
            lines.append(f"  * {pattern}")
    return "\n".join(lines)


def clause_semantics_block(case: Any, contracts_dir: Path | None = None) -> str:
    """The clause block for a benchmark case; empty string when unavailable.

    Only ``domain`` and ``contract.clause_id`` are read from the case —
    never its ground truth, rationale or labels.
    """

    clause_id = getattr(getattr(case, "contract", None), "clause_id", "")
    domain = getattr(case, "domain", "")
    if not clause_id or not domain:
        return ""
    contracts = contracts_dir or DEFAULT_CONTRACTS_DIR
    clause = _clause_from_contract(clause_id, domain, contracts)
    if clause is None:
        return ""
    return _render_clause_block(_clause_to_dict(clause, domain, contracts))


__all__ = ["clause_semantics_block"]
