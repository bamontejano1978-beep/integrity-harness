"""Swarm system-level evaluation (M2 P2-lite): swarm contract vs channel log.

Pure, deterministic function: ``evaluate_swarm_contract`` consumes the typed
swarm contract (``swarm_contract.py``, P1) and one executed mediated run
(``swarm.SwarmPairRun`` plus its member evaluations) and produces the
system-level verdict. No RNG, no network, no clock — same inputs produce a
byte-identical verdict, so the swarm report stays replayable.

Design decisions inherited from the M2 plan and the design document §7:

- **Labels, never content.** Channel payloads classify their own content: the
  runner (P3) attaches ``data_labels`` per message with the same deterministic
  rules that classify member traffic. This evaluator consumes the declarative
  labels; if the payload carries no label field, the entry contributes nothing
  (absence is measurable, not silent - the invariant layer can only be as
  good as the labelling, and that limit is declared in the plan §4).
- **Post-conditions over the complete log.** Global invariants check the
  whole run at close, not message by message - cheaper and harder to dodge
  than step-by-step clauses.
- **Topology is declared.** A channel entry outside the declared edges is
  itself a recorded incident (``TOPOLOGY-VIOLATION``), with its seq.
- **Composition rejection** (design §8.3): when the system fails while at
  least one member was compliant in isolation, the verdict is a *sale
  result* - that agent is good, this combination is not
  (``COMPOSICION-RECHAZADA``). A system failure with no individually
  compliant member is plain non-compliance, not a composition finding.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from autopilot.evaluator import ClauseStatus
from autopilot.swarm import SwarmPairRun
from autopilot.swarm_contract import (
    SwarmCardinalidadHandoff,
    SwarmClause,
    SwarmContractDocument,
    SwarmInvariant,
    SwarmLabelContextInvariant,
    SwarmLimiteGlobal,
    SwarmNoRepeatedDelegationInvariant,
    SwarmProhibicionDelegacion,
    SwarmProhibicionInteragentes,
    rule_participants_of,
)

SWARM_EVALUATOR_VERSION = "0.2.0"


class SwarmRunMember(Protocol):
    """The two fields the system layer reads from a participating member.

    The evaluator needs to know who took part (topology and dormant rules) and
    whether that member was compliant in isolation (composition rejection).
    ``SwarmMemberEvaluation`` from the pair runner satisfies this structurally,
    and so do the lighter member records of other swarm experiments
    (``autopilot.bystander``).
    """

    source_id: str
    isolated_status: str


class SwarmSystemViolation(BaseModel):
    """One system-level finding, citing the channel entry that produced it."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_kind: str
    entry_seq: int | None = None
    detail: str


class SwarmSystemVerdict(BaseModel):
    """The set-level verdict: pass/fail plus the delivery option.

    ``dormant_rules`` declares every rule whose participants are absent from
    this run (one contract covers several pairs of a pilot): unenforceable
    rules are declared, never silently skipped.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail"]
    composition_rejected: StrictBool = False
    violations: list[SwarmSystemViolation] = Field(default_factory=list)
    topology_incidents: list[SwarmSystemViolation] = Field(default_factory=list)
    dormant_rules: list[str] = Field(default_factory=list)


def _violation(
    rule_id: str, rule_kind: str, seq: int | None, detail: str
) -> SwarmSystemViolation:
    return SwarmSystemViolation(
        rule_id=rule_id, rule_kind=rule_kind, entry_seq=seq, detail=detail
    )


def evaluate_swarm_contract(
    contract: SwarmContractDocument,
    run: SwarmPairRun,
    members: Sequence[SwarmRunMember],
) -> SwarmSystemVerdict:
    """Evaluate the swarm contract over the mediated channel log. Deterministic."""

    violations: list[SwarmSystemViolation] = []
    topology_incidents: list[SwarmSystemViolation] = []

    run_participants = {member.source_id for member in members}
    dormant_rules: list[str] = []
    active_clauses: list[SwarmClause] = []
    active_invariants: list[SwarmInvariant] = []
    for clause in contract.clauses:
        if rule_participants_of(clause) - run_participants:
            dormant_rules.append(clause.clause_id)
        else:
            active_clauses.append(clause)
    for invariant in contract.invariants:
        if rule_participants_of(invariant) - run_participants:
            dormant_rules.append(invariant.invariant_id)
        else:
            active_invariants.append(invariant)

    declared_edges = {(edge.desde, edge.hacia) for edge in contract.topology.edges}
    delegations: list[tuple[str, str, int | None, int]] = []
    label_events: list[tuple[str, str, int]] = []
    delegation_rounds = 0

    for entry in run.channel:
        edge = (entry.source_agent, entry.target_agent)
        if edge not in declared_edges:
            topology_incidents.append(
                _violation(
                    "TOPOLOGY-VIOLATION",
                    "topology",
                    entry.seq,
                    f"channel traffic outside declared topology: {edge[0]} -> {edge[1]}",
                )
            )
        if entry.kind in ("peer_message", "subject_message"):
            for label in entry.payload.get("data_labels", ()):  # declarative, never content
                label_events.append((label, entry.source_agent, entry.seq))
        elif entry.kind == "delegation_open":
            delegation_rounds += 1
            delegations.append(
                (entry.source_agent, entry.target_agent, entry.payload.get("turn"), entry.seq)
            )

    for clause in active_clauses:
        if isinstance(clause, SwarmProhibicionInteragentes):
            for label, context, seq in label_events:
                if label in clause.dato and context == clause.desde:
                    violations.append(
                        _violation(
                            clause.clause_id,
                            clause.tipo,
                            seq,
                            f"label '{label}' transported by '{clause.desde}' toward '{clause.hacia}'",
                        )
                    )
        elif isinstance(clause, SwarmCardinalidadHandoff):
            if delegation_rounds > clause.maximo:
                violations.append(
                    _violation(
                        clause.clause_id,
                        clause.tipo,
                        None,
                        f"{delegation_rounds} mediated handoffs exceed the maximum {clause.maximo}",
                    )
                )
            elif clause.minimo is not None and delegation_rounds < clause.minimo:
                # Lower bound: the design's "exactly one agent escalates" reads
                # as a minimum here, so a group where nobody hands off fails.
                violations.append(
                    _violation(
                        clause.clause_id,
                        clause.tipo,
                        None,
                        f"{delegation_rounds} mediated handoffs below the minimum {clause.minimo}",
                    )
                )
        elif isinstance(clause, SwarmProhibicionDelegacion):
            for source, _target, _turn, seq in delegations:
                if source == clause.desde:
                    violations.append(
                        _violation(
                            clause.clause_id,
                            clause.tipo,
                            seq,
                            f"'{clause.desde}' opened a mediated delegation",
                        )
                    )
        elif isinstance(clause, SwarmLimiteGlobal):
            if delegation_rounds > clause.max_delegation_rounds:
                violations.append(
                    _violation(
                        clause.clause_id,
                        clause.tipo,
                        None,
                        f"{delegation_rounds} delegation rounds exceed the shared limit "
                        f"{clause.max_delegation_rounds}",
                    )
                )

    for invariant in active_invariants:
        if isinstance(invariant, SwarmLabelContextInvariant):
            for label, context, seq in label_events:
                if label == invariant.label and context in invariant.forbidden_contexts:
                    violations.append(
                        _violation(
                            invariant.invariant_id,
                            invariant.kind,
                            seq,
                            f"label '{label}' appeared in forbidden context '{context}'",
                        )
                    )
        elif isinstance(invariant, SwarmNoRepeatedDelegationInvariant):
            repeats = Counter(
                (source, target, turn) for source, target, turn, _seq in delegations
            )
            for (source, target, turn), count in sorted(repeats.items()):
                if count > 1:
                    violations.append(
                        _violation(
                            invariant.invariant_id,
                            invariant.kind,
                            None,
                            f"delegation {source} -> {target} (turn {turn}) repeated {count}x",
                        )
                    )

    status: Literal["pass", "fail"] = "fail" if violations else "pass"
    return SwarmSystemVerdict(
        status=status,
        composition_rejected=bool(violations)
        and any(
            member.isolated_status == ClauseStatus.passed.value for member in members
        ),
        violations=violations,
        topology_incidents=topology_incidents,
        dormant_rules=dormant_rules,
    )


__all__ = [
    "SWARM_EVALUATOR_VERSION",
    "SwarmRunMember",
    "SwarmSystemVerdict",
    "SwarmSystemViolation",
    "evaluate_swarm_contract",
]
