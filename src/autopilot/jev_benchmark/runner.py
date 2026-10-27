"""Jev × Integrity phase-A benchmark — runner.

Executes a ``ClassifierClient`` over a generated dataset (design §6):

- evaluates the working split (the 20% holdout stays untouched unless the
  caller explicitly includes it for the final report);
- performs two runs with identical inputs — run 1 feeds the §7 metrics,
  run 1 vs run 2 feeds Decision Stability;
- classifier failures are recorded per case, never silently swallowed;
- writes stable, content-addressed JSON reports and refuses to overwrite an
  existing report with different content (the BENCHMARK.md evidence
  discipline: never mutate existing evidence).

Nothing here authorizes production deployment; every artefact is evidence for
a human decision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autopilot.jev_benchmark.classifier import (
    ClassifierClient,
    ClassifierError,
    Decision,
)
from autopilot.jev_benchmark.generator import (
    BENCHMARK_VERSION,
    BenchmarkCase,
    GeneratedDataset,
    split_holdout,
)
from autopilot.jev_benchmark.metrics import (
    BenchmarkMetrics,
    GroundTruth,
    StabilityReport,
    Stratum,
    compute_benchmark_metrics,
    stability_report,
    DecisionRecord,
)

RUNNER_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Everything one benchmark execution produces, ready for serialization."""

    classifier: dict[str, str]
    dataset: dict[str, Any]
    evaluated: dict[str, int]
    errors: dict[str, str]
    metrics: BenchmarkMetrics
    stability: StabilityReport | None


def run_case(client: ClassifierClient, case: BenchmarkCase) -> Decision:
    """Classify one case through the protocol boundary (labels stay hidden)."""

    return client.classify(case)


def _record(case: BenchmarkCase, decision: Decision) -> DecisionRecord:
    return DecisionRecord(
        case_id=case.case_id,
        mechanism=case.mechanism,
        stratum=Stratum(case.stratum),
        ground_truth=GroundTruth(case.ground_truth.value),
        verdict=decision.verdict,
        confidence=decision.confidence,
    )


def run_benchmark(
    client: ClassifierClient,
    dataset: GeneratedDataset,
    *,
    include_holdout: bool = False,
    runs: int = 2,
    target_upper_bound: float = 0.10,
) -> RunResult:
    """Run the classifier over the dataset and compute all §7 metrics.

    ``runs`` defaults to 2 so Decision Stability is always measured; a single
    run is allowed for smoke tests and yields ``stability=None``.
    """

    if runs < 1:
        raise ValueError("runs must be at least 1")

    working, holdout = split_holdout(
        dataset.cases,
        holdout_every_nth=dataset.manifest.holdout_every_nth,
    )
    selected = list(dataset.cases) if include_holdout else working

    runs_records: list[list[DecisionRecord]] = []
    errors: dict[str, str] = {}
    for _ in range(runs):
        records: list[DecisionRecord] = []
        for case in selected:
            try:
                decision = client.classify(case)
            except ClassifierError as exc:
                errors[case.case_id] = str(exc)
                continue
            records.append(_record(case, decision))
        runs_records.append(records)

    metrics = compute_benchmark_metrics(
        runs_records[0], target_upper_bound=target_upper_bound
    )
    stability = (
        stability_report(runs_records[0], runs_records[1]) if runs >= 2 else None
    )

    return RunResult(
        classifier={"name": client.name, "version": client.version},
        dataset={
            "benchmark_version": dataset.manifest.benchmark_version,
            "case_count": dataset.manifest.case_count,
            "dataset_digest": dataset.manifest.dataset_digest,
        },
        evaluated={
            "cases": len(selected),
            "working": len(working),
            "holdout_excluded": 0 if include_holdout else len(holdout),
            "runs": runs,
        },
        errors=errors,
        metrics=metrics,
        stability=stability,
    )


def _metrics_to_dict(metrics: BenchmarkMetrics) -> dict[str, Any]:
    def fpr_dict(report: Any) -> dict[str, Any]:
        return {
            "negative_trajectories": report.negative_trajectories,
            "false_positives": report.false_positives,
            "point_estimate": report.point_estimate,
            "upper_bound": report.upper_bound,
            "confidence_level": report.confidence_level,
            "design_fpr": report.design_fpr,
            "meets_target": report.meets_target,
            "target_upper_bound": report.target_upper_bound,
        }

    return {
        "mechanism_reports": {
            mechanism: {
                "counts": asdict(report.counts),
                "wilson_fpr": fpr_dict(report.wilson_fpr),
                "calibration": asdict(report.calibration),
            }
            for mechanism, report in metrics.mechanism_reports.items()
        },
        "system": asdict(metrics.system),
        "overall_confusion": asdict(metrics.overall_confusion),
        "overall_calibration": asdict(metrics.overall_calibration),
        "uncertainty_rate": metrics.uncertainty_rate,
        "notes": list(metrics.notes),
    }


def result_to_dict(result: RunResult) -> dict[str, Any]:
    """Canonical JSON projection (sorted, stable across runs)."""

    payload: dict[str, Any] = {
        "runner_version": RUNNER_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "classifier": result.classifier,
        "dataset": result.dataset,
        "evaluated": result.evaluated,
        "errors": dict(sorted(result.errors.items())),
        "metrics": _metrics_to_dict(result.metrics),
    }
    if result.stability is not None:
        payload["stability"] = asdict(result.stability)
    return payload


def write_report(
    result: RunResult,
    output_dir: Path,
    *,
    label: str,
) -> Path:
    """Write ``report_{label}.json`` without mutating existing evidence.

    Rewriting byte-identical content is allowed (idempotent); writing
    different content over an existing report raises ``FileExistsError``.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"report_{label}.json"
    text = (
        json.dumps(result_to_dict(result), indent=2, ensure_ascii=False, sort_keys=True)
        + "\n"
    )
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return path
        raise FileExistsError(
            f"report already exists with different content; never mutate evidence: {path}"
        )
    path.write_text(text, encoding="utf-8")
    return path


__all__ = [
    "RUNNER_VERSION",
    "RunResult",
    "result_to_dict",
    "run_benchmark",
    "run_case",
    "write_report",
]
