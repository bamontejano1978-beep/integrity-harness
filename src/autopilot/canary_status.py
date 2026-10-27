"""The canary state of every audited clause, shaped for the public site.

A detector card answers one reader who wants the reasoning; this module answers
the visitor who wants the list: which clauses a committed fixture can make fail
today, and which ones nothing exercises yet. The state is not hand-written. It
comes from ``check_registry``, the same re-run the claims gate performs, so a
page built from it cannot claim a detector works when the build disagrees.

Two artifacts come out of one document:

* ``web/assets/data/canary-status.json`` — machine-readable, with the fixture
  and (when it exists) the control behind every verdict.
* the summary and table rows inside the two localized pages, between HTML
  comment markers. The prose around them is hand-written; the state is not.

Publishing is fail-closed: if any canary stopped firing the gate rejects the
state, and this module refuses to render a page that would call it demonstrated.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from autopilot.clause_canaries import (
    DEFAULT_REGISTRY,
    CanaryError,
    CanaryRegistry,
    CanaryReport,
    check_registry,
    corpus_clause_ids,
)
from autopilot.evaluator import ClauseStatus

STATUS_SCHEMA_VERSION = "0.1"
DEFAULT_STATUS_JSON = "web/assets/data/canary-status.json"
PAGE_PATHS: dict[str, str] = {
    "es": "web/canarios/index.html",
    "en": "web/en/canaries/index.html",
}

SUMMARY_START = "<!-- canary-summary:start -->"
SUMMARY_END = "<!-- canary-summary:end -->"
ROWS_START = "<!-- canary-rows:start -->"
ROWS_END = "<!-- canary-rows:end -->"
EXTRAS_START = "<!-- canary-extras:start -->"
EXTRAS_END = "<!-- canary-extras:end -->"

Language = Literal["es", "en"]

LABELS: dict[str, dict[str, str]] = {
    "es": {
        "audited": "cláusulas auditadas",
        "demonstrated": "con un fixture que hoy las hace fallar",
        "unproven": "sin ningún fixture negativo",
        "with_control": "canarios con control de contraste",
        "state_demonstrated": "Demostrado",
        "state_unproven": "Sin fixture negativo",
        "control": "control",
        "no_control": "sin control de contraste",
        "no_negative": "ningún fixture del repositorio la hace fallar",
        "empty": "Ninguna cláusula en esta tabla.",
    },
    "en": {
        "audited": "audited clauses",
        "demonstrated": "with a fixture that makes them fail today",
        "unproven": "with no negative fixture at all",
        "with_control": "canaries with a contrast control",
        "state_demonstrated": "Demonstrated",
        "state_unproven": "No negative fixture",
        "control": "control",
        "no_control": "no contrast control",
        "no_negative": "no committed fixture makes it fail",
        "empty": "No clause in this table.",
    },
}


class ClauseMetadata(BaseModel):
    """The descriptive columns of the audit snapshot, kept verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    severity: str | None = None
    domains: list[str] = Field(default_factory=list)


class ClauseStatusRow(BaseModel):
    """One clause and what the gate observed for it."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    kind: str
    severity: str | None = None
    domains: list[str] = Field(default_factory=list)
    state: Literal["demonstrated", "unproven"]
    fixture: str | None = None
    control: str | None = None
    reason: str | None = None

    @property
    def discriminating(self) -> bool:
        return self.control is not None


class StatusTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audited: int
    demonstrated: int
    unproven: int
    canaries: int
    with_control: int
    without_control: int
    beyond_corpus: int


class CanaryStatus(BaseModel):
    """The published state: the audited corpus first, then contract extras."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = STATUS_SCHEMA_VERSION
    source: str = (
        "Every row is the verdict of autopilot.clause_canaries.check_registry,"
        " the same re-run the claims gate performs."
    )
    registry: str = DEFAULT_REGISTRY
    corpus: str
    totals: StatusTotals
    clauses: list[ClauseStatusRow]
    beyond_corpus: list[ClauseStatusRow] = Field(default_factory=list)


def _corpus_rows(root: Path, corpus: str) -> list[dict[str, str]]:
    path = root / corpus
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CanaryError(f"cannot read the corpus {corpus}: {exc}") from exc
    try:
        return list(csv.DictReader(io.StringIO(text)))
    except csv.Error as exc:
        raise CanaryError(f"cannot parse the corpus {corpus}: {exc}") from exc


def corpus_metadata(root: str | Path, corpus: str) -> dict[str, ClauseMetadata]:
    """Clause id to its audit metadata; a missing ``-`` means "not recorded"."""

    metadata: dict[str, ClauseMetadata] = {}
    for row in _corpus_rows(Path(root), corpus):
        clause_id = (row.get("id") or "").strip()
        if not clause_id:
            continue
        severity = (row.get("severidad") or "").strip()
        domains = [
            item.strip() for item in (row.get("dominio") or "").split(",") if item.strip()
        ]
        metadata[clause_id] = ClauseMetadata(
            kind=(row.get("tipo") or "").strip(),
            severity=None if severity in ("", "-") else severity,
            domains=domains,
        )
    return metadata


def _row(
    clause_id: str,
    *,
    metadata: dict[str, ClauseMetadata],
    registry: CanaryRegistry,
    observations: dict[tuple[str, str], str],
    unproven: dict[str, str],
) -> ClauseStatusRow:
    meta = metadata.get(clause_id, ClauseMetadata(kind=""))
    case = registry.by_clause().get(clause_id)
    if case is not None:
        status = observations.get((clause_id, "canary"))
        if status != ClauseStatus.failed.value:
            raise CanaryError(
                f"{clause_id}: the canary {case.fixture} reports {status}, so the"
                " site cannot publish it as demonstrated"
            )
        return ClauseStatusRow(
            clause_id=clause_id,
            kind=meta.kind,
            severity=meta.severity,
            domains=meta.domains,
            state="demonstrated",
            fixture=case.fixture,
            control=case.control,
        )
    reason = unproven.get(clause_id)
    if reason is None:
        raise CanaryError(
            f"{clause_id}: no canary and no unproven entry, so the site has"
            " nothing honest to say about it"
        )
    return ClauseStatusRow(
        clause_id=clause_id,
        kind=meta.kind,
        severity=meta.severity,
        domains=meta.domains,
        state="unproven",
        reason=reason,
    )


def build_status(
    registry: CanaryRegistry,
    root: str | Path,
    *,
    report: CanaryReport | None = None,
    registry_path: str | None = None,
) -> CanaryStatus:
    """Re-run the gate over ``root`` and describe every clause it accounts for."""

    base = Path(root)
    if report is None:
        report = check_registry(registry, base)
    if report.problems:
        raise CanaryError(
            "the gate rejects this state, so it cannot be published: "
            + "; ".join(report.problems)
        )

    observations = {
        (item.clause_id, item.role): item.status for item in report.observations
    }
    metadata = corpus_metadata(base, registry.corpus)
    corpus = corpus_clause_ids(base, registry.corpus)
    unproven = {item.clause_id: item.reason for item in registry.unproven}
    by_clause = registry.by_clause()

    clauses = [
        _row(
            clause_id,
            metadata=metadata,
            registry=registry,
            observations=observations,
            unproven=unproven,
        )
        for clause_id in corpus
    ]
    extras = sorted((set(by_clause) | set(unproven)) - set(corpus))
    beyond = [
        _row(
            clause_id,
            metadata=metadata,
            registry=registry,
            observations=observations,
            unproven=unproven,
        )
        for clause_id in extras
    ]

    canaries = list(by_clause.values())
    return CanaryStatus(
        registry=registry_path or DEFAULT_REGISTRY,
        corpus=registry.corpus,
        totals=StatusTotals(
            audited=len(corpus),
            demonstrated=sum(1 for row in clauses if row.state == "demonstrated"),
            unproven=sum(1 for row in clauses if row.state == "unproven"),
            canaries=len(canaries),
            with_control=sum(1 for case in canaries if case.discriminating),
            without_control=sum(1 for case in canaries if not case.discriminating),
            beyond_corpus=len(beyond),
        ),
        clauses=clauses,
        beyond_corpus=beyond,
    )


# ---------------------------------------------------------------------------
# Rendering: the JSON, then the two regions each page lets us own.
# ---------------------------------------------------------------------------


def render_status_json(status: CanaryStatus) -> str:
    """Stable JSON for the published data; a diff means the verdicts changed."""

    return (
        json.dumps(
            status.model_dump(mode="json", exclude_none=True),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def _short_path(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def short_domains(domains: list[str]) -> str:
    """Sector names without the contract version, for a readable table cell."""

    seen: list[str] = []
    for domain in domains:
        name = re.sub(r"\s+v\d+(?:\.\d+)*$", "", domain).strip()
        if name and name not in seen:
            seen.append(name)
    return ", ".join(seen)


def _state_cell(row: ClauseStatusRow, lang: str) -> str:
    labels = LABELS[lang]
    if row.state == "unproven":
        return (
            f'<span class="watch">{labels["state_unproven"]}</span>'
            f'<span class="canary-note">{labels["no_negative"]}</span>'
        )
    if row.control is None:
        note = labels["no_control"]
    else:
        note = (
            f'{labels["control"]}: '
            f'<code class="canary-path" title="{html.escape(row.control)}">'
            f"{html.escape(_short_path(row.control))}</code>"
        )
    return (
        f'<span class="pass">{labels["state_demonstrated"]}</span>'
        f'<span class="canary-note">{note}</span>'
    )


def _fixture_cell(row: ClauseStatusRow, lang: str) -> str:
    if row.fixture is None:
        return '<span class="canary-note">—</span>'
    return (
        f'<code class="canary-path canary-path--wrap" title="{html.escape(row.fixture)}">'
        f"{html.escape(_short_path(row.fixture))}</code>"
    )


def _table_row(row: ClauseStatusRow, lang: str, *, indent: str = "          ") -> list[str]:
    severity = row.severity or "—"
    domain = short_domains(row.domains) or "—"
    return [
        f'{indent}<tr data-state="{row.state}" data-kind="{html.escape(row.kind)}"'
        f' data-severity="{html.escape(row.severity or "")}">',
        f"{indent}  <td><code>{html.escape(row.clause_id)}</code></td>",
        f"{indent}  <td>{html.escape(severity)}</td>",
        f"{indent}  <td>{html.escape(domain)}</td>",
        f"{indent}  <td>{_state_cell(row, lang)}</td>",
        f"{indent}  <td>{_fixture_cell(row, lang)}</td>",
        f"{indent}</tr>",
    ]


def render_summary(status: CanaryStatus, lang: str) -> str:
    labels = LABELS[lang]
    totals = status.totals
    cells = (
        (totals.audited, labels["audited"]),
        (totals.demonstrated, labels["demonstrated"]),
        (totals.unproven, labels["unproven"]),
        (totals.with_control, labels["with_control"]),
    )
    lines = [SUMMARY_START]
    lines.append('        <div class="shell proof-grid" data-canary-totals>')
    for value, label in cells:
        lines.append(f"          <div><strong>{value}</strong><span>{label}</span></div>")
    lines.append("        </div>")
    lines.append(SUMMARY_END)
    return "\n".join(lines)


def render_rows(status: CanaryStatus, lang: str) -> str:
    lines = [ROWS_START]
    for row in status.clauses:
        lines.extend(_table_row(row, lang))
    lines.append(ROWS_END)
    return "\n".join(lines)


def render_extras(status: CanaryStatus, lang: str) -> str:
    lines = [EXTRAS_START]
    if not status.beyond_corpus:
        lines.append(
            '          <tr><td colspan="5" class="canary-note">'
            + html.escape(LABELS[lang]["empty"])
            + "</td></tr>"
        )
    for row in status.beyond_corpus:
        lines.extend(_table_row(row, lang))
    lines.append(EXTRAS_END)
    return "\n".join(lines)


REGIONS: tuple[tuple[str, str, str], ...] = (
    ("summary", SUMMARY_START, SUMMARY_END),
    ("rows", ROWS_START, ROWS_END),
    ("extras", EXTRAS_START, EXTRAS_END),
)


def render_region(status: CanaryStatus, lang: str, region: str) -> str:
    if region == "summary":
        return render_summary(status, lang)
    if region == "rows":
        return render_rows(status, lang)
    if region == "extras":
        return render_extras(status, lang)
    raise CanaryError(f"unknown page region: {region}")


def page_problems(status: CanaryStatus, root: str | Path) -> list[str]:
    """Every page region must match what the gate observed; empty means in sync."""

    base = Path(root)
    problems: list[str] = []
    for lang, relative in PAGE_PATHS.items():
        path = base / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{relative}: cannot read the page: {exc}")
            continue
        for region, start, end in REGIONS:
            pattern = re.compile(
                re.escape(start) + r".*?" + re.escape(end), re.DOTALL
            )
            match = pattern.search(text)
            if match is None:
                problems.append(
                    f"{relative}: missing the generated {region} region"
                    f" ({start} … {end})"
                )
                continue
            expected = render_region(status, lang, region)
            if match.group(0) != expected:
                problems.append(
                    f"{relative}: the {region} region does not match the canaries"
                    " it describes"
                )
    return problems


def write_page_regions(status: CanaryStatus, root: str | Path) -> list[str]:
    """Replace the generated regions in place; the prose between them is kept."""

    base = Path(root)
    written: list[str] = []
    for lang, relative in PAGE_PATHS.items():
        path = base / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CanaryError(f"cannot read {relative}: {exc}") from exc
        for region, start, end in REGIONS:
            pattern = re.compile(
                re.escape(start) + r".*?" + re.escape(end), re.DOTALL
            )
            if pattern.search(text) is None:
                raise CanaryError(
                    f"{relative}: missing the markers for the {region} region"
                )
            replacement = render_region(status, lang, region)
            text = pattern.sub(lambda _match, value=replacement: value, text, count=1)
        path.write_text(text, encoding="utf-8")
        written.append(relative)
    return written
