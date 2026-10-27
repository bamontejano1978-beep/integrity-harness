"""Bystander experiment: diffusion of responsibility over an N-agent channel.

The design document ``docs/product/evaluacion_enjambres_20260918_v0.1.md`` §3
already names this failure in its own taxonomy: *"responsabilidad difusa: cada
agente asume que otro escaló; nadie escala"*. That is the bystander effect of
Darley & Latané (1968) under another name. This module turns the named failure
into a runnable experiment over the **same** mediated channel, swarm contract
and system verdict that M1/M2 use — no new evidence format, no new evaluator.

Paradigm

- One incident needs an escalation to the human operator, and ``n_bystanders``
  agents are present. Each has a stable personal "independence" threshold
  ``theta_i`` and a stable "noticed" draw, both derived from the seed.
- **Mechanism 1 — diffusion** (``mechanism="diffusion"``): felt responsibility
  is diluted by the number present, ``r = responsibility_weight / n``, decided
  without reading anyone else. Bystander *i* escalates iff it noticed and
  ``r >= theta_i``. Nobody is modelled as watching the others, so the dilution
  alone has to produce the result — the mechanism the original model isolates.
- **Mechanism 2 — pluralistic ignorance** (``mechanism="pluralistic_ignorance"``):
  agents **read the channel**. A bystander sees how many earlier bystanders
  stayed put and treats that inaction as evidence that nothing is wrong:
  ``r_i = responsibility_weight / (n + visible_non_reactors)``. One visible
  non-reaction is enough to start cooling the next agent, so the freeze
  compounds instead of waiting for the group to be large.
- Either way the escalation is a ``delegation_open`` handoff on the mediated
  channel and a non-escalating bystander records a ``bystander_observation``
  with its reason. The swarm contract asks ``cardinalidad_handoff: minimo: 1``:
  a group where nobody escalates fails systemically. Every bystander is
  compliant *in isolation* (alone, ``r = responsibility_weight``), so the
  failure returns as ``composition_rejected`` — the sale framing of design §8.3.

Evidence

- The channel is the shared N-agent channel of ``autopilot.social_swarm``
  (hash-chained ``SwarmChannelEntry``, re-checked by ``verify_swarm_channel``).
- ``sign_bystander_trial`` produces a ``SocialReport``: the M2 signing pattern
  generalised from two members to N — one Ed25519 signature over the payload,
  the channel digest and the chain head, plus one digest per member record.

What this is, and what it is not (declared, per the house rule)

- **It is** a controlled simulation of two documented social mechanisms over a
  hash-chained, verifiable channel, judged by the unchanged contract engine.
  One run is reproducible byte for byte; a sweep of K runs is a distribution.
- **It is not** an experiment on LLM agents. The thresholds come from the seed,
  not from a model. The honest claim is *"this mechanism reproduces the
  group-size effect under a documented policy"*, never *"LLMs show the
  bystander effect"*. That is M3 and stays compute-gated.

Determinism is by construction: no RNG, no clock, no network. Every stochastic
draw is ``_unit_interval(seed, label, run_index, index)`` over SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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

BYSTANDER_EXPERIMENT_VERSION = "0.1.0"

#: The agent that witnesses the incident, the escalation target, and the
#: per-bystander id pattern. All are declared topology nodes, never implicit.
VICTIM_ID = "victim"
HUMAN_OPERATOR_ID = "human_operator"

BystanderMechanism = Literal["diffusion", "pluralistic_ignorance"]


class BystanderDesign(ExtensibleModel):
    """Deterministic design of the experiment (the manipulated variable is ``n``)."""

    experiment_id: Annotated[str, Field(min_length=1, max_length=120)] = (
        "bystander-diffusion-v0.1"
    )
    seed: Annotated[str, Field(min_length=1, max_length=120)] = "bystander-v0.1"
    #: Felt responsibility of one bystander among ``n``; the diffusion knob.
    responsibility_weight: Annotated[float, Field(gt=0.0, le=1.0)] = 1.0
    #: Probability a bystander notices the incident at all (deterministic draw).
    noticing_probability: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    #: Which social mechanism drives the decision (see the module docstring).
    mechanism: BystanderMechanism = "diffusion"


class BystanderMember(SocialMember):
    """One bystander's stable draw and its decision in a given run.

    ``isolated_status`` is the member's verdict *alone* (``n = 1``), which is
    what makes a group failure a composition finding rather than a plain
    non-compliance.
    """

    index: int
    noticed: bool
    threshold: float
    felt_responsibility: float
    escalated: bool
    isolated_status: str
    #: Pluralistic ignorance: how many earlier bystanders it saw not acting.
    visible_non_reactors: int = 0


class BystanderTrial(BaseModel):
    """One executed run: the mediated channel plus the system verdict over it."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    seed: str
    run_index: int
    n_bystanders: int
    mechanism: BystanderMechanism
    members: list[BystanderMember]
    channel: SwarmPairRun
    escalations: int
    verdict: SwarmSystemVerdict


class BystanderSummary(BaseModel):
    """Aggregates of one (group size, mechanism) cell over K runs."""

    model_config = ConfigDict(extra="forbid")

    n_bystanders: int
    mechanism: BystanderMechanism
    trials: int
    #: Escalations / (n * trials): the per-bystander helping probability.
    individual_escalation_rate: float
    #: Fraction of groups where at least one bystander escalated.
    group_escalation_rate: float
    #: Fraction of groups where nobody escalated (the bystander effect).
    nobody_escalated_rate: float
    system_fail_rate: float
    composition_rejected_rate: float
    #: min(responsibility_weight / n, 1): the closed-form diffusion prediction,
    #: printed next to the measured rate. ``None`` when the cell does not use the
    #: diffusion mechanism, because the formula does not model the other one.
    theoretical_individual_rate: float | None


class BystanderSweep(BaseModel):
    """The whole experiment: one summary per cell, plus a sweep digest."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    seed: str
    mechanism: BystanderMechanism
    trials_per_group: int
    responsibility_weight: float
    noticing_probability: float
    summaries: list[BystanderSummary]
    sweep_digest: str


def _bystander_ids(n_bystanders: int) -> list[str]:
    return [f"bystander_{index}" for index in range(1, n_bystanders + 1)]


def bystander_contract(n_bystanders: int) -> SwarmContractDocument:
    """The swarm contract for one group size: at least one escalation, no loop.

    Generated (not hand-written) so the topology always matches the number of
    declared bystanders; the contract is what the unchanged evaluator consumes.
    """

    bystanders = _bystander_ids(n_bystanders)
    edges = [
        edge
        for bystander in bystanders
        for edge in (
            SwarmTopologyEdge(desde=VICTIM_ID, hacia=bystander),
            SwarmTopologyEdge(desde=bystander, hacia=HUMAN_OPERATOR_ID),
            SwarmTopologyEdge(desde=bystander, hacia=VICTIM_ID),
        )
    ]
    return SwarmContractDocument(
        swarm_contract_id=f"SWARM-BYSTANDER-N{n_bystanders}-PILOT",
        topology=SwarmTopology(
            nodes=[VICTIM_ID, *bystanders, HUMAN_OPERATOR_ID], edges=edges
        ),
        clauses=[
            SwarmCardinalidadHandoff(
                clause_id="SI-ENTONCES-ESCALA-AL-MENOS-UNO-001",
                tipo="cardinalidad_handoff",
                alcance="sistema",
                maximo=n_bystanders,
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


def bystander_contract_digest(n_bystanders: int) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(bystander_contract(n_bystanders).model_dump(mode="json"))
    ).hexdigest()


def _visible_non_reactors(entries: list[SwarmChannelEntry]) -> int:
    """Read the channel: how many earlier bystanders stayed put."""

    return sum(1 for entry in entries if entry.kind == "bystander_observation")


def run_bystander_trial(
    design: BystanderDesign, n_bystanders: int, run_index: int
) -> BystanderTrial:
    """Execute one incident with ``n_bystanders`` present. Deterministic."""

    if n_bystanders < 1:
        raise ValueError("n_bystanders must be at least 1")
    if run_index < 1:
        raise ValueError("run_index must be at least 1")

    bystanders = _bystander_ids(n_bystanders)
    entries: list[SwarmChannelEntry] = []
    members: list[BystanderMember] = []
    escalations = 0

    # The incident reaches every bystander: it is on the mediated channel, so
    # "who knew what, when" is evidenced rather than assumed.
    for bystander in bystanders:
        append_channel_entry(
            entries,
            "incident",
            VICTIM_ID,
            bystander,
            {
                "turn": 1,
                "event": "incident_declared",
                "requires_escalation": True,
                "bystanders_present": n_bystanders,
                "data_labels": [],
            },
        )

    for index, bystander in enumerate(bystanders, start=1):
        # Stable personal draws: the same person across every group size, so the
        # sweep is a within-subject comparison and not a fresh random cast.
        threshold = unit_interval(design.seed, "threshold", run_index, index)
        noticed = (
            unit_interval(design.seed, "noticing", run_index, index)
            < design.noticing_probability
        )
        visible_non_reactors = _visible_non_reactors(entries)
        if design.mechanism == "pluralistic_ignorance":
            # Mechanism 2: the earlier inaction is read as evidence that this is
            # not an emergency, so responsibility is diluted further.
            felt_responsibility = design.responsibility_weight / (
                n_bystanders + visible_non_reactors
            )
        else:
            felt_responsibility = design.responsibility_weight / n_bystanders
        escalated = bool(noticed and felt_responsibility >= threshold)

        if escalated:
            escalations += 1
            append_channel_entry(
                entries,
                "delegation_open",
                bystander,
                HUMAN_OPERATOR_ID,
                {
                    "turn": 1,
                    "event": "escalation",
                    "felt_responsibility": felt_responsibility,
                    "threshold": threshold,
                    "visible_non_reactors": visible_non_reactors,
                    "data_labels": [],
                },
            )
            append_channel_entry(
                entries,
                "delegation_close",
                bystander,
                HUMAN_OPERATOR_ID,
                {"turn": 1, "event": "escalation", "acted_on": True, "data_labels": []},
            )
        else:
            append_channel_entry(
                entries,
                "bystander_observation",
                bystander,
                VICTIM_ID,
                {
                    "turn": 1,
                    "event": "no_escalation",
                    "noticed": noticed,
                    "escalated": False,
                    "felt_responsibility": felt_responsibility,
                    "threshold": threshold,
                    "visible_non_reactors": visible_non_reactors,
                    "reason": (
                        "diffusion_of_responsibility" if noticed else "did_not_notice"
                    ),
                    "data_labels": [],
                },
            )

        # Alone, responsibility is undiluted: the same person would escalate as
        # long as it noticed and its threshold sits under the full weight.
        isolated_status = (
            "pass"
            if noticed and design.responsibility_weight >= threshold
            else "fail"
        )
        members.append(
            BystanderMember(
                source_id=bystander,
                index=index,
                noticed=noticed,
                threshold=threshold,
                felt_responsibility=felt_responsibility,
                escalated=escalated,
                isolated_status=isolated_status,
                visible_non_reactors=visible_non_reactors,
            )
        )

    run = SwarmPairRun(
        channel=entries,
        rounds_completed=1,
        delegation_rounds_used=escalations,
    )
    verdict = evaluate_swarm_contract(bystander_contract(n_bystanders), run, members)
    return BystanderTrial(
        experiment_id=design.experiment_id,
        seed=design.seed,
        run_index=run_index,
        n_bystanders=n_bystanders,
        mechanism=design.mechanism,
        members=members,
        channel=run,
        escalations=escalations,
        verdict=verdict,
    )


def run_bystander_sweep(
    design: BystanderDesign,
    n_values: tuple[int, ...] | list[int],
    trials_per_group: int,
) -> BystanderSweep:
    """Run K trials for each group size and aggregate the rates."""

    if trials_per_group < 1:
        raise ValueError("trials_per_group must be at least 1")
    order = list(n_values)
    if not order:
        raise ValueError("n_values must not be empty")
    if any(n < 1 for n in order):
        raise ValueError("every group size must be at least 1")

    summaries: list[BystanderSummary] = []
    for n_bystanders in order:
        escalations = 0
        groups_with_escalation = 0
        groups_without_escalation = 0
        system_failures = 0
        composition_rejections = 0
        for run_index in range(1, trials_per_group + 1):
            trial = run_bystander_trial(design, n_bystanders, run_index)
            escalations += trial.escalations
            if trial.escalations >= 1:
                groups_with_escalation += 1
            else:
                groups_without_escalation += 1
            if trial.verdict.status == "fail":
                system_failures += 1
            if trial.verdict.composition_rejected:
                composition_rejections += 1
        member_trials = n_bystanders * trials_per_group
        summaries.append(
            BystanderSummary(
                n_bystanders=n_bystanders,
                mechanism=design.mechanism,
                trials=trials_per_group,
                individual_escalation_rate=escalations / member_trials,
                group_escalation_rate=groups_with_escalation / trials_per_group,
                nobody_escalated_rate=groups_without_escalation / trials_per_group,
                system_fail_rate=system_failures / trials_per_group,
                composition_rejected_rate=composition_rejections / trials_per_group,
                theoretical_individual_rate=(
                    min(design.responsibility_weight / n_bystanders, 1.0)
                    if design.mechanism == "diffusion"
                    else None
                ),
            )
        )

    digest_payload = [summary.model_dump(mode="json") for summary in summaries]
    digest = _canonical_hash(
        {
            "experiment_id": design.experiment_id,
            "seed": design.seed,
            "mechanism": design.mechanism,
            "trials_per_group": trials_per_group,
            "responsibility_weight": design.responsibility_weight,
            "noticing_probability": design.noticing_probability,
            "summaries": digest_payload,
        }
    )
    return BystanderSweep(
        experiment_id=design.experiment_id,
        seed=design.seed,
        mechanism=design.mechanism,
        trials_per_group=trials_per_group,
        responsibility_weight=design.responsibility_weight,
        noticing_probability=design.noticing_probability,
        summaries=summaries,
        sweep_digest=digest,
    )


def format_sweep(sweep: BystanderSweep) -> str:
    """A plain-text table for one sweep, shared by the CLI and the script.

    ASCII only: the CLI may run on a Windows console without ``-X utf8``.
    """

    lines = [
        "",
        f"Bystander / diffusion of responsibility - mechanism: {sweep.mechanism}",
        f"seed={sweep.seed}  trials/group={sweep.trials_per_group}  "
        f"weight={sweep.responsibility_weight}",
        "",
        "  n   individual   model    group(>=1)   nobody(0)   fail    composition",
        "  --  ----------   -------   ---------   --------   ----    -----------",
    ]
    for summary in sweep.summaries:
        model = (
            "-"
            if summary.theoretical_individual_rate is None
            else f"{summary.theoretical_individual_rate:.3f}"
        )
        lines.append(
            f"  {summary.n_bystanders:<2}  "
            f"{summary.individual_escalation_rate:>10.3f}   "
            f"{model:>7}   "
            f"{summary.group_escalation_rate:>9.3f}   "
            f"{summary.nobody_escalated_rate:>8.3f}   "
            f"{summary.system_fail_rate:>5.3f}   "
            f"{summary.composition_rejected_rate:>11.3f}"
        )
    lines.append("")
    lines.append(f"sweep digest: {sweep.sweep_digest}")
    lines.append("")
    return "\n".join(lines)


def write_sweep(sweep: BystanderSweep, directory: str | Path) -> Path:
    """Write the sweep as JSON; existing evidence is never overwritten."""

    target = Path(directory) / f"sweep_{sweep.mechanism}.json"
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


def bystander_payload(trial: BystanderTrial) -> dict:
    """The experiment-specific part of the run, covered by the signature."""

    return {
        "mechanism": trial.mechanism,
        "escalations": trial.escalations,
        "verdict": trial.verdict.model_dump(mode="json"),
    }


def build_bystander_report(trial: BystanderTrial, *, batch_digest: str | None = None) -> SocialReport:
    """Assemble the unsigned bystander report (evidence digests, no signature)."""

    return build_social_report(
        experiment_kind="bystander",
        experiment_id=trial.experiment_id,
        seed=trial.seed,
        run_index=trial.run_index,
        group_size=trial.n_bystanders,
        payload=bystander_payload(trial),
        channel=trial.channel,
        members=trial.members,
        swarm_contract_id=bystander_contract(trial.n_bystanders).swarm_contract_id,
        contract_sha256=bystander_contract_digest(trial.n_bystanders),
        batch_digest=batch_digest,
    )


def sign_bystander_trial(
    trial: BystanderTrial,
    *,
    private_key_path,
    public_key_path,
    batch_digest: str | None = None,
    signed_at: str | None = None,
) -> SocialReport:
    """Sign one trial's report over its channel and all N member records."""

    report = build_bystander_report(trial, batch_digest=batch_digest)
    return sign_social_report(
        report,
        channel=trial.channel,
        members=trial.members,
        private_key_path=private_key_path,
        public_key_path=public_key_path,
        signed_at=signed_at,
    )


def sign_sweep_samples(
    sweep: BystanderSweep,
    design: BystanderDesign,
    directory: str | Path,
    *,
    private_key_path: str | Path,
    public_key_path: str | Path,
    signed_at: str | None = None,
) -> list[SocialReport]:
    """Sign one representative trial per group, bound to the sweep digest.

    Writes ``signed/<mechanism>/n<size>.report.json`` and its paired
    ``.evidence.json``; existing files are never overwritten.
    """

    target_dir = Path(directory) / "signed" / sweep.mechanism
    reports: list[SocialReport] = []
    for summary in sweep.summaries:
        trial = run_bystander_trial(design, summary.n_bystanders, 1)
        report = sign_bystander_trial(
            trial,
            private_key_path=private_key_path,
            public_key_path=public_key_path,
            batch_digest=sweep.sweep_digest,
            signed_at=signed_at,
        )
        save_social_report(report, target_dir / f"n{summary.n_bystanders}.report.json")
        evidence_path = target_dir / f"n{summary.n_bystanders}.evidence.json"
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
    "BYSTANDER_EXPERIMENT_VERSION",
    "HUMAN_OPERATOR_ID",
    "VICTIM_ID",
    "BystanderDesign",
    "BystanderMechanism",
    "BystanderMember",
    "BystanderSummary",
    "BystanderSweep",
    "BystanderTrial",
    "bystander_contract",
    "bystander_contract_digest",
    "bystander_payload",
    "build_bystander_report",
    "format_sweep",
    "run_bystander_sweep",
    "run_bystander_trial",
    "sign_bystander_trial",
    "sign_sweep_samples",
    "write_sweep",
]
