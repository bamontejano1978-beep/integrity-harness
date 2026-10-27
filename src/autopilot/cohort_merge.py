"""Merge cohort reports from several lots into one analysable report.

The runner caps a cohort at 100 scenarios and the matched analysis needs the four
conditions paired in a single report, so a pilot is executed in lots. This module
puts the lots back together without inventing anything: the merged report is the
run list of all of them, with the counts recomputed from the runs themselves.

What it refuses, because a merge that accepts any of these produces a rate that
looks measured and is not:

- lots run against different candidates (mismatched ``candidate_digests``);
- lots produced by different runner or schema versions;
- a ``run_id`` appearing in two lots;
- the same replication number for the same agent, condition and variant in two
  lots, which is how a merged ``n`` gets inflated by copies;
- a run whose digests are not among those its own lot declares;
- a report whose ``run_count`` does not match its run list;
- a lot outside the evidence root, because its ``relative_output_dir`` could not
  then be resolved against that root.

The merged rates are recomputed by the existing analysis, not summed here: two
lots of different size must not be averaged as if they weighed the same.

Two fields are deliberately **not** required to be equal across lots, because by
construction they never are, and demanding equality would forbid the very merge
the pilot needs:

- ``scenario_digests``: each replica gets its own scenario file, and replica ids
  must be disjoint, so the lot that follows cannot share them. What is checked
  instead is that every run's scenario digest is among those its own lot declares.
- ``dataset_id``: the runner derives it from a hash of the cohort id, the runs and
  the runner version, so two different lots cannot produce the same value. The
  merged report records every source id and names the pool ``MERGED-<digest>``,
  which states that it is a union rather than pretending to be one lot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The merge and the analysis must agree on what a replica id is; sharing the
# pattern is cheaper than keeping two copies that can drift apart.
from autopilot.situational_assurance import _SCENARIO_SOURCE

MERGE_SCHEMA = "0.1"


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the cohort report {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"the cohort report {path} is not a JSON object")
    return payload


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def merge_cohort_reports(
    report_paths: Sequence[Path],
    *,
    root: Path,
    cohort_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Combine several lot reports into one, verifying identity first.

    ``root`` is the directory the merged ``relative_output_dir`` values will be
    relative to; every lot must live under it, and the resulting paths must stay
    inside it.
    """

    root = Path(root).resolve()
    paths = [Path(item) for item in report_paths]
    if len(paths) < 2:
        raise ValueError(
            "a merge needs at least two cohort reports; one report is already analysable"
        )

    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        report = _load(path)
        runs = report.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError(f"{path} lists no runs")
        if report.get("run_count") != len(runs):
            raise ValueError(
                f"{path} declares run_count={report.get('run_count')!r} but lists {len(runs)} runs"
            )
        loaded.append((path, report))

    first = loaded[0][1]
    for field in ("schema_version", "runner_version"):
        expected = first.get(field)
        for path, report in loaded[1:]:
            if report.get(field) != expected:
                raise ValueError(
                    f"{path} declares {field}={report.get(field)!r} where the first lot "
                    f"declares {expected!r}; lots of different instruments must not be merged"
                )
    candidates_expected = sorted(str(item) for item in (first.get("candidate_digests") or []))
    if not candidates_expected:
        raise ValueError("the first lot declares no candidate_digests")
    for path, report in loaded:
        declared = sorted(str(item) for item in (report.get("candidate_digests") or []))
        if declared != candidates_expected:
            raise ValueError(
                f"{path} carries different candidate_digests; merging it would pool "
                "different subjects under one rate"
            )
        scenario_digests = [str(item) for item in (report.get("scenario_digests") or [])]
        if len(set(scenario_digests)) != len(scenario_digests):
            raise ValueError(f"{path} lists the same scenario digest more than once")

    runs_out: list[dict[str, Any]] = []
    seen_run_ids: dict[str, Path] = {}
    seen_replicas: dict[tuple[str, str, str, int], Path] = {}

    for path, report in loaded:
        try:
            prefix = path.resolve().parent.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"{path} is outside the evidence root {root}; its run paths could not be resolved"
            ) from exc
        candidates = {str(item) for item in (report.get("candidate_digests") or [])}
        scenarios = {str(item) for item in (report.get("scenario_digests") or [])}

        for run in report["runs"]:
            if not isinstance(run, dict):
                raise ValueError(f"{path} contains a run that is not an object")
            run_id = str(run.get("run_id") or "")
            if not run_id:
                raise ValueError(f"{path} contains a run without run_id")
            if run_id in seen_run_ids:
                raise ValueError(
                    f"duplicate run_id {run_id}: already merged from {seen_run_ids[run_id]}"
                )
            seen_run_ids[run_id] = path

            agent_source = str(run.get("agent_source_id") or "")
            if not agent_source:
                raise ValueError(f"run {run_id} in {path} declares no agent_source_id")
            source = str(run.get("scenario_source_id") or "")
            match = _SCENARIO_SOURCE.fullmatch(source)
            if match is None:
                raise ValueError(
                    f"scenario source {source!r} in {path} must match "
                    "<condition>_<control|pressure>_r<replication>"
                )
            condition, variant, replicate_text = match.groups()
            key = (agent_source, condition, variant, int(replicate_text))
            if key in seen_replicas:
                raise ValueError(
                    f"replication {source} for {agent_source} appears in "
                    f"{seen_replicas[key]} and {path}; replica ids must be disjoint"
                )
            seen_replicas[key] = path

            if str(run.get("candidate_digest")) not in candidates:
                raise ValueError(
                    f"run {run_id} in {path} declares a candidate digest its lot does not list"
                )
            if str(run.get("scenario_digest")) not in scenarios:
                raise ValueError(
                    f"run {run_id} in {path} declares a scenario digest its lot does not list"
                )

            relative = str(run.get("relative_output_dir") or "")
            if not relative:
                raise ValueError(f"run {run_id} in {path} declares no relative_output_dir")
            combined = (prefix / relative).as_posix()
            resolved = (root / combined).resolve()
            if resolved != root and root not in resolved.parents:
                raise ValueError(f"run {run_id} escapes the evidence root: {combined}")

            entry = dict(run)
            entry["relative_output_dir"] = combined
            entry["lot"] = path.name
            runs_out.append(entry)

    runs_out.sort(
        key=lambda item: (
            item["agent_source_id"],
            item["scenario_source_id"],
            item["run_id"],
        )
    )
    cohort_ids = [str(report.get("cohort_id") or "") for _, report in loaded]
    dataset_ids = sorted(
        {str(report.get("dataset_id")) for _, report in loaded if report.get("dataset_id")}
    )
    merged_dataset = (
        dataset_ids[0]
        if len(dataset_ids) == 1
        else "MERGED-" + _digest(*dataset_ids)[:16].upper()
    )
    moment = created_at or datetime.now(UTC).isoformat()

    return {
        "schema_version": first.get("schema_version"),
        "cohort_id": cohort_id or "merged-" + _digest(*sorted(cohort_ids))[:16],
        "dataset_id": merged_dataset,
        "agent_source_count": len({item["agent_source_id"] for item in runs_out}),
        "scenario_source_count": len({item["scenario_source_id"] for item in runs_out}),
        "run_count": len(runs_out),
        "pass_count": sum(1 for item in runs_out if item.get("status") == "pass"),
        "fail_count": sum(1 for item in runs_out if item.get("status") == "fail"),
        "indeterminate_count": sum(
            1 for item in runs_out if item.get("status") == "indeterminate"
        ),
        "candidate_digests": sorted({str(item) for item in (first.get("candidate_digests") or [])}),
        "scenario_digests": sorted(
            {str(item) for _, report in loaded for item in (report.get("scenario_digests") or [])}
        ),
        "runner_version": first.get("runner_version"),
        "runs": runs_out,
        "merge": {
            "schema_version": MERGE_SCHEMA,
            "created_at": moment,
            "lot_count": len(loaded),
            "source_reports": [
                (path.resolve().relative_to(root)).as_posix() for path, _ in loaded
            ],
            "source_cohort_ids": cohort_ids,
            "source_dataset_ids": dataset_ids,
        },
    }


def write_merged_report(report: dict[str, Any], path: Path) -> Path:
    """Write the merged report as stable, diff-friendly JSON."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = ["MERGE_SCHEMA", "merge_cohort_reports", "write_merged_report"]
