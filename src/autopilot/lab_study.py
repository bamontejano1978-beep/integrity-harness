"""Studies: one preregistered question over the runs that answer it.

A package identifies one run. A folder of packages identifies a pile. Neither
is a study: neither says what was asked, how many runs were planned before
looking at the data, or which of them may be pooled. Building that from memory
after the fact is how a convenient sample becomes a finding.

This module adds the missing unit without adding a fifth record format:

- a **preregistration** declared before the runs (hypothesis, primary metric,
  planned replications, stopping rule, analysis plan),
- a **single comparability key** (``experiment``, ``subject_kind``,
  ``protocol_version``), because a study of unlike runs is two studies,
- **references to runs by artifact digest**, so every claim in the study can be
  traced back to a package a reader can verify offline.

Declared limits of this design:

1. A study groups runs of one protocol. It does not compare their results,
   pool rates or test the hypothesis it declares.
2. The digest binds the study to the runs it names; it proves the study has not
   changed, not that the protocol measures what it claims.
3. ``planned_replications`` versus ``run_count`` is a declared plan, not a
   quality verdict: reaching the plan does not make a result true, and stopping
   short of it is reported, not punished.
4. An unsealed run gives integrity only; a study built on unsealed runs is not
   attributable to Integrity Labs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autopilot.lab_evidence import artifact_digest
from autopilot.lab_index import build_run_index
from autopilot.lab_package import (
    LabPackage,
    SubjectKind,
    verify_lab_package,
)

STUDY_SCHEMA = "0.1"
STUDY_KIND = "preregistered_study"

#: What a study does not do, repeated wherever one is rendered or exported.
DECLARED_LIMITS: tuple[str, ...] = (
    "A study groups runs of one protocol; it does not compare their results or "
    "pool a rate across them.",
    "The study does not certify that the protocol measures what it claims; that "
    "judgement is the reader's.",
    "An unsealed run gives integrity only; a study over unsealed runs is not "
    "attributable to Integrity Labs.",
    "planned_replications versus run_count is a declared plan, not a quality "
    "verdict.",
)

StudyStatus = Literal["under_plan", "as_planned", "over_plan"]

_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DECLARED_FIELDS = ("title", "experiment", "protocol_version", "claim_boundary")


class StudyPreregistration(BaseModel):
    """What was decided before running, so it cannot be decided afterwards."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1)
    primary_metric: str = Field(min_length=1)
    planned_replications: int = Field(ge=1)
    stopping_rule: str = Field(min_length=1)
    analysis_plan: str = Field(min_length=1)


class LabStudy(BaseModel):
    """One preregistered question and the runs that answer it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = STUDY_SCHEMA
    study_kind: Literal["preregistered_study"] = STUDY_KIND
    study_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    experiment: str = Field(min_length=1)
    subject_kind: SubjectKind
    protocol_version: str = Field(min_length=1)
    preregistration: StudyPreregistration
    created_at: str = Field(min_length=1)
    claim_boundary: str = Field(min_length=1)
    run_digests: list[str] = Field(min_length=1)
    run_count: int = Field(ge=1)
    status: StudyStatus
    study_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    notes: str = ""


class StudyVerification(BaseModel):
    """Result of checking a study document without trusting its fields."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    study_id: str
    run_count: int
    problems: list[str] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(item: LabPackage | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, LabPackage):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return item
    raise TypeError(
        "packages must be LabPackage instances or package documents, "
        f"not {type(item).__name__}"
    )


def _status_for(run_count: int, planned_replications: int) -> StudyStatus:
    if run_count < planned_replications:
        return "under_plan"
    if run_count > planned_replications:
        return "over_plan"
    return "as_planned"


def lab_study_digest(payload: dict[str, Any]) -> str:
    """SHA-256 of the study content, without its own digest field."""

    body = {key: value for key, value in payload.items() if key != "study_sha256"}
    return artifact_digest(body)


def build_lab_study(
    packages: Sequence[LabPackage | dict[str, Any]],
    *,
    study_id: str,
    title: str,
    preregistration: StudyPreregistration | dict[str, Any],
    created_at: str | None = None,
    notes: str = "",
) -> LabStudy:
    """Bind a preregistered question to the runs that answer it.

    Every run is verified first, and all of them must share the same
    comparability key: a study that mixes protocols would let a reader pool
    numbers that mean different things.
    """

    items = [_payload(item) for item in packages]
    if not items:
        raise ValueError(
            "a study needs at least one packaged run; preregistration without "
            "evidence is a plan, not a study"
        )

    verified: list[LabPackage] = []
    failures: list[str] = []
    for position, item in enumerate(items, start=1):
        verification = verify_lab_package(item)
        if not verification.valid:
            detail = "; ".join(verification.problems or ["did not verify"])
            failures.append(f"run {position}: {detail}")
            continue
        verified.append(LabPackage.model_validate(item))
    if failures:
        raise ValueError(
            "refusing to build a study over packages that do not verify: "
            + " | ".join(failures)
        )

    keys = {(run.experiment, run.subject_kind, run.protocol_version) for run in verified}
    if len(keys) != 1:
        described = "; ".join(
            f"{experiment} / {subject} / {protocol}"
            for experiment, subject, protocol in sorted(keys)
        )
        raise ValueError(
            "a study covers one comparability key, but its runs span "
            f"{len(keys)} ({described}); split it into one study per protocol"
        )
    experiment, subject_kind, protocol_version = keys.pop()

    boundaries = {run.claim_boundary for run in verified}
    if len(boundaries) != 1:
        raise ValueError(
            f"the runs declare {len(boundaries)} different claim boundaries; a "
            "study cannot license more than its runs allow"
        )

    digests = [run.artifact_sha256 for run in verified]
    duplicates = sorted({digest for digest in digests if digests.count(digest) > 1})
    if duplicates:
        raise ValueError(
            "duplicate artifact digests in one study: "
            + ", ".join(digest[:16] for digest in duplicates)
            + "; counting one run twice inflates n"
        )

    index = build_run_index(verified, created_at=created_at)
    if len(index.groups) != 1:
        raise ValueError(
            "the runs do not form a single comparability group; "
            "a study cannot mix protocols"
        )

    prereg = (
        preregistration
        if isinstance(preregistration, StudyPreregistration)
        else StudyPreregistration.model_validate(preregistration)
    )

    moment = created_at or _now()
    try:
        parsed_moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an RFC 3339 timestamp") from exc
    if parsed_moment.tzinfo is None:
        raise ValueError("created_at must include a timezone")

    body: dict[str, Any] = {
        "schema_version": STUDY_SCHEMA,
        "study_kind": STUDY_KIND,
        "study_id": study_id,
        "title": title,
        "experiment": experiment,
        "subject_kind": subject_kind,
        "protocol_version": protocol_version,
        "preregistration": prereg.model_dump(mode="json"),
        "created_at": parsed_moment.isoformat(),
        "claim_boundary": next(iter(boundaries)),
        "run_digests": digests,
        "run_count": len(digests),
        "status": _status_for(len(digests), prereg.planned_replications),
        "notes": notes,
    }
    return LabStudy(**body, study_sha256=lab_study_digest(body))


def verify_lab_study(payload: dict[str, Any]) -> StudyVerification:
    """Check one study document, reporting every problem it can."""

    problems: list[str] = []
    study_id = str(payload.get("study_id") or "")

    if payload.get("schema_version") != STUDY_SCHEMA:
        problems.append(f"unsupported study schema: {payload.get('schema_version')!r}")
    if payload.get("study_kind") != STUDY_KIND:
        problems.append(f"study_kind must be {STUDY_KIND!r}")
    if not study_id:
        problems.append("study_id is missing")
    for field in _DECLARED_FIELDS:
        if not str(payload.get(field) or "").strip():
            problems.append(f"{field} is missing or empty")

    planned: int | None = None
    raw_preregistration = payload.get("preregistration")
    if not isinstance(raw_preregistration, dict):
        problems.append("preregistration is missing")
    else:
        try:
            prereg = StudyPreregistration.model_validate(raw_preregistration)
            planned = prereg.planned_replications
        except ValidationError as exc:
            details = "; ".join(
                f"{error['loc'][0]}: {error['msg']}"
                for error in exc.errors()
                if error.get("loc")
            )
            problems.append(f"preregistration is incomplete: {details}")

    raw_digests = payload.get("run_digests")
    digests: list[str] = []
    if not isinstance(raw_digests, list) or not raw_digests:
        problems.append("run_digests is missing or empty")
    else:
        digests = [str(digest) for digest in raw_digests]
        invalid = [digest for digest in digests if not _DIGEST_PATTERN.match(digest)]
        if invalid:
            problems.append(f"{len(invalid)} run digest(s) are not sha256 hex")
        duplicates = sorted({digest for digest in digests if digests.count(digest) > 1})
        if duplicates:
            problems.append(
                "duplicate run digests: "
                + ", ".join(digest[:16] for digest in duplicates)
            )

    run_count = payload.get("run_count")
    if run_count != len(digests):
        problems.append(
            f"run_count {run_count!r} does not match {len(digests)} run digest(s)"
        )
    if (
        planned is not None
        and isinstance(run_count, int)
        and run_count > 0
        and payload.get("status") != _status_for(run_count, planned)
    ):
        problems.append(
            f"status must be {_status_for(run_count, planned)!r} for {run_count} "
            f"run(s) against {planned} planned"
        )

    if payload.get("study_sha256") != lab_study_digest(payload):
        problems.append("study_sha256 does not match the study content (the study changed)")

    return StudyVerification(
        valid=not problems,
        study_id=study_id,
        run_count=len(digests),
        problems=problems,
    )


def write_lab_study(study: LabStudy, path: Path) -> Path:
    """Write the study as stable, diff-friendly JSON."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(study.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return path


def render_lab_study_text(study: LabStudy) -> str:
    """Human-readable summary of one study."""

    planned = study.preregistration.planned_replications
    lines: list[str] = [
        f"Estudio {study.study_id} · {study.title}",
        f"  protocolo {study.protocol_version} · sujeto {study.subject_kind} · "
        f"experimento {study.experiment}",
        f"  planificadas {planned} · corridas {study.run_count} · estado {study.status}",
        f"  hipotesis: {study.preregistration.hypothesis}",
        f"  metrica primaria: {study.preregistration.primary_metric}",
        f"  regla de parada: {study.preregistration.stopping_rule}",
        f"  plan de analisis: {study.preregistration.analysis_plan}",
        "  digests: " + ", ".join(digest[:16] for digest in study.run_digests),
        f"  sello del estudio: {study.study_sha256[:16]}",
        "",
        "Limite: el estudio agrupa corridas de un solo protocolo; no compara "
        "sus resultados ni calcula tasas conjuntas.",
    ]
    return "\n".join(lines)


__all__ = [
    "DECLARED_LIMITS",
    "STUDY_KIND",
    "STUDY_SCHEMA",
    "LabStudy",
    "StudyPreregistration",
    "StudyStatus",
    "StudyVerification",
    "build_lab_study",
    "lab_study_digest",
    "render_lab_study_text",
    "verify_lab_study",
    "write_lab_study",
]
