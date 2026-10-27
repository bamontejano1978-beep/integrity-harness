"""Project a cohort run into a citable lab package.

The cohort runner records what happened; a package is what a third party can
cite, re-check and group. The gap between the two is not formatting: a package
needs three declarations the cohort report does not make — what produced the
behaviour, which protocol the run belongs to, and what the result may be taken
to claim — and inventing any of them here would let two unlike runs be pooled
under one rate.

So the projection asks for them instead. ``subject_kind`` is the exception: it
is read from the candidate manifest's provider, because a caller labelling a
deterministic stub as ``llm_model`` would corrupt the one field that decides
comparability. An unknown provider is refused rather than guessed.

What the projection refuses:

- a run whose manifest and cohort entry disagree about the candidate digest;
- a manifest and an evaluation that name different contracts;
- an empty experiment, protocol identity or claim boundary;
- a provider that does not identify what produced the behaviour.

Usage:
    from autopilot.lab_projection import project_cohort_run_from_report

    package = project_cohort_run_from_report(
        Path("data/agent_runs/.../cohort_report.json"),
        "COHORT-RUN-...",
        experiment="situational_pressure",
        protocol_version="situational-pressure-v0.1/INSURANCE-CLAIMS-INTAKE@0.1.0",
        claim_boundary="Model behaviour without a runtime guard, one episode per replica.",
    )
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from autopilot.lab_package import (
    LabPackage,
    SubjectKind,
    build_lab_package,
    verify_lab_package,
)

#: Which subject produced the behaviour, read from the candidate manifest. The
#: map is deliberately short: a provider that is not here is not guessed, because
#: ``subject_kind`` is what decides whether two runs may share a rate.
PROVIDER_SUBJECT_KINDS: dict[str, SubjectKind] = {
    "openai_compatible": "llm_model",
    "ollama": "llm_model",
    "deterministic": "simulated_policy",
}

_REQUIRED_RUN = ("run_id", "candidate_digest", "scenario_digest")
_REQUIRED_MANIFEST = (
    "agent_id",
    "candidate_version",
    "candidate_digest",
    "contract_id",
    "contract_version",
    "provider_type",
)
_REQUIRED_EVALUATION = (
    "contract_id",
    "contract_version",
    "trajectory_id",
    "status",
)


def subject_kind_for_provider(provider_type: str) -> SubjectKind:
    """Read what produced the behaviour from the provider, never from the caller."""

    key = (provider_type or "").strip().lower()
    subject = PROVIDER_SUBJECT_KINDS.get(key)
    if subject is None:
        raise ValueError(
            f"provider {provider_type!r} does not identify what produced the "
            "behaviour; add it to PROVIDER_SUBJECT_KINDS instead of guessing"
        )
    return subject


def _require_fields(source: Mapping[str, Any], fields: tuple[str, ...], what: str) -> None:
    missing = [name for name in fields if source.get(name) in (None, "")]
    if missing:
        raise ValueError(f"{what} is missing: {', '.join(missing)}")


def _single_line(value: str, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{what} is required")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{what} must be a single line")
    return text


def _same(left: Any, right: Any) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left == right


def project_cohort_run(
    *,
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    experiment: str,
    protocol_version: str,
    claim_boundary: str,
    execution_mode: Literal["live", "static"] = "live",
    created_at: str | None = None,
    key_dir: Path | None = None,
    signed_at: str | None = None,
) -> LabPackage:
    """Turn one cohort run into a package that verifies.

    ``protocol_version`` is required on purpose. The cohort report does not carry
    one, and its only derivable identity would be ``CONTRACT@version``, which is
    exactly what an unpressured intake run also declares; a pressure run given
    that identity would be pooled with runs it does not belong with.
    """

    _require_fields(run, _REQUIRED_RUN, "the cohort run entry")
    _require_fields(manifest, _REQUIRED_MANIFEST, "the candidate manifest")
    _require_fields(evaluation, _REQUIRED_EVALUATION, "the evaluation")

    experiment_name = _single_line(experiment, "experiment")
    protocol = _single_line(protocol_version, "protocol_version")
    boundary = _single_line(claim_boundary, "claim_boundary")

    if not _same(manifest["contract_id"], evaluation["contract_id"]):
        raise ValueError(
            "the manifest and the evaluation name different contracts: "
            f"{manifest['contract_id']!r} and {evaluation['contract_id']!r}"
        )
    if not _same(manifest["contract_version"], evaluation["contract_version"]):
        raise ValueError(
            "the manifest and the evaluation name different contract versions: "
            f"{manifest['contract_version']!r} and {evaluation['contract_version']!r}"
        )
    if not _same(manifest["candidate_digest"], run["candidate_digest"]):
        raise ValueError(
            "the cohort entry and the manifest disagree about the candidate digest; "
            "one of them does not describe the run being packaged"
        )

    subject_kind = subject_kind_for_provider(str(manifest["provider_type"]))

    clauses: list[dict[str, Any]] = []
    for result in evaluation.get("clause_results") or []:
        if not isinstance(result, Mapping):
            continue
        evidence = result.get("evidence") or []
        clauses.append(
            {
                "clause_id": result.get("clause_id"),
                "status": result.get("status"),
                "evaluation_method": result.get("evaluation_method"),
                "confidence": result.get("confidence"),
                "reason": result.get("reason"),
                "evidence_refs": [
                    item.get("ref") for item in evidence if isinstance(item, Mapping)
                ],
            }
        )

    failed = run.get("failed_clause_ids")
    artifact: dict[str, Any] = {
        "run_id": str(run["run_id"]),
        "experiment": experiment_name,
        "subject_kind": subject_kind,
        "protocol_version": protocol,
        "claim_boundary": boundary,
        "contract_id": str(manifest["contract_id"]),
        "contract_version": str(manifest["contract_version"]),
        "agent": {
            "source_id": run.get("agent_source_id"),
            "agent_id": str(manifest["agent_id"]),
            "candidate_version": str(manifest["candidate_version"]),
            "candidate_digest": str(manifest["candidate_digest"]),
            "provider_type": str(manifest["provider_type"]),
            "provider_model": manifest.get("provider_model"),
            "safety_guard": manifest.get("safety_guard"),
            "runner_version": manifest.get("runner_version"),
        },
        "scenario": {
            "source_id": run.get("scenario_source_id"),
            "digest": str(run["scenario_digest"]),
        },
        "outcomes": {
            "status": evaluation["status"],
            "recommended_autonomy": evaluation.get("recommended_autonomy"),
            "block_release": evaluation.get("block_release"),
            "failed_clause_ids": list(failed) if isinstance(failed, list) else [],
            "evaluated_at": evaluation.get("evaluated_at"),
        },
        "clauses": clauses,
        # The trajectory id stays nested: at the top level it would make the
        # artifact look like a fixture handed to the lab, not a run the lab made.
        "source": {
            "manifest_id": manifest.get("manifest_id"),
            "trajectory_id": evaluation["trajectory_id"],
            "cohort_id": run.get("cohort_id"),
            "dataset_id": run.get("dataset_id"),
        },
    }

    package = build_lab_package(
        artifact,
        experiment=experiment_name,
        subject_kind=subject_kind,
        protocol_version=protocol,
        execution_mode=execution_mode,
        created_at=created_at,
        key_dir=key_dir,
        signed_at=signed_at,
    )
    verification = verify_lab_package(package.model_dump(mode="json"))
    if not verification.valid:
        raise ValueError(
            "the projected package does not verify: " + "; ".join(verification.problems)
        )
    return package


def project_cohort_run_from_report(
    report_path: Path,
    run_id: str,
    *,
    experiment: str,
    protocol_version: str,
    claim_boundary: str,
    execution_mode: Literal["live", "static"] = "live",
    created_at: str | None = None,
    key_dir: Path | None = None,
    signed_at: str | None = None,
) -> LabPackage:
    """Project one run of a cohort report, reading its manifest and evaluation.

    The run directory is resolved from the entry's ``relative_output_dir``, which
    is relative to the report. A report whose entry has no such field is refused:
    guessing the layout would package whatever happened to be there.
    """

    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the cohort report {report_path}: {exc}") from exc
    if not isinstance(report, Mapping):
        raise ValueError("the cohort report is not a JSON object")

    entries = report.get("runs")
    if not isinstance(entries, list):
        raise ValueError("the cohort report has no runs list")
    entry = next(
        (item for item in entries if isinstance(item, Mapping) and item.get("run_id") == run_id),
        None,
    )
    if entry is None:
        raise ValueError(f"run {run_id!r} is not in {report_path}")
    relative = entry.get("relative_output_dir")
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"run {run_id!r} declares no relative_output_dir")

    run_dir = report_path.parent / relative
    manifest_path = run_dir / "candidate_manifest.json"
    evaluation_path = run_dir / "evaluation.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the run files under {run_dir}: {exc}") from exc
    if not isinstance(manifest, Mapping) or not isinstance(evaluation, Mapping):
        raise ValueError("the manifest and the evaluation must be JSON objects")

    enriched = dict(entry)
    enriched.setdefault("cohort_id", report.get("cohort_id"))
    enriched.setdefault("dataset_id", report.get("dataset_id"))

    return project_cohort_run(
        run=enriched,
        manifest=manifest,
        evaluation=evaluation,
        experiment=experiment,
        protocol_version=protocol_version,
        claim_boundary=claim_boundary,
        execution_mode=execution_mode,
        created_at=created_at,
        key_dir=key_dir,
        signed_at=signed_at,
    )


__all__ = [
    "PROVIDER_SUBJECT_KINDS",
    "project_cohort_run",
    "project_cohort_run_from_report",
    "subject_kind_for_provider",
]
