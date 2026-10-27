"""Asch conformity experiment over the N-agent mediated channel (v0.1).

Asch (1951, 1956): a subject answers an objectively verifiable question after a
unanimous majority of peers has given an obviously wrong answer. The subject
conforms far more often than when answering alone, and — Asch's most famous
result — a single dissenting ally collapses that conformity.

This module runs the paradigm over the same N-agent runner the bystander
experiment uses (``autopilot.social_swarm``): one mediated, hash-chained
channel, per-peer answers, one subject answer, and a signed report.

Policy (documented, deterministic)

- ``majority_size`` peers answer first; ``wrong_peers`` of them give the wrong
  answer. With ``dissenter=True`` one peer gives the *correct* answer (social
  support); without it the majority is unanimous.
- Conformity pressure is the share of the peer group backing the wrong answer,
  ``c = wrong_peers / (peer_count + 1)``. Unanimity raises it; one ally cuts it.
- The subject has a stable independence threshold ``theta`` drawn from the seed.
  It conforms iff ``theta < c`` and otherwise answers from its own (correct)
  private judgment. With ``theta`` uniform, ``P(conform) = c`` — so the model
  predicts the shape, not the level.

Reading the channel is part of the run, not an assumption: the subject's answer
is recorded after the peers' answers, together with how many wrong answers it
saw. Topology is declared in the report payload, so "the majority reached the
subject" is checkable evidence.

What this is, and what it is not (declared, per the house rule)

- **It is** a controlled simulation of the conformity mechanism over a
  hash-chained channel with signed evidence. Same seed ⇒ byte-identical run.
- **It is not** an experiment on LLM agents. The absolute conformity level is a
  property of the documented policy (a uniform independence threshold); Asch's
  subjects conformed at a lower absolute rate because many were strongly
  independent. The claim here is the *law of the phenomenon* — conformity rises
  with the size of a unanimous majority and saturates, and one ally collapses it
  — not a numeric match to the 1950s experiments.
- **There is no swarm contract here.** Conformity is not a contract violation;
  the experiment's verdict is the conformity outcome. The contract layer is what
  the bystander experiment exercises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

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
from autopilot.swarm import SwarmChannelEntry, SwarmPairRun
from autopilot.swarm_contract import SwarmTopology, SwarmTopologyEdge

ASCH_EXPERIMENT_VERSION = "0.1.0"

PRESENTER_ID = "presenter"
SUBJECT_ID = "subject"

AschRole = Literal["majority", "dissenter", "subject"]


class AschDesign(ExtensibleModel):
    """Deterministic design; the manipulated variable is the majority size."""

    experiment_id: Annotated[str, Field(min_length=1, max_length=120)] = (
        "asch-conformity-v0.1"
    )
    seed: Annotated[str, Field(min_length=1, max_length=120)] = "asch-v0.1"
    #: One peer gives the correct answer (Asch's dissenting ally).
    dissenter: bool = False
    ground_truth: Annotated[str, Field(min_length=1, max_length=60)] = "A"
    wrong_answer: Annotated[str, Field(min_length=1, max_length=60)] = "B"

    @model_validator(mode="after")
    def _validate_answers(self) -> AschDesign:
        if self.ground_truth == self.wrong_answer:
            raise ValueError("ground_truth and wrong_answer must differ")
        return self


class AschMember(SocialMember):
    """One participant record: a peer's answer or the subject's own answer."""

    role: AschRole
    index: int
    answer: str
    correct: bool
    conformed: bool = False
    #: Independence threshold; only the subject has one.
    threshold: float | None = None
    #: Wrong answers the subject saw on the channel before answering.
    visible_wrong_peers: int = 0


class AschVerdict(BaseModel):
    """The run's outcome: did the subject keep its own (correct) answer?"""

    model_config = ConfigDict(extra="forbid")

    #: pass = the subject answered correctly; fail = it conformed to the majority.
    status: Literal["pass", "fail"]
    subject_answer: str
    ground_truth: str
    conformed: StrictBool
    peer_count: int
    wrong_peers: int
    conformity_pressure: float


class AschTrial(BaseModel):
    """One executed run: the mediated channel plus the conformity verdict."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    seed: str
    run_index: int
    majority_size: int
    dissenter: bool
    members: list[AschMember]
    channel: SwarmPairRun
    verdict: AschVerdict


class AschSummary(BaseModel):
    """Aggregates of one (majority size, condition) cell over K runs."""

    model_config = ConfigDict(extra="forbid")

    majority_size: int
    dissenter: bool
    trials: int
    conformity_rate: float
    resistance_rate: float
    #: wrong_peers / (peer_count + 1): the closed-form pressure and, with a
    #: uniform threshold, the predicted conformity rate.
    theoretical_conformity_rate: float


class AschSweep(BaseModel):
    """The whole experiment: one summary per cell, plus a sweep digest."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    seed: str
    dissenter: bool
    trials_per_group: int
    summaries: list[AschSummary]
    sweep_digest: str


def _peer_ids(majority_size: int) -> list[str]:
    return [f"peer_{index}" for index in range(1, majority_size + 1)]


def asch_topology(majority_size: int) -> SwarmTopology:
    """The declared edges: the presenter reaches everyone, peers reach the subject."""

    peers = _peer_ids(majority_size)
    edges = [SwarmTopologyEdge(desde=PRESENTER_ID, hacia=peer) for peer in peers]
    edges.append(SwarmTopologyEdge(desde=PRESENTER_ID, hacia=SUBJECT_ID))
    edges.extend(SwarmTopologyEdge(desde=peer, hacia=SUBJECT_ID) for peer in peers)
    edges.append(SwarmTopologyEdge(desde=SUBJECT_ID, hacia=PRESENTER_ID))
    return SwarmTopology(nodes=[PRESENTER_ID, *peers, SUBJECT_ID], edges=edges)


def asch_pressure(peer_count: int, wrong_peers: int) -> float:
    """Conformity pressure: the share of the peer group backing the wrong answer."""

    if peer_count < 1:
        raise ValueError("peer_count must be at least 1")
    if not 0 <= wrong_peers <= peer_count:
        raise ValueError("wrong_peers must be between 0 and peer_count")
    return wrong_peers / (peer_count + 1)


def run_asch_trial(design: AschDesign, majority_size: int, run_index: int) -> AschTrial:
    """Execute one question with ``majority_size`` peers. Deterministic."""

    if majority_size < 1:
        raise ValueError("majority_size must be at least 1")
    if run_index < 1:
        raise ValueError("run_index must be at least 1")

    peers = _peer_ids(majority_size)
    wrong_peers = majority_size - (1 if design.dissenter else 0)
    pressure = asch_pressure(majority_size, wrong_peers)
    entries: list[SwarmChannelEntry] = []
    members: list[AschMember] = []

    # The presenter states the question to every participant; the answer is not
    # on the channel (each participant is supposed to judge for itself).
    for peer in peers:
        append_channel_entry(
            entries,
            "task_prompt",
            PRESENTER_ID,
            peer,
            {
                "turn": 1,
                "question_id": "line-length-match-001",
                "candidates": sorted([design.ground_truth, design.wrong_answer]),
                "data_labels": [],
            },
        )
    append_channel_entry(
        entries,
        "task_prompt",
        PRESENTER_ID,
        SUBJECT_ID,
        {
            "turn": 1,
            "question_id": "line-length-match-001",
            "candidates": sorted([design.ground_truth, design.wrong_answer]),
            "peer_count": majority_size,
            "data_labels": [],
        },
    )

    for index, peer in enumerate(peers, start=1):
        # With a dissenter, the last peer is the ally who says what is true.
        is_dissenter = design.dissenter and index == majority_size
        answer = design.ground_truth if is_dissenter else design.wrong_answer
        role: AschRole = "dissenter" if is_dissenter else "majority"
        append_channel_entry(
            entries,
            "majority_answer",
            peer,
            SUBJECT_ID,
            {
                "turn": 1,
                "peer_index": index,
                "role": role,
                "answer": answer,
                "correct": is_dissenter,
                "data_labels": [],
            },
        )
        members.append(
            AschMember(
                source_id=peer,
                role=role,
                index=index,
                answer=answer,
                correct=is_dissenter,
            )
        )

    # The subject reads the channel: how many wrong answers did it see, and did
    # anyone back the truth? Both are derived from the entries just written.
    visible_wrong_peers = sum(
        1
        for entry in entries
        if entry.kind == "majority_answer" and entry.payload["correct"] is False
    )
    threshold = unit_interval(design.seed, "subject-threshold", run_index)
    conformed = bool(threshold < pressure)
    subject_answer = design.wrong_answer if conformed else design.ground_truth
    append_channel_entry(
        entries,
        "subject_answer",
        SUBJECT_ID,
        PRESENTER_ID,
        {
            "turn": 1,
            "answer": subject_answer,
            "correct": not conformed,
            "conformed": conformed,
            "threshold": threshold,
            "conformity_pressure": pressure,
            "peer_count": majority_size,
            "wrong_peers": wrong_peers,
            "visible_wrong_peers": visible_wrong_peers,
            "data_labels": [],
        },
    )
    members.append(
        AschMember(
            source_id=SUBJECT_ID,
            role="subject",
            index=0,
            answer=subject_answer,
            correct=not conformed,
            conformed=conformed,
            threshold=threshold,
            visible_wrong_peers=visible_wrong_peers,
        )
    )

    run = SwarmPairRun(channel=entries, rounds_completed=1, delegation_rounds_used=0)
    verdict = AschVerdict(
        status="pass" if not conformed else "fail",
        subject_answer=subject_answer,
        ground_truth=design.ground_truth,
        conformed=conformed,
        peer_count=majority_size,
        wrong_peers=wrong_peers,
        conformity_pressure=pressure,
    )
    return AschTrial(
        experiment_id=design.experiment_id,
        seed=design.seed,
        run_index=run_index,
        majority_size=majority_size,
        dissenter=design.dissenter,
        members=members,
        channel=run,
        verdict=verdict,
    )


def run_asch_sweep(
    design: AschDesign,
    k_values: tuple[int, ...] | list[int],
    trials_per_group: int,
) -> AschSweep:
    """Run K subjects for each majority size and aggregate the conformity rate."""

    if trials_per_group < 1:
        raise ValueError("trials_per_group must be at least 1")
    order = list(k_values)
    if not order or any(k < 1 for k in order):
        raise ValueError("every majority size must be at least 1")

    summaries: list[AschSummary] = []
    for majority_size in order:
        conformed = 0
        for run_index in range(1, trials_per_group + 1):
            trial = run_asch_trial(design, majority_size, run_index)
            if trial.verdict.conformed:
                conformed += 1
        wrong_peers = majority_size - (1 if design.dissenter else 0)
        summaries.append(
            AschSummary(
                majority_size=majority_size,
                dissenter=design.dissenter,
                trials=trials_per_group,
                conformity_rate=conformed / trials_per_group,
                resistance_rate=1 - conformed / trials_per_group,
                theoretical_conformity_rate=asch_pressure(majority_size, wrong_peers),
            )
        )

    digest = _canonical_hash(
        {
            "experiment_id": design.experiment_id,
            "seed": design.seed,
            "dissenter": design.dissenter,
            "trials_per_group": trials_per_group,
            "summaries": [summary.model_dump(mode="json") for summary in summaries],
        }
    )
    return AschSweep(
        experiment_id=design.experiment_id,
        seed=design.seed,
        dissenter=design.dissenter,
        trials_per_group=trials_per_group,
        summaries=summaries,
        sweep_digest=digest,
    )


def condition_name(dissenter: bool) -> str:
    """The sweep and artifact label for a condition."""

    return "con_disidente" if dissenter else "unanime"


def format_sweep(sweep: AschSweep) -> str:
    """A plain-text table for one sweep, shared by the CLI and the script.

    ASCII only: the CLI may run on a Windows console without ``-X utf8``.
    """

    lines = [
        "",
        f"Asch conformity - condition: {condition_name(sweep.dissenter)}",
        f"seed={sweep.seed}  subjects/group={sweep.trials_per_group}",
        "",
        "  k   conformity   model     resistance",
        "  --  ----------   -------   ----------",
    ]
    for summary in sweep.summaries:
        lines.append(
            f"  {summary.majority_size:<2}  "
            f"{summary.conformity_rate:>10.3f}   "
            f"{summary.theoretical_conformity_rate:>7.3f}   "
            f"{summary.resistance_rate:>10.3f}"
        )
    lines.append("")
    lines.append(f"sweep digest: {sweep.sweep_digest}")
    lines.append("")
    return "\n".join(lines)


def write_sweep(sweep: AschSweep, directory: str | Path) -> Path:
    """Write the sweep as JSON; existing evidence is never overwritten."""

    target = Path(directory) / f"sweep_{condition_name(sweep.dissenter)}.json"
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


def asch_payload(trial: AschTrial) -> dict:
    """The experiment-specific part of the run, covered by the signature."""

    return {
        "majority_size": trial.majority_size,
        "dissenter": trial.dissenter,
        "conformed": trial.verdict.conformed,
        "verdict": trial.verdict.model_dump(mode="json"),
        "topology": asch_topology(trial.majority_size).model_dump(mode="json"),
    }


def build_asch_report(trial: AschTrial, *, batch_digest: str | None = None) -> SocialReport:
    """Assemble the unsigned Asch report (evidence digests, no signature)."""

    return build_social_report(
        experiment_kind="asch",
        experiment_id=trial.experiment_id,
        seed=trial.seed,
        run_index=trial.run_index,
        group_size=trial.majority_size,
        payload=asch_payload(trial),
        channel=trial.channel,
        members=trial.members,
        batch_digest=batch_digest,
    )


def sign_asch_trial(
    trial: AschTrial,
    *,
    private_key_path,
    public_key_path,
    batch_digest: str | None = None,
    signed_at: str | None = None,
) -> SocialReport:
    """Sign one trial's report over its channel and all N member records."""

    report = build_asch_report(trial, batch_digest=batch_digest)
    return sign_social_report(
        report,
        channel=trial.channel,
        members=trial.members,
        private_key_path=private_key_path,
        public_key_path=public_key_path,
        signed_at=signed_at,
    )


def sign_sweep_samples(
    sweep: AschSweep,
    design: AschDesign,
    directory: str | Path,
    *,
    private_key_path: str | Path,
    public_key_path: str | Path,
    signed_at: str | None = None,
) -> list[SocialReport]:
    """Sign one representative subject per size, bound to the sweep digest.

    Writes ``signed/<condition>/k<size>.report.json`` and its paired
    ``.evidence.json``; existing files are never overwritten.
    """

    target_dir = Path(directory) / "signed" / condition_name(sweep.dissenter)
    reports: list[SocialReport] = []
    for summary in sweep.summaries:
        trial = run_asch_trial(design, summary.majority_size, 1)
        report = sign_asch_trial(
            trial,
            private_key_path=private_key_path,
            public_key_path=public_key_path,
            batch_digest=sweep.sweep_digest,
            signed_at=signed_at,
        )
        save_social_report(report, target_dir / f"k{summary.majority_size}.report.json")
        evidence_path = target_dir / f"k{summary.majority_size}.evidence.json"
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
    "ASCH_EXPERIMENT_VERSION",
    "PRESENTER_ID",
    "SUBJECT_ID",
    "AschDesign",
    "AschMember",
    "AschRole",
    "AschSummary",
    "AschSweep",
    "AschTrial",
    "AschVerdict",
    "asch_payload",
    "asch_pressure",
    "asch_topology",
    "build_asch_report",
    "condition_name",
    "format_sweep",
    "run_asch_sweep",
    "run_asch_trial",
    "sign_asch_trial",
    "sign_sweep_samples",
    "write_sweep",
]
