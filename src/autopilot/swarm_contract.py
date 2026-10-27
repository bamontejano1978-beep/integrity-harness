"""Swarm contract v0.2 — typed document for system-level restrictions (M2 P1).

Implements P1 of ``docs/product/evaluacion_enjambres_m2_plan_20260925_v0.1.md``.
The individual contract says "you must not X" to one agent; the swarm contract
says things about the **set** (design §7 of
``docs/product/evaluacion_enjambres_20260918_v0.1.md``). Deliberate design
decision inherited from that document: inter-agent restrictions live in this
*separate* document, so every individual contract stays self-sufficient and the
unchanged clause engine keeps judging individual trajectories. The deterministic
evaluation over the channel log lives in ``swarm_evaluator`` (P2).

Four clause families, evaluated per channel entry by ``swarm_evaluator``:

- ``prohibicion_interagentes``: no transport of the listed data labels on a
  declared edge (labels travel, never content — the same declarative-label
  rule the adapter-classified channel payloads carry).
- ``cardinalidad_handoff``: an upper bound on mediated handoffs for the whole
  run. v0.2 measures the mediated delegation as the handoff proxy; the live
  human-handoff resolution arrives with real interaction (M3).
- ``prohibicion_delegacion``: no mediated delegation from the listed agent
  (permission laundering: delegating to a peer a task your own contract bans).
- ``limite_global``: a shared budget ceiling (v0.2: delegation rounds).

Invariants (post-conditions over the complete log):

- ``label_context``: a data label never appeared in a forbidden context.
- ``no_repeated_delegation``: the same (source, target, turn) delegated in
  more than one round — the ping-pong / re-escalation loop.

Topology is declared (who may talk to whom); a channel entry outside the
declared edges is itself a recorded incident. Nothing here reads or writes
evidence files: this module is the typed document, its loader and validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import ExtensibleModel, SnakeName

SWARM_CONTRACT_VERSION = "0.2.0"

#: Channel entry kinds the evaluator may lawfully check. ``SwarmChannelEntry``
#: allows exactly these five; kept in sync here so a future kind fails loudly
#: instead of silently bypassing the swarm clauses.
CHANNEL_KINDS = (
    "peer_message",
    "subject_message",
    "delegation_open",
    "delegation_close",
    "incident",
    "bystander_observation",
    "task_prompt",
    "majority_answer",
    "subject_answer",
    "cascade_decision",
)

SwarmClauseType = Literal[
    "prohibicion_interagentes",
    "cardinalidad_handoff",
    "prohibicion_delegacion",
    "limite_global",
]
SwarmInvariantKind = Literal["label_context", "no_repeated_delegation"]


class SwarmTopologyEdge(ExtensibleModel):
    """One declared, directed channel edge (``desde`` may message ``hacia``)."""

    desde: SnakeName
    hacia: SnakeName


class SwarmTopology(ExtensibleModel):
    nodes: list[SnakeName] = Field(min_length=2)
    edges: list[SwarmTopologyEdge] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_edges(self) -> SwarmTopology:
        declared = set(self.nodes)
        for edge in self.edges:
            unknown = [name for name in (edge.desde, edge.hacia) if name not in declared]
            if unknown:
                raise ValueError(
                    f"topology edge references undeclared nodes: {sorted(set(unknown))}"
                )
        return self


#: The DSL's own convention spells the identifier ``id`` (see the individual
#: contract clauses); the typed field is ``clause_id`` and the loader below
#: normalizes the ``id`` spelling before validation, keeping the strict
#: unknown-fields guard of ``ExtensibleModel`` untouched.
_CLAUSE_ID_FIELD = Annotated[str, Field(min_length=1, max_length=120)]


class SwarmProhibicionInteragentes(ExtensibleModel):
    clause_id: _CLAUSE_ID_FIELD
    tipo: Literal["prohibicion_interagentes"]
    desde: SnakeName
    hacia: SnakeName
    #: Data labels whose transport on this edge is forbidden (labels, not content).
    dato: list[str] = Field(min_length=1)


class SwarmCardinalidadHandoff(ExtensibleModel):
    clause_id: _CLAUSE_ID_FIELD
    tipo: Literal["cardinalidad_handoff"]
    alcance: Literal["sistema"]
    maximo: Annotated[int, Field(ge=1)]
    #: Optional lower bound. ``minimo: 1`` is the swarm-level expression of the
    #: design's "exactly one agent must escalate to the human"; absent keeps the
    #: ceiling-only semantics, so existing v0.2 contracts do not change.
    minimo: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> SwarmCardinalidadHandoff:
        if self.minimo is not None and self.minimo > self.maximo:
            raise ValueError("minimo cannot exceed maximo")
        return self


class SwarmProhibicionDelegacion(ExtensibleModel):
    clause_id: _CLAUSE_ID_FIELD
    tipo: Literal["prohibicion_delegacion"]
    #: The agent that must not open mediated delegations.
    desde: SnakeName


class SwarmLimiteGlobal(ExtensibleModel):
    clause_id: _CLAUSE_ID_FIELD
    tipo: Literal["limite_global"]
    alcance: Literal["sistema"]
    #: Shared ceiling for the whole set (v0.2: delegation rounds).
    max_delegation_rounds: Annotated[int, Field(ge=1)]


SwarmClause = (
    SwarmProhibicionInteragentes
    | SwarmCardinalidadHandoff
    | SwarmProhibicionDelegacion
    | SwarmLimiteGlobal
)


class SwarmLabelContextInvariant(ExtensibleModel):
    kind: Literal["label_context"]
    #: The invariant_id is the caller-facing handle (clause_id analogue).
    invariant_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=60)]
    #: Contexts where this label must never appear (agent ids).
    forbidden_contexts: list[SnakeName] = Field(min_length=1)


class SwarmNoRepeatedDelegationInvariant(ExtensibleModel):
    kind: Literal["no_repeated_delegation"]
    invariant_id: Annotated[str, Field(min_length=1, max_length=120)]


SwarmInvariant = SwarmLabelContextInvariant | SwarmNoRepeatedDelegationInvariant


class SwarmContractDocument(ExtensibleModel):
    """The typed swarm contract (schema ``0.2``)."""

    schema_version: Literal["0.2"] = "0.2"
    swarm_contract_id: Annotated[str, Field(min_length=1, max_length=255)]
    topology: SwarmTopology
    clauses: list[SwarmClause] = Field(default_factory=list)
    invariants: list[SwarmInvariant] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_ids(self) -> SwarmContractDocument:
        clause_ids = [clause.clause_id for clause in self.clauses]
        if len(clause_ids) != len(set(clause_ids)):
            raise ValueError("swarm clause ids must be unique")
        invariant_ids = [invariant.invariant_id for invariant in self.invariants]
        if len(invariant_ids) != len(set(invariant_ids)):
            raise ValueError("swarm invariant ids must be unique")
        # The typo guard: a rule that references an undeclared agent can never
        # resolve against any run and would silently never fire. Rules must
        # speak about declared topology nodes; a run may then cover any subset
        # (one contract, several pairs of one pilot), with absent participants
        # declared as dormant rules in the system verdict.
        declared = set(self.topology.nodes)
        unknown = sorted(participants_of(self) - declared)
        if unknown:
            raise ValueError(
                f"rules reference agents absent from the topology: {unknown}"
            )
        return self


def _load_text_file(path: str | Path) -> tuple[str, Path]:
    resolved = Path(path)
    try:
        return resolved.read_text(encoding="utf-8"), resolved
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc


def _normalize_id_keys(document: Any) -> Any:
    """Rename the DSL's ``id`` spelling to the typed field, per section.

    The individual contracts spell it ``id``; the swarm contract keeps that
    authoring convention and normalizes before validation so the strict
    unknown-fields guard never sees the alias.
    """

    if not isinstance(document, dict):
        return document
    document = dict(document)
    for section, target in (("clauses", "clause_id"), ("invariants", "invariant_id")):
        entries = document.get(section)
        if isinstance(entries, list):
            normalized: list[Any] = []
            for entry in entries:
                if isinstance(entry, dict) and "id" in entry and target not in entry:
                    renamed = dict(entry)
                    renamed[target] = renamed.pop("id")
                    entry = renamed
                normalized.append(entry)
            document[section] = normalized
    return document


def load_swarm_contract(path: str | Path) -> SwarmContractDocument:
    """Load a swarm contract YAML with the hardened loader (no anchors/aliases)."""

    text, _resolved = _load_text_file(path)
    try:
        document = _normalize_id_keys(load_yaml_mapping(text))
        return SwarmContractDocument.model_validate(document)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def participants_of(contract: SwarmContractDocument) -> set[str]:
    """Every agent id the document speaks about (topology + clauses)."""

    names: set[str] = set(contract.topology.nodes)
    for clause in contract.clauses:
        if isinstance(clause, SwarmProhibicionInteragentes):
            names.update((clause.desde, clause.hacia))
        elif isinstance(clause, SwarmProhibicionDelegacion):
            names.add(clause.desde)
    for invariant in contract.invariants:
        if isinstance(invariant, SwarmLabelContextInvariant):
            names.update(invariant.forbidden_contexts)
    return names


def rule_participants_of(clause: SwarmClause | SwarmInvariant) -> set[str]:
    """Agents one rule references - used to declare dormant rules per run."""

    names: set[str] = set()
    if isinstance(clause, SwarmProhibicionInteragentes):
        names.update((clause.desde, clause.hacia))
    elif isinstance(clause, SwarmProhibicionDelegacion):
        names.add(clause.desde)
    elif isinstance(clause, SwarmLabelContextInvariant):
        names.update(clause.forbidden_contexts)
    return names


__all__ = [
    "CHANNEL_KINDS",
    "SWARM_CONTRACT_VERSION",
    "SwarmCardinalidadHandoff",
    "SwarmClause",
    "SwarmContractDocument",
    "SwarmLabelContextInvariant",
    "SwarmLimiteGlobal",
    "SwarmNoRepeatedDelegationInvariant",
    "SwarmProhibicionDelegacion",
    "SwarmProhibicionInteragentes",
    "SwarmTopology",
    "SwarmTopologyEdge",
    "load_swarm_contract",
    "participants_of",
    "rule_participants_of",
]
