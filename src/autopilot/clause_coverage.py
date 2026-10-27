"""Per-clause coverage ledger over evaluation artifacts.

Answers one question the anchoring audit could only answer by hand: for every
clause in a corpus, how often has it actually been executed, and has it ever
been observed to fail?

Two gaps matter and both are reported explicitly instead of being inferred:

- a clause that is only ever ``not_applicable`` has executed nothing, because
  no trigger was observed in any artifact;
- a clause that has only ever passed has never been observed to discriminate.

This module decides nothing, mutates nothing and never invents a status: it
reads ``evaluation.json`` artifacts, aggregates what they record, and lets a
gate turn "no clause is untested" into a build requirement.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from autopilot.contracts.models import ContractDocument
from autopilot.evaluator import ClauseStatus

CLAUSE_COVERAGE_VERSION = "0.1"

#: File name the cohort and evaluate commands write per run.
EVALUATION_FILENAME = "evaluation.json"

_EXECUTED_STATUSES = (
    ClauseStatus.passed,
    ClauseStatus.failed,
    ClauseStatus.indeterminate,
)


class ClauseCoverageEntry(BaseModel):
    """What the artifacts say about one clause."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    contracts: list[str]
    evaluations: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    indeterminate: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    first_evaluated_at: str | None = None
    last_evaluated_at: str | None = None
    last_failed_at: str | None = None

    @property
    def triggered(self) -> bool:
        """True when at least one artifact observed the clause's trigger."""

        return self.evaluations > 0

    @property
    def discriminated(self) -> bool:
        """True when the clause has been observed to fail at least once."""

        return self.failed > 0


class ClauseCoverageReport(BaseModel):
    """Aggregate coverage plus the two gaps a gate can act on."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    #: What the numbers came from. Set when a report is read from a recorded
    #: snapshot rather than aggregated from evaluation artifacts.
    source: str | None = None
    artifact_count: int = Field(ge=0)
    ignored_artifact_count: int = Field(ge=0)
    clause_count: int = Field(ge=0)
    entries: list[ClauseCoverageEntry]
    never_triggered: list[str]
    never_failed: list[str]
    expected_without_artifact: list[str]
    gate_passed: bool = True
    ledger_version: str = CLAUSE_COVERAGE_VERSION


class ClauseCoverageInputError(ValueError):
    """Raised when an input path cannot be read as an artifact tree."""


def _as_clause_status(value: object) -> ClauseStatus | None:
    if isinstance(value, ClauseStatus):
        return value
    if isinstance(value, str):
        try:
            return ClauseStatus(value)
        except ValueError:
            return None
    return None


def artifact_paths(paths: Iterable[str | Path]) -> list[Path]:
    """Expand files and directories into a sorted list of artifacts.

    A directory is searched recursively for ``evaluation.json``. An explicit
    file is taken as-is, which lets callers point at a renamed or copied
    artifact without teaching the ledger about single-file layouts.
    """

    resolved: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            resolved.extend(sorted(path.rglob(EVALUATION_FILENAME)))
        elif path.is_file():
            resolved.append(path)
        else:
            raise ClauseCoverageInputError(f"not found: {path}")
    # Deterministic order; duplicates from overlapping roots collapse.
    unique: dict[Path, None] = {}
    for path in resolved:
        unique.setdefault(path.resolve(), None)
    return sorted(unique)


def _load_artifact(path: Path) -> list[dict[str, object]] | None:
    """Return the artifact's clause records, or None when it is not one.

    Leniency is deliberate: a mixed directory may contain other JSON. Those
    files are counted as ignored rather than silently changing the report.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    clause_results = payload.get("clause_results")
    if not isinstance(clause_results, list):
        return None
    contract_id = payload.get("contract_id")
    contract_version = payload.get("contract_version")
    records: list[dict[str, object]] = []
    for result in clause_results:
        if not isinstance(result, dict):
            continue
        clause_id = result.get("clause_id")
        if not isinstance(clause_id, str) or not clause_id:
            continue
        records.append(
            {
                "clause_id": clause_id,
                "status": _as_clause_status(result.get("status")),
                "observed_at": result.get("observed_at")
                if isinstance(result.get("observed_at"), str)
                else None,
                "contract_id": contract_id if isinstance(contract_id, str) else "",
                "contract_version": contract_version
                if isinstance(contract_version, str)
                else "",
            }
        )
    return records


def declared_clause_ids(documents: Sequence[ContractDocument]) -> list[str]:
    """Every clause id a corpus declares, in document order, de-duplicated."""

    seen: dict[str, None] = {}
    for document in documents:
        for clause in document.clauses:
            seen.setdefault(clause.id, None)
    return list(seen)


def collect_clause_coverage(
    paths: Iterable[str | Path],
    *,
    declared: Iterable[str] = (),
    require_all_triggered: bool = False,
    require_all_failed: bool = False,
) -> ClauseCoverageReport:
    """Aggregate artifacts and evaluate the requested gates.

    ``declared`` is the expected clause corpus (usually every clause of the
    contracts under test). Declaring it is what makes "this clause has no
    artifact at all" visible; without it the ledger can only describe what it
    was shown.
    """

    artifacts = artifact_paths(paths)
    totals: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "passed": 0,
            "failed": 0,
            "indeterminate": 0,
            "not_applicable": 0,
            "contracts": set(),
            "stamps": [],
            "failures": [],
        }
    )
    ignored = 0
    for path in artifacts:
        records = _load_artifact(path)
        if records is None:
            ignored += 1
            continue
        for record in records:
            clause_id = str(record["clause_id"])
            bucket = totals[clause_id]
            status = record["status"]
            if isinstance(status, ClauseStatus):
                bucket[status.name] = int(bucket[status.name]) + 1
                if status is ClauseStatus.failed:
                    stamp = record.get("observed_at")
                    if isinstance(stamp, str):
                        failures = bucket["failures"]
                        assert isinstance(failures, list)
                        failures.append(stamp)
            contract_id = record.get("contract_id")
            contract_version = record.get("contract_version")
            if isinstance(contract_id, str) and contract_id:
                label = (
                    f"{contract_id} v{contract_version}"
                    if isinstance(contract_version, str) and contract_version
                    else contract_id
                )
                contracts = bucket["contracts"]
                assert isinstance(contracts, set)
                contracts.add(label)
            stamp = record.get("observed_at")
            if isinstance(stamp, str) and stamp:
                stamps = bucket["stamps"]
                assert isinstance(stamps, list)
                stamps.append(stamp)

    entries: list[ClauseCoverageEntry] = []
    for clause_id in sorted(totals):
        bucket = totals[clause_id]
        stamps = sorted(str(stamp) for stamp in bucket["stamps"])  # type: ignore[union-attr]
        failures = sorted(str(stamp) for stamp in bucket["failures"])  # type: ignore[union-attr]
        passed = int(bucket["passed"])
        failed = int(bucket["failed"])
        indeterminate = int(bucket["indeterminate"])
        not_applicable = int(bucket["not_applicable"])
        entries.append(
            ClauseCoverageEntry(
                clause_id=clause_id,
                contracts=sorted(str(item) for item in bucket["contracts"]),  # type: ignore[union-attr]
                evaluations=passed + failed + indeterminate,
                passed=passed,
                failed=failed,
                indeterminate=indeterminate,
                not_applicable=not_applicable,
                first_evaluated_at=stamps[0] if stamps else None,
                last_evaluated_at=stamps[-1] if stamps else None,
                last_failed_at=failures[-1] if failures else None,
            )
        )

    observed = {entry.clause_id for entry in entries}
    declared_ids = list(dict.fromkeys(declared))
    expected_without_artifact = sorted(
        clause_id for clause_id in declared_ids if clause_id not in observed
    )
    never_triggered = sorted(entry.clause_id for entry in entries if not entry.triggered)
    never_failed = sorted(entry.clause_id for entry in entries if not entry.discriminated)

    gate_passed = gate_satisfied(
        never_triggered=never_triggered,
        never_failed=never_failed,
        expected_without_artifact=expected_without_artifact,
        require_all_triggered=require_all_triggered,
        require_all_failed=require_all_failed,
    )

    return ClauseCoverageReport(
        artifact_count=len(artifacts) - ignored,
        ignored_artifact_count=ignored,
        clause_count=len(entries),
        entries=entries,
        never_triggered=never_triggered,
        never_failed=never_failed,
        expected_without_artifact=expected_without_artifact,
        gate_passed=gate_passed,
    )


#: Columns the anchoring audit records per clause. Only the coverage-relevant
#: ones are read; severity and verdict stay in the audit document.
AUDIT_COLUMNS = ("id", "pass", "fail", "na")


def load_audit_csv(
    path: str | Path, *, source_label: str | None = None
) -> ClauseCoverageReport:
    """Build a report from a recorded audit snapshot instead of artifacts.

    The anchoring audit already counted, per clause, how often it passed, failed
    or was not applicable across the evaluated corpus. Rendering cards from that
    snapshot is not a new measurement: the report says where the numbers came
    from and the cards carry no timestamps, because the audit did not record
    them.
    """

    audit_path = Path(path)
    try:
        text = audit_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ClauseCoverageInputError(f"cannot read audit CSV {audit_path}: {exc}") from exc
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ClauseCoverageInputError(f"audit CSV {audit_path} has no rows")
    missing = [column for column in AUDIT_COLUMNS if column not in rows[0]]
    if missing:
        raise ClauseCoverageInputError(
            f"audit CSV {audit_path} is missing column(s): {', '.join(missing)}"
        )

    entries: list[ClauseCoverageEntry] = []
    for row in rows:
        clause_id = (row.get("id") or "").strip()
        if not clause_id:
            continue
        try:
            passed = int(row["pass"])
            failed = int(row["fail"])
            not_applicable = int(row["na"])
        except (TypeError, ValueError) as exc:
            raise ClauseCoverageInputError(
                f"audit CSV {audit_path} has a non-numeric count for {clause_id}"
            ) from exc
        domain = (row.get("dominio") or "").strip()
        entries.append(
            ClauseCoverageEntry(
                clause_id=clause_id,
                contracts=[domain] if domain else [],
                evaluations=passed + failed,
                passed=passed,
                failed=failed,
                indeterminate=0,
                not_applicable=not_applicable,
            )
        )
    if not entries:
        raise ClauseCoverageInputError(f"audit CSV {audit_path} has no clause rows")

    duplicate_ids = sorted(
        clause_id
        for clause_id, count in Counter(entry.clause_id for entry in entries).items()
        if count > 1
    )
    if duplicate_ids:
        raise ClauseCoverageInputError(
            f"audit CSV {audit_path} repeats clause id(s): {', '.join(duplicate_ids)}"
        )

    entries.sort(key=lambda entry: entry.clause_id)
    return ClauseCoverageReport(
        source=source_label or f"recorded audit snapshot: {audit_path.name}",
        artifact_count=0,
        ignored_artifact_count=0,
        clause_count=len(entries),
        entries=entries,
        never_triggered=[entry.clause_id for entry in entries if not entry.triggered],
        never_failed=[entry.clause_id for entry in entries if not entry.discriminated],
        expected_without_artifact=[],
    )


@dataclass(frozen=True)
class CardCanary:
    """The live canary verdict a card carries: a table cell and its detail."""

    state: str
    detail: str


def render_detector_cards_document(
    report: ClauseCoverageReport,
    *,
    title: str = "Detector cards — clause corpus",
    canary_states: Mapping[str, CardCanary] | None = None,
) -> str:
    """One document: a summary table for triage and a card per clause.

    ``canary_states`` is optional so the snapshot can still be rendered on its
    own; when given, every row and card carries the executable verdict next to
    the recorded one, and the document stops being a description of the past.
    """

    lines = [
        f"# {title}",
        "",
        f"- Clauses: **{report.clause_count}**",
        f"- Source: {report.source or 'evaluation artifacts aggregated by the ledger'}",
        "",
        "`not exercised` means no trigger was observed, so the clause produced no "
        "verdict; `never fired` means it ran and was never seen to fail.",
        "",
        "The counts are the snapshot's. A corpus added after it was recorded is not "
        "reflected here: aggregate that corpus' artifacts with `clause-coverage` "
        "for a current ledger instead of reading this document as the state today.",
    ]
    if canary_states is not None:
        lines.extend(
            [
                "",
                "The **Canary** column is the live verdict of "
                "`scripts/check_clause_canaries.py`: `fires` means a committed "
                "fixture still makes the clause fail, `unproven` means no fixture "
                "does, and `stale` means a fixture is registered but no longer "
                "produces the failure the gate expects.",
            ]
        )
    if canary_states is None:
        lines.extend(
            [
                "",
                "| Clause | Evaluations | Pass | Fail | Not applicable | Status |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "| Clause | Evaluations | Pass | Fail | Not applicable | Status | Canary |",
                "|---|---:|---:|---:|---:|---|---|",
            ]
        )
    for entry in report.entries:
        canary = canary_states.get(entry.clause_id) if canary_states is not None else None
        row = (
            f"| `{entry.clause_id}` | {entry.evaluations} | {entry.passed} | "
            f"{entry.failed} | {entry.not_applicable} | {_status_short(entry)} |"
        )
        if canary is not None:
            row += f" {canary.state} |"
        lines.append(row)
    lines.extend(["", "## Cards"])
    for entry in report.entries:
        canary = canary_states.get(entry.clause_id) if canary_states is not None else None
        lines.extend(
            ["", detector_card(entry, heading="###", canary=canary).rstrip("\n")]
        )
    return "\n".join(lines) + "\n"


def gate_satisfied(
    *,
    never_triggered: Sequence[str],
    never_failed: Sequence[str],
    expected_without_artifact: Sequence[str],
    require_all_triggered: bool,
    require_all_failed: bool,
) -> bool:
    """One definition of the gate, shared by both report sources."""

    passed = True
    if require_all_triggered:
        passed = passed and not never_triggered and not expected_without_artifact
    if require_all_failed:
        passed = passed and not never_failed and not expected_without_artifact
    return passed


def _coverage_state(entry: ClauseCoverageEntry) -> str:
    """The one sentence a reader needs about whether this detector is proven."""

    if not entry.triggered:
        return (
            "No artifact observed this clause's trigger. It has not been exercised "
            "here, so this card is not evidence that the detector works."
        )
    if not entry.discriminated:
        return (
            f"Executed {entry.evaluations} time(s) and never observed to fail. "
            "Detection is not demonstrated: no artifact shows this clause "
            "distinguishing a compliant run from a violating one."
        )
    when = entry.last_failed_at or "at a time the source did not record"
    return (
        f"Observed to fail {entry.failed} time(s), most recently {when}. "
        "This clause has been seen to fire on real evidence."
    )


def _status_short(entry: ClauseCoverageEntry) -> str:
    """Table cell for the one-word version of the same three states."""

    if not entry.triggered:
        return "not exercised"
    if not entry.discriminated:
        return "never fired"
    return "fired"


def detector_card(
    entry: ClauseCoverageEntry,
    *,
    heading: str = "#",
    canary: CardCanary | None = None,
) -> str:
    """One clause's record, in the shape a reviewer can sign off on."""

    lines = [
        f"{heading} Detector card — `{entry.clause_id}`",
        "",
        "## What the corpus says",
        "",
        f"- Evaluations: **{entry.evaluations}**",
        f"- Pass: {entry.passed} · Fail: {entry.failed} · "
        f"Indeterminate: {entry.indeterminate} · Not applicable: {entry.not_applicable}",
        f"- Contracts: {', '.join(entry.contracts) if entry.contracts else 'none recorded'}",
        f"- First evaluation: {entry.first_evaluated_at or 'not recorded'}",
        f"- Last evaluation: {entry.last_evaluated_at or 'not recorded'}",
        f"- Last failure: {entry.last_failed_at or 'never'}",
        "",
        "## Status",
        "",
        _coverage_state(entry),
    ]
    if canary is not None:
        lines.extend(["", "## Canary", "", canary.detail])
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "This card describes the evaluation artifacts this ledger was given. It is "
            "not a claim about behaviour outside that corpus, and `not_applicable` "
            "counts are not executions: the clause produced no verdict at all.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_detector_cards(report: ClauseCoverageReport) -> dict[str, str]:
    """One card per clause, keyed by clause id."""

    return {entry.clause_id: detector_card(entry) for entry in report.entries}


def write_detector_cards(
    report: ClauseCoverageReport, directory: str | Path
) -> list[Path]:
    """Write the cards as Markdown files; returns the paths in clause order."""

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for clause_id, card in render_detector_cards(report).items():
        path = target / f"{clause_id.lower()}.md"
        path.write_text(card, encoding="utf-8")
        written.append(path)
    return written


def render_clause_coverage(report: ClauseCoverageReport) -> str:
    """Human-readable ledger: one row per clause, then the two gaps."""

    lines = [
        f"CLAUSE COVERAGE: {report.clause_count} clause(s) across "
        f"{report.artifact_count} artifact(s), {report.ignored_artifact_count} ignored"
    ]
    header = f"  {'clause':<44} {'evals':>5} {'pass':>5} {'fail':>5} {'indet':>5} {'n/a':>5}"
    lines.append(header)
    for entry in report.entries:
        lines.append(
            f"  {entry.clause_id:<44} {entry.evaluations:>5} {entry.passed:>5} "
            f"{entry.failed:>5} {entry.indeterminate:>5} {entry.not_applicable:>5}"
        )
    if report.entries:
        lines.append("")
    if report.expected_without_artifact:
        lines.append(
            "declared but never executed: " + ", ".join(report.expected_without_artifact)
        )
    if report.never_triggered:
        lines.append("observed only as not_applicable: " + ", ".join(report.never_triggered))
    if report.never_failed:
        lines.append("never observed to fail: " + ", ".join(report.never_failed))
    if not (report.expected_without_artifact or report.never_triggered or report.never_failed):
        lines.append("every clause has executed and has been observed to fail")
    lines.append(f"GATE: {'PASS' if report.gate_passed else 'FAIL'}")
    return "\n".join(lines) + "\n"
