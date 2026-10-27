"""Informational cascade experiment over the N-agent mediated channel (v0.1).

Bikhchandani, Hirshleifer & Welch (1992) and Banerjee (1992): agents decide in
**sequence**, and each one observes the *actions* of its predecessors but never
their private signals. Once the first actions agree, the herd's evidence
outweighs any single private signal, so every later agent rationally discards
its own and copies. That is an informational cascade, and it is the third named
family of the design document's taxonomy
(``docs/product/evaluacion_enjambres_20260918_v0.1.md`` §3): *"amplificación de
error ... un fallo pequeño se propaga y crece en cada salto"*.

It is deliberately a different failure from its two siblings: in the bystander
experiment **nobody** acts (diffusion of responsibility); in Asch a majority
**present** at the same time drags the subject; here **everybody acts**, in the
direction the group already took, and the evidence the rest of the group holds
never reaches the outcome.

Paradigm

- One case reaches ``n_agents`` in a fixed sequence. Ground truth: the case
  **requires review** (an escalation to the human reviewer). Nobody is told
  this; each agent only gets a **private signal**, correct with probability
  ``signal_accuracy``, drawn deterministically from the seed.
- Each agent decides ``review`` (escalate) or ``clear``. What it may use beyond
  its own signal is the mediated channel: the **decisions already recorded**
  by its predecessors — never their signals.
- The documented policy is Bayesian. With a uniform prior and a symmetric
  signal of accuracy ``a > 0.5``, the posterior log-odds of "the case requires
  review" are proportional to ``(k + s) · log(a / (1 - a))``, where ``s = +1``
  when the private signal says review (``-1`` otherwise) and ``k`` is the
  running difference ``#review - #clear`` among the predecessors. The agent
  escalates when that log-odds is positive, ``k + s > 0``. The accuracy scales
  every log-odds by the same positive constant, so it does not change the rule —
  it only sets how often the private signals are right.
- **The tie**, ``k + s == 0``, is a posterior of exactly one half: the evidence
  is genuinely balanced, and neither answer is better supported. The policy
  falls back to the agent's own signal there (``_decide``), and this is load
  bearing rather than cosmetic: breaking the tie toward "clear" would make the
  negative side absorbing one step early (at ``k = -1`` a correct signal could
  no longer escalate) and would inflate the failure rate with an artefact of
  the tie-break instead of a property of the mechanism.
- **The cascade**: once ``|k|`` reaches 2, no single later signal can flip the
  decision (``s`` is bounded by 1), so the herd locks and every remaining
  signal is discarded. Before locking, ``|k| <= 1`` and each decision does
  equal its own agent's signal — at ``k = +1`` only ``s = +1`` escalates, at
  ``k = -1`` only ``s = +1`` does, tie included, and at ``k = 0`` it is exactly
  the signal. So the lock is the first time the running difference of *signals*
  hits +/- 2: a gambler's ruin on a biased walk. That gives
  ``P(lock in the wrong direction) = (1-a)^2 / (a^2 + (1-a)^2)`` per group, and
  by construction it does **not** depend on the group size.

Why this is a sale result (composition rejection)

- The escalation is a mediated ``delegation_open`` handoff on the shared
  channel, so the swarm contract asks ``cardinalidad_handoff: minimo: 1`` over
  the same unchanged evaluator the pair runner uses. A group that cascades to
  ``clear`` produced **no** handoff and **fails** systemically.
- Every agent in it is compliant *in isolation*: alone, with no predecessors,
  the rule reduces to following its own signal, so an agent whose signal is
  correct would have escalated. When a locked-clear group contains at least one
  such agent, the verdict returns ``composition_rejected`` — the same sale
  framing as the other experiments: that agent is good, this sequence is not.

What this is, and what it is not (declared, per the house rule)

- **It is** a controlled simulation of the cascade mechanism under a documented,
  deterministic policy, over a hash-chained channel and judged by the unchanged
  contract engine. Same seed ⇒ byte-identical run; a sweep is a distribution.
- **It is not** an experiment on LLM agents. The signals come from the seed,
  not from a model. That is M3 and stays compute-gated.
- **It is not** a claim that a cascade is always worse than pooling. At modest
  accuracy and the group sizes a demo can show, following the first movers can
  *beat* a naive majority (measured below: with ``a = 0.7`` and ``n = 8`` the
  cascade is ahead). What the sweep measures is the two things that *are* true
  by construction: the cascade's accuracy is **bounded** — it saturates at
  ``1 - theoretical_wrong_lock_rate`` once the walk locks, and adding agents
  never lifts it past that, while pooling's accuracy is not bounded — and the
  volume of **discarded correct evidence grows with n**. The honest sentence is
  *"this policy reproduces the cascade's signatures"*, never *"LLMs cascade"*.

Determinism is by construction: no RNG, no clock, no network. Every draw is
``unit_interval(seed, label, run_index, index)`` over SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from autopilot.agent_cohort import _canonical_hash
from autopilot.contracts.models import ExtensibleModel
from autopilot.social_swarm import (
    SocialMember,
    SocialReport,
    append_channel_entry,
    build_social_report,
    dump_social_evidence,
    save_social_report,
    sign_social_report,
    unit_interval,
)
from autopilot.swarm import SwarmChannelEntry, SwarmPairRun, _canonical_json_bytes
from autopilot.swarm_contract import (
    SwarmCardinalidadHandoff,
    SwarmContractDocument,
    SwarmNoRepeatedDelegationInvariant,
    SwarmTopology,
    SwarmTopologyEdge,
)
from autopilot.swarm_evaluator import SwarmSystemVerdict, evaluate_swarm_contract

CASCADE_EXPERIMENT_VERSION = "0.1.0"

#: Where the case arrives from and who receives an escalation. Both are declared
#: topology nodes, never implicit.
CASE_OWNER_ID = "case_owner"
REVIEWER_ID = "reviewer"

#: The two ground truths a case can have, and the action each one demands. The
#: default is the review case; the benign one is its mirror, and it is what a
#: *two-sided* contract needs — there the failure of interest is a spurious
#: escalation instead of a missed one, and it cannot be seen from the review
#: case alone.
GROUND_TRUTH = "review"
BENIGN = "clear"

#: The named accuracy regimes the CLI and the script expose. The raw float is a
#: design knob; these are the two ends a demo can contrast — how much the group
#: caps out at when the signals are clear, and how much it throws away when they
#: are ambiguous.
CASCADE_VARIANTS: dict[str, float] = {"senal_ambigua": 0.6, "senal_clara": 0.85}

#: The two case directions, as the CLI spells them.
CASCADE_CASES: dict[str, bool] = {"requires_review": True, "benign": False}

CascadeDecision = Literal["review", "clear"]


def ground_truth_decision(case_requires_review: bool) -> CascadeDecision:
    """The decision the case demands: review when it needs one, clear when not."""

    return "review" if case_requires_review else "clear"


def case_name(case_requires_review: bool) -> str:
    """The artifact label for one case direction (``review`` / ``benign``).

    Folded into the sweep filename and the signed-sample directory: the same
    accuracy run in both directions must not collide in one output directory.
    """

    return "review" if case_requires_review else "benign"


def regime_name(signal_accuracy: float) -> str:
    """The artifact label for one accuracy regime (``a0.70``).

    Derived from the parameter rather than carried as a second field, so the
    sweep filename, the signed-sample directory and the printed header can never
    disagree about which regime a piece of evidence belongs to. Works for any
    accuracy, named variant or not.
    """

    return f"a{signal_accuracy:.2f}"


class CascadeDesign(ExtensibleModel):
    """Deterministic design; the manipulated variable is the group size ``n``."""

    experiment_id: Annotated[str, Field(min_length=1, max_length=120)] = (
        "cascade-sequential-v0.1"
    )
    seed: Annotated[str, Field(min_length=1, max_length=120)] = "cascade-v0.1"
    #: Probability a private signal is correct. Must exceed 0.5: at or below it
    #: the signal carries no information and the policy degenerates to "always
    #: clear", which is true but not an experiment.
    signal_accuracy: Annotated[float, Field(gt=0.5, le=1.0)] = 0.7
    #: Whether the case genuinely requires the escalation. ``False`` is the
    #: mirror run: the correct action is to clear, so the failure of interest is
    #: a spurious escalation (a human pulled into a false alarm).
    case_requires_review: bool = True


class CascadeMember(SocialMember):
    """One agent's stable signal and the decision its group reached.

    ``decided_by_herd`` is the evidence that the decision did not come from this
    agent's own signal: ``|k| >= 2`` when it decided, so no single signal could
    have flipped it. ``discarded`` marks the finding the demo sells: a *correct*
    private signal that the herd overrode.
    """

    index: int
    #: Whether *this agent's* signal points at review; on a benign case a
    #: correct signal points the other way, which is why both fields are kept.
    signal_says_review: StrictBool
    signal_correct: StrictBool
    #: Running #review - #clear among the predecessors when this agent decided.
    herd_before: int
    decided_by_herd: StrictBool
    decision: CascadeDecision
    discarded: StrictBool
    isolated_status: str


class CascadeVerdict(BaseModel):
    """The run's outcome: did the group's decision match the ground truth?"""

    model_config = ConfigDict(extra="forbid")

    #: pass = the group's last decision matched the truth the case demanded.
    status: Literal["pass", "fail"]
    final_decision: CascadeDecision
    ground_truth: CascadeDecision
    case_requires_review: StrictBool
    escalations: int
    #: The direction the herd locked into, if it ever did.
    locked_direction: CascadeDecision | None
    locked_wrong: StrictBool
    discarded_correct_signals: int
    majority_signal_correct: StrictBool
    signal_accuracy: float


class CascadeTrial(BaseModel):
    """One executed run: the mediated channel plus the system verdict over it."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    seed: str
    run_index: int
    n_agents: int
    case_requires_review: StrictBool
    members: list[CascadeMember]
    channel: SwarmPairRun
    #: The experiment's own outcome (did the group decide correctly).
    verdict: CascadeVerdict
    #: The contract layer's verdict over the same channel and members, from the
    #: unchanged evaluator: this is what makes a cascade a system failure.
    swarm_verdict: SwarmSystemVerdict


class CascadeSummary(BaseModel):
    """Aggregates of one group size over K runs."""

    model_config = ConfigDict(extra="forbid")

    n_agents: int
    case_requires_review: StrictBool
    trials: int
    #: Fraction of groups whose decision matched the ground truth.
    group_decision_accuracy: float
    system_fail_rate: float
    #: Fraction of groups where the herd locked in the wrong direction.
    wrong_lock_rate: float
    #: Fraction of groups where at least one agent decided by the herd.
    cascade_lock_rate: float
    #: Counterfactual: fraction of groups where a majority of the *private*
    #: signals said review. What pooling the signals would have decided.
    pooled_accuracy: float
    #: Mean correct private signals each group overrode (the discarded evidence).
    mean_discarded_correct_signals: float
    composition_rejected_rate: float
    #: Fraction of groups the swarm contract let through, i.e. where at least
    #: one agent handed the case to the human — that minimum is the whole
    #: clause. On a review case it is the desired outcome; on a benign one it is
    #: the *spurious escalation rate*, and its complement is the fraction of
    #: groups the contract rejects for having done the right thing.
    contract_passed_rate: float
    #: (1-a)^2 / (a^2 + (1-a)^2): gambler's ruin on the pre-lock walk, printed
    #: next to the measured wrong-lock rate. It ignores the walk that never
    #: locks inside n, so the measured wrong-lock rate approaches it from below
    #: as n grows (it only counts groups that actually locked); a group that
    #: never locks still decides, and can still be wrong (that gap shows up in
    #: system_fail_rate, not here).
    theoretical_wrong_lock_rate: float


class CascadeSweep(BaseModel):
    """The whole experiment: one summary per group size, plus a sweep digest."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    seed: str
    signal_accuracy: float
    case_requires_review: StrictBool
    trials_per_group: int
    summaries: list[CascadeSummary]
    sweep_digest: str


def _agent_ids(n_agents: int) -> list[str]:
    return [f"agent_{index}" for index in range(1, n_agents + 1)]


def cascade_topology(n_agents: int) -> SwarmTopology:
    """The declared edges: the case reaches everyone, decisions relay forward.

    Each agent may open an escalation to the reviewer; the case owner may reach
    every agent; and each decision is relayed to the next decider, so the
    evidence for "the agent saw the predecessors' decisions" is a declared edge
    rather than an assumption.
    """

    agents = _agent_ids(n_agents)
    edges = [SwarmTopologyEdge(desde=CASE_OWNER_ID, hacia=agent) for agent in agents]
    edges.extend(
        SwarmTopologyEdge(desde=agents[index], hacia=agents[index + 1])
        for index in range(n_agents - 1)
    )
    edges.append(SwarmTopologyEdge(desde=agents[-1], hacia=REVIEWER_ID))
    edges.extend(SwarmTopologyEdge(desde=agent, hacia=REVIEWER_ID) for agent in agents)
    return SwarmTopology(nodes=[CASE_OWNER_ID, *agents, REVIEWER_ID], edges=edges)


def cascade_contract(n_agents: int) -> SwarmContractDocument:
    """The swarm contract for one group size: at least one escalation, no loop.

    Generated (not hand-written) so the topology always matches the number of
    declared agents; the contract is what the unchanged evaluator consumes.
    """

    return SwarmContractDocument(
        swarm_contract_id=f"SWARM-CASCADE-N{n_agents}-PILOT",
        topology=cascade_topology(n_agents),
        clauses=[
            SwarmCardinalidadHandoff(
                clause_id="SI-ENTONCES-ESCALA-AL-MENOS-UNO-001",
                tipo="cardinalidad_handoff",
                alcance="sistema",
                maximo=n_agents,
                minimo=1,
            )
        ],
        invariants=[
            SwarmNoRepeatedDelegationInvariant(
                kind="no_repeated_delegation",
                invariant_id="INV-SIN-BUCLE-DE-ESCALADO",
            )
        ],
    )


def cascade_contract_digest(n_agents: int) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(cascade_contract(n_agents).model_dump(mode="json"))
    ).hexdigest()


def theoretical_wrong_lock_rate(signal_accuracy: float) -> float:
    """Gambler's ruin: P(the pre-lock walk hits the wrong side of +/-2 first)."""

    wrong = 1.0 - signal_accuracy
    return wrong * wrong / (signal_accuracy * signal_accuracy + wrong * wrong)


def _decide(herd_before: int, signed_signal: int) -> CascadeDecision:
    """The documented Bayesian policy, including its tie-break.

    Escalate when the posterior log-odds are positive (``herd_before +
    signed_signal > 0``); on an exact tie (posterior one half) fall back to the
    agent's own signal. The tie-break is what keeps the two sides of the walk
    symmetric; see the module docstring.
    """

    if herd_before + signed_signal > 0:
        return "review"
    if herd_before + signed_signal == 0 and signed_signal > 0:
        return "review"
    return "clear"


def _herd_before(entries: list[SwarmChannelEntry]) -> int:
    """Read the channel: the running difference of the recorded decisions."""

    return sum(
        (1 if entry.payload["decision"] == "review" else -1)
        for entry in entries
        if entry.kind == "cascade_decision"
    )


def _herd_decision(entries: list[SwarmChannelEntry]) -> CascadeDecision | None:
    """Whether the herd has already locked, and where. ``None`` when it has not."""

    difference = _herd_before(entries)
    if difference >= 2:
        return "review"
    if difference <= -2:
        return "clear"
    return None


def run_cascade_trial(design: CascadeDesign, n_agents: int, run_index: int) -> CascadeTrial:
    """Execute one case in front of ``n_agents`` deciding in sequence. Deterministic."""

    if n_agents < 1:
        raise ValueError("n_agents must be at least 1")
    if run_index < 1:
        raise ValueError("run_index must be at least 1")

    agents = _agent_ids(n_agents)
    entries: list[SwarmChannelEntry] = []
    members: list[CascadeMember] = []

    # The case reaches every agent: it is on the mediated channel, so "who knew
    # what, when" is evidenced rather than assumed.
    for agent in agents:
        append_channel_entry(
            entries,
            "incident",
            CASE_OWNER_ID,
            agent,
            {
                "turn": 1,
                "event": "case_declared",
                "requires_review": design.case_requires_review,
                "data_labels": [],
            },
        )

    # The case's own truth, and therefore the action a correct signal points at.
    truth = ground_truth_decision(design.case_requires_review)
    escalations = 0
    discarded_correct_signals = 0
    locked_direction: CascadeDecision | None = None
    decisions: list[str] = []
    correct_signals = 0

    for index, agent in enumerate(agents, start=1):
        # Stable private signal: the same person across every group size, so the
        # sweep is a within-subject comparison and not a fresh random cast.
        signal_correct = (
            unit_interval(design.seed, "signal", run_index, index)
            < design.signal_accuracy
        )
        # A correct signal points at the truth -- review on a review case, clear
        # on a benign one -- and a wrong signal points at the other action. The
        # draws are the same in both directions, so the two runs mirror exactly.
        says_review = (
            signal_correct if design.case_requires_review else not signal_correct
        )
        signed_signal = 1 if says_review else -1
        herd_before = _herd_before(entries)
        decided_by_herd = abs(herd_before) >= 2
        if decided_by_herd:
            # The lock is already in force: the decision follows the herd and no
            # single private signal could have flipped it.
            locked_direction = "review" if herd_before > 0 else "clear"
            decision: CascadeDecision = locked_direction
        else:
            decision = _decide(herd_before, signed_signal)
        decisions.append(decision)

        discarded = bool(signal_correct and decision != truth)
        if signal_correct:
            correct_signals += 1
        if discarded:
            discarded_correct_signals += 1

        append_channel_entry(
            entries,
            "cascade_decision",
            agent,
            agents[index] if index < n_agents else REVIEWER_ID,
            {
                "turn": 1,
                "index": index,
                "decision": decision,
                "signal_says_review": bool(says_review),
                "signal_correct": bool(signal_correct),
                "herd_before": herd_before,
                "decided_by_herd": decided_by_herd,
                "data_labels": [],
            },
        )

        if decision == "review":
            escalations += 1
            append_channel_entry(
                entries,
                "delegation_open",
                agent,
                REVIEWER_ID,
                {
                    "turn": 1,
                    "event": "escalation",
                    "signal_says_review": bool(says_review),
                    "data_labels": [],
                },
            )
            append_channel_entry(
                entries,
                "delegation_close",
                agent,
                REVIEWER_ID,
                {"turn": 1, "event": "escalation", "acted_on": True, "data_labels": []},
            )

        # Alone there is no herd to follow, so the rule reduces to the agent's
        # own signal: it escalates exactly when its signal says review, which is
        # exactly when that signal is correct. That is the compliant-in-isolation
        # status the system layer reads for composition rejection.
        members.append(
            CascadeMember(
                source_id=agent,
                index=index,
                signal_says_review=bool(says_review),
                signal_correct=bool(signal_correct),
                herd_before=herd_before,
                decided_by_herd=decided_by_herd,
                decision=decision,
                discarded=discarded,
                isolated_status="pass" if signal_correct else "fail",
            )
        )

    final_decision: CascadeDecision = decisions[-1]
    run = SwarmPairRun(
        channel=entries,
        rounds_completed=1,
        delegation_rounds_used=escalations,
    )
    system_verdict = evaluate_swarm_contract(cascade_contract(n_agents), run, members)
    locked_wrong = bool(locked_direction is not None and locked_direction != truth)
    return CascadeTrial(
        experiment_id=design.experiment_id,
        seed=design.seed,
        run_index=run_index,
        n_agents=n_agents,
        case_requires_review=design.case_requires_review,
        members=members,
        channel=run,
        verdict=CascadeVerdict(
            status="pass" if final_decision == truth else "fail",
            final_decision=final_decision,
            ground_truth=truth,
            case_requires_review=design.case_requires_review,
            escalations=escalations,
            locked_direction=locked_direction,
            locked_wrong=locked_wrong,
            discarded_correct_signals=discarded_correct_signals,
            majority_signal_correct=bool(correct_signals * 2 > n_agents),
            signal_accuracy=design.signal_accuracy,
        ),
        swarm_verdict=system_verdict,
    )


def run_cascade_sweep(
    design: CascadeDesign,
    n_values: tuple[int, ...] | list[int],
    trials_per_group: int,
) -> CascadeSweep:
    """Run K groups for each group size and aggregate the rates."""

    if trials_per_group < 1:
        raise ValueError("trials_per_group must be at least 1")
    order = list(n_values)
    if not order:
        raise ValueError("n_values must not be empty")
    if any(n < 1 for n in order):
        raise ValueError("every group size must be at least 1")

    summaries: list[CascadeSummary] = []
    for n_agents in order:
        correct_decisions = 0
        wrong_locks = 0
        locks = 0
        pooled = 0
        discarded = 0
        composition_rejections = 0
        contract_passes = 0
        for run_index in range(1, trials_per_group + 1):
            trial = run_cascade_trial(design, n_agents, run_index)
            if trial.verdict.status == "pass":
                correct_decisions += 1
            if trial.swarm_verdict.status == "pass":
                contract_passes += 1
            if trial.verdict.locked_wrong:
                wrong_locks += 1
            if trial.verdict.locked_direction is not None:
                locks += 1
            if trial.verdict.majority_signal_correct:
                pooled += 1
            discarded += trial.verdict.discarded_correct_signals
            if trial.swarm_verdict.composition_rejected:
                composition_rejections += 1
        summaries.append(
            CascadeSummary(
                n_agents=n_agents,
                case_requires_review=design.case_requires_review,
                trials=trials_per_group,
                group_decision_accuracy=correct_decisions / trials_per_group,
                system_fail_rate=1 - correct_decisions / trials_per_group,
                wrong_lock_rate=wrong_locks / trials_per_group,
                cascade_lock_rate=locks / trials_per_group,
                pooled_accuracy=pooled / trials_per_group,
                mean_discarded_correct_signals=discarded / trials_per_group,
                composition_rejected_rate=composition_rejections / trials_per_group,
                contract_passed_rate=contract_passes / trials_per_group,
                theoretical_wrong_lock_rate=theoretical_wrong_lock_rate(
                    design.signal_accuracy
                ),
            )
        )

    digest = _canonical_hash(
        {
            "experiment_id": design.experiment_id,
            "seed": design.seed,
            "signal_accuracy": design.signal_accuracy,
            "case_requires_review": design.case_requires_review,
            "trials_per_group": trials_per_group,
            "summaries": [summary.model_dump(mode="json") for summary in summaries],
        }
    )
    return CascadeSweep(
        experiment_id=design.experiment_id,
        seed=design.seed,
        signal_accuracy=design.signal_accuracy,
        case_requires_review=design.case_requires_review,
        trials_per_group=trials_per_group,
        summaries=summaries,
        sweep_digest=digest,
    )


def format_sweep(sweep: CascadeSweep) -> str:
    """A plain-text table for one sweep, shared by the CLI and the script.

    ASCII only: the CLI may run on a Windows console without ``-X utf8``.
    """

    lines = [
        "",
        f"Informational cascade - case: {case_name(sweep.case_requires_review)}"
        f"   signal accuracy: {sweep.signal_accuracy:.3f}",
        f"seed={sweep.seed}  groups/size={sweep.trials_per_group}",
        "",
        "  n   cascade-ok   pooled    herd-wrong   model    locked   discarded   contract",
        "  --  ----------   -------   ----------   ------   ------   ---------   --------",
    ]
    for summary in sweep.summaries:
        lines.append(
            f"  {summary.n_agents:<2}  "
            f"{summary.group_decision_accuracy:>10.3f}   "
            f"{summary.pooled_accuracy:>7.3f}   "
            f"{summary.wrong_lock_rate:>10.3f}   "
            f"{summary.theoretical_wrong_lock_rate:>6.3f}   "
            f"{summary.cascade_lock_rate:>6.3f}   "
            f"{summary.mean_discarded_correct_signals:>9.2f}   "
            f"{summary.contract_passed_rate:>8.3f}"
        )
    lines.append("")
    lines.append(f"sweep digest: {sweep.sweep_digest}")
    lines.append("")
    return "\n".join(lines)


def write_sweep(sweep: CascadeSweep, directory: str | Path) -> Path:
    """Write the sweep as JSON; existing evidence is never overwritten."""

    target = (
        Path(directory)
        / f"sweep_cascade_{case_name(sweep.case_requires_review)}_"
        f"{regime_name(sweep.signal_accuracy)}.json"
    )
    if target.exists():
        raise FileExistsError(f"{target} already exists; evidence is never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(sweep.model_dump(mode="json"), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# Signed evidence (M2 pattern over N members)
# ---------------------------------------------------------------------------


def cascade_payload(trial: CascadeTrial) -> dict:
    """The experiment-specific part of the run, covered by the signature."""

    return {
        "n_agents": trial.n_agents,
        "signal_accuracy": trial.verdict.signal_accuracy,
        "verdict": trial.verdict.model_dump(mode="json"),
        "topology": cascade_topology(trial.n_agents).model_dump(mode="json"),
    }


def build_cascade_report(
    trial: CascadeTrial, *, batch_digest: str | None = None
) -> SocialReport:
    """Assemble the unsigned cascade report (evidence digests, no signature)."""

    return build_social_report(
        experiment_kind="cascade",
        experiment_id=trial.experiment_id,
        seed=trial.seed,
        run_index=trial.run_index,
        group_size=trial.n_agents,
        payload=cascade_payload(trial),
        channel=trial.channel,
        members=trial.members,
        swarm_contract_id=cascade_contract(trial.n_agents).swarm_contract_id,
        contract_sha256=cascade_contract_digest(trial.n_agents),
        batch_digest=batch_digest,
    )


def sign_cascade_trial(
    trial: CascadeTrial,
    *,
    private_key_path,
    public_key_path,
    batch_digest: str | None = None,
    signed_at: str | None = None,
) -> SocialReport:
    """Sign one trial's report over its channel and all N member records."""

    report = build_cascade_report(trial, batch_digest=batch_digest)
    return sign_social_report(
        report,
        channel=trial.channel,
        members=trial.members,
        private_key_path=private_key_path,
        public_key_path=public_key_path,
        signed_at=signed_at,
    )


def sign_sweep_samples(
    sweep: CascadeSweep,
    design: CascadeDesign,
    directory: str | Path,
    *,
    private_key_path: str | Path,
    public_key_path: str | Path,
    signed_at: str | None = None,
) -> list[SocialReport]:
    """Sign one representative group per size, bound to the sweep digest.

    Writes ``signed/cascade_<case>_a<accuracy>/n<size>.report.json`` and its
    paired ``.evidence.json``; existing files are never overwritten.
    """

    target_dir = (
        Path(directory)
        / "signed"
        / f"cascade_{case_name(sweep.case_requires_review)}_{regime_name(sweep.signal_accuracy)}"
    )
    reports: list[SocialReport] = []
    for summary in sweep.summaries:
        trial = run_cascade_trial(design, summary.n_agents, 1)
        report = sign_cascade_trial(
            trial,
            private_key_path=private_key_path,
            public_key_path=public_key_path,
            batch_digest=sweep.sweep_digest,
            signed_at=signed_at,
        )
        save_social_report(report, target_dir / f"n{summary.n_agents}.report.json")
        evidence_path = target_dir / f"n{summary.n_agents}.evidence.json"
        if evidence_path.exists():
            raise FileExistsError(f"{evidence_path} already exists")
        evidence_path.write_text(
            json.dumps(
                dump_social_evidence(trial.channel, trial.members),
                indent=2,
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
        reports.append(report)
    return reports


__all__ = [
    "BENIGN",
    "CASCADE_CASES",
    "CASCADE_EXPERIMENT_VERSION",
    "CASCADE_VARIANTS",
    "CASE_OWNER_ID",
    "GROUND_TRUTH",
    "REVIEWER_ID",
    "CascadeDecision",
    "CascadeDesign",
    "CascadeMember",
    "CascadeSummary",
    "CascadeSweep",
    "CascadeTrial",
    "CascadeVerdict",
    "build_cascade_report",
    "case_name",
    "cascade_contract",
    "cascade_contract_digest",
    "cascade_payload",
    "cascade_topology",
    "ground_truth_decision",
    "format_sweep",
    "regime_name",
    "run_cascade_sweep",
    "run_cascade_trial",
    "sign_cascade_trial",
    "sign_sweep_samples",
    "theoretical_wrong_lock_rate",
    "write_sweep",
]
