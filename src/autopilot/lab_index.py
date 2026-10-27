"""Run index: which packaged runs may be compared with which.

A package identifies one run. A folder of packages still does not tell a
reader whether two of them answer the same question: a simulation and a model
run can share a scenario name, and two runs of the same experiment can use
different protocol versions. Pooling them produces a rate over unlike things.

This module groups *verified* packages by the triple that decides
comparability — ``experiment``, ``subject_kind`` and ``protocol_version`` — and
records the heterogeneity the triple does not capture:

- instrument versions, because the same protocol run by different code is not
  automatically the same procedure,
- claim boundaries, because two runs can share a protocol and still license
  different claims,
- duplicate digests, because counting one run twice inflates every rate.

Declared limits of this design:

1. The index groups runs. It does not compare their results, pool rates or
   test hypotheses.
2. Including a package requires that it verifies; a package that fails
   verification is listed as rejected and excluded from every group, so a
   tampered file cannot be laundered by appearing in a comparison.
3. A group is comparable under the declared protocol. The index does not
   certify that the protocol measures what it claims.
4. Unsealed entries carry integrity only: the index does not attribute them to
   Integrity Labs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autopilot import __version__ as AUTOPILOT_VERSION
from autopilot.lab_package import (
    LAB_PACKAGE_KIND,
    LabPackage,
    SubjectKind,
    verify_lab_package,
)

RUN_INDEX_SCHEMA = "0.1"
RUN_INDEX_KIND = "run_index"

#: The fields that decide whether two runs answer the same question. They are
#: few on purpose: every extra field narrows comparability, every missing one
#: lets unlike runs be pooled.
COMPARABILITY_FIELDS: tuple[str, ...] = (
    "experiment",
    "subject_kind",
    "protocol_version",
)

DECLARED_LIMITS: tuple[str, ...] = (
    "The index groups runs by protocol identity; it does not compare results, "
    "pool rates or test hypotheses.",
    "A group is comparable only under its declared protocol; the index does "
    "not certify that the protocol measures what it claims.",
    "Rejected packages are excluded from every group; excluding them prevents "
    "silent corruption, it does not establish that the remaining runs are "
    "sound.",
    "An unsealed entry carries integrity only: the index does not attribute it "
    "to Integrity Labs.",
)

RULE_EXPLANATION = (
    "Two runs are directly comparable only when they share the experiment, the "
    "subject kind and the protocol version. A different subject kind means a "
    "different kind of evidence; a different protocol version means a different "
    "procedure; a different experiment means a different mechanism. Runs that "
    "differ in any of the three are listed in separate groups and must not be "
    "pooled."
)

HETEROGENEITY_CHECKS: tuple[str, ...] = (
    "instrument_version: the same protocol run by different lab versions is "
    "flagged, because the procedure may have changed with the code",
    "claim_boundary: runs sharing a protocol must still agree on what they may "
    "claim before their findings are merged",
    "duplicate_digest: a repeated artifact is counted once so that n is not "
    "inflated by copies",
)


class LabComparabilityRule(BaseModel):
    """The grouping rule, stated so a reader can apply it without this code."""

    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(min_length=1)
    explanation: str = Field(min_length=1)
    heterogeneity_checks: list[str] = Field(min_length=1)


class LabIndexEntry(BaseModel):
    """One verified package as it appears in the index."""

    model_config = ConfigDict(extra="forbid")

    experiment: str = Field(min_length=1)
    subject_kind: SubjectKind
    protocol_version: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str = Field(min_length=1)
    claim_boundary: str = Field(min_length=1)
    seal_status: Literal["sealed_by_lab", "unsealed_digest_only"]
    key_id: str | None = None
    execution_mode: Literal["live", "static"]
    lab_version: str = Field(min_length=1)
    source: str | None = None


class LabComparabilityGroup(BaseModel):
    """Runs that share a comparability key, with their remaining differences."""

    model_config = ConfigDict(extra="forbid")

    experiment: str = Field(min_length=1)
    subject_kind: SubjectKind
    protocol_version: str = Field(min_length=1)
    run_count: int = Field(ge=1)
    sealed_count: int = Field(ge=0)
    unsealed_count: int = Field(ge=0)
    instrument_versions: list[str] = Field(min_length=1)
    claim_boundaries: list[str] = Field(min_length=1)
    artifact_sha256: list[str] = Field(min_length=1)
    duplicates_ignored: int = Field(ge=0, default=0)
    warnings: list[str] = Field(default_factory=list)


class LabRejectedPackage(BaseModel):
    """A package the index refused to place in any group."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    experiment: str | None = None
    artifact_sha256: str | None = None
    problems: list[str] = Field(min_length=1)


class LabRunIndex(BaseModel):
    """A set of packaged runs, organised by what can be compared."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = RUN_INDEX_SCHEMA
    index_kind: Literal["run_index"] = RUN_INDEX_KIND
    created_at: str = Field(min_length=1)
    index_version: str = Field(min_length=1)
    comparability_rule: LabComparabilityRule
    package_count: int = Field(ge=0)
    accepted_count: int = Field(
        ge=0,
        description="Distinct artifacts indexed; copies of one digest count once",
    )
    rejected_count: int = Field(ge=0)
    duplicate_count: int = Field(
        ge=0, description="Accepted packages whose digest already appeared in the group"
    )
    groups: list[LabComparabilityGroup] = Field(default_factory=list)
    entries: list[LabIndexEntry] = Field(default_factory=list)
    rejected: list[LabRejectedPackage] = Field(default_factory=list)
    skipped_sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limits: list[str] = Field(min_length=1)


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


def _entry(source: str | None, package: LabPackage) -> LabIndexEntry:
    return LabIndexEntry(
        experiment=package.experiment,
        subject_kind=package.subject_kind,
        protocol_version=package.protocol_version,
        artifact_sha256=package.artifact_sha256,
        created_at=package.created_at,
        claim_boundary=package.claim_boundary,
        seal_status=package.seal_status,
        key_id=package.seal.key_id if package.seal is not None else None,
        execution_mode=package.execution.mode,
        lab_version=package.execution.lab_version,
        source=source,
    )


def _group_warnings(
    *,
    instrument_versions: list[str],
    claim_boundaries: list[str],
    sealed_count: int,
    duplicates_ignored: int,
) -> list[str]:
    warnings: list[str] = []
    if len(instrument_versions) > 1:
        warnings.append(
            "instrument_version_varies: "
            + ", ".join(instrument_versions)
            + " — the same protocol was run by different lab versions; check "
            "whether the procedure changed before pooling"
        )
    if len(claim_boundaries) > 1:
        warnings.append(
            "claim_boundary_varies: "
            + f"{len(claim_boundaries)} distinct boundaries in one group — the "
            "runs may not license the same claim even under one protocol"
        )
    if sealed_count == 0:
        warnings.append(
            "no_run_is_sealed: entries are integrity-only and not attributable "
            "to a key"
        )
    if duplicates_ignored:
        warnings.append(
            f"duplicate_digests_ignored: {duplicates_ignored} repeated "
            "artifact(s) counted once"
        )
    return warnings


def _assemble(
    items: Iterable[tuple[str | None, dict[str, Any]]],
    *,
    created_at: str | None,
    skipped_sources: list[str] | None = None,
) -> LabRunIndex:
    accepted: list[LabIndexEntry] = []
    rejected: list[LabRejectedPackage] = []
    package_count = 0

    for source, payload in items:
        package_count += 1
        verification = verify_lab_package(payload)
        if not verification.valid:
            rejected.append(
                LabRejectedPackage(
                    source=source,
                    experiment=payload.get("experiment") or None,
                    artifact_sha256=payload.get("artifact_sha256") or None,
                    problems=verification.problems or ["package did not verify"],
                )
            )
            continue
        try:
            package = LabPackage.model_validate(payload)
        except ValidationError as exc:
            rejected.append(
                LabRejectedPackage(
                    source=source,
                    experiment=payload.get("experiment") or None,
                    artifact_sha256=payload.get("artifact_sha256") or None,
                    problems=[f"package does not match the package schema: {exc}"],
                )
            )
            continue
        accepted.append(_entry(source, package))

    buckets: dict[tuple[str, SubjectKind, str], list[LabIndexEntry]] = {}
    for entry in accepted:
        key = (entry.experiment, entry.subject_kind, entry.protocol_version)
        buckets.setdefault(key, []).append(entry)

    groups: list[LabComparabilityGroup] = []
    duplicate_count = 0
    digest_keys: dict[str, set[tuple[str, SubjectKind, str]]] = {}
    for key in sorted(buckets):
        members = sorted(
            buckets[key], key=lambda entry: (entry.created_at, entry.artifact_sha256)
        )
        unique: list[LabIndexEntry] = []
        seen: set[str] = set()
        for entry in members:
            digest_keys.setdefault(entry.artifact_sha256, set()).add(key)
            if entry.artifact_sha256 in seen:
                continue
            seen.add(entry.artifact_sha256)
            unique.append(entry)
        duplicates = len(members) - len(unique)
        duplicate_count += duplicates
        sealed_count = sum(1 for e in unique if e.seal_status == "sealed_by_lab")
        instrument_versions = sorted({e.lab_version for e in unique})
        claim_boundaries = sorted({e.claim_boundary for e in unique})
        groups.append(
            LabComparabilityGroup(
                experiment=key[0],
                subject_kind=key[1],
                protocol_version=key[2],
                run_count=len(unique),
                sealed_count=sealed_count,
                unsealed_count=len(unique) - sealed_count,
                instrument_versions=instrument_versions,
                claim_boundaries=claim_boundaries,
                artifact_sha256=[e.artifact_sha256 for e in unique],
                duplicates_ignored=duplicates,
                warnings=_group_warnings(
                    instrument_versions=instrument_versions,
                    claim_boundaries=claim_boundaries,
                    sealed_count=sealed_count,
                    duplicates_ignored=duplicates,
                ),
            )
        )

    warnings: list[str] = []
    for digest in sorted(digest_keys):
        keys = digest_keys[digest]
        if len(keys) > 1:
            described = "; ".join(
                f"{experiment} / {subject} / {protocol}"
                for experiment, subject, protocol in sorted(keys)
            )
            warnings.append(
                f"artifact {digest[:16]} appears under {len(keys)} comparability "
                f"keys ({described}); one run cannot be two protocols"
            )
    if skipped_sources:
        warnings.append(
            f"skipped_sources: {len(skipped_sources)} file(s) are not run "
            "packages and were not indexed"
        )

    moment = created_at or _now()
    parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at must include a timezone")

    accepted_unique = sum(group.run_count for group in groups)
    return LabRunIndex(
        created_at=parsed.isoformat(),
        index_version=AUTOPILOT_VERSION,
        comparability_rule=LabComparabilityRule(
            fields=list(COMPARABILITY_FIELDS),
            explanation=RULE_EXPLANATION,
            heterogeneity_checks=list(HETEROGENEITY_CHECKS),
        ),
        package_count=package_count,
        accepted_count=accepted_unique,
        rejected_count=len(rejected),
        duplicate_count=duplicate_count,
        groups=groups,
        entries=accepted,
        rejected=rejected,
        skipped_sources=list(skipped_sources or []),
        warnings=warnings,
        limits=list(DECLARED_LIMITS),
    )


def build_run_index(
    packages: Iterable[LabPackage | dict[str, Any]],
    *,
    created_at: str | None = None,
) -> LabRunIndex:
    """Index package documents or package models, verifying each one first."""

    return _assemble(
        ((None, _payload(item)) for item in packages),
        created_at=created_at,
    )


def collect_package_files(paths: Iterable[Path]) -> list[Path]:
    """List JSON files under the given paths, without reading them."""

    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.json")))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"no such package path: {path}")
    return files


def build_run_index_from_paths(
    paths: Iterable[Path],
    *,
    created_at: str | None = None,
) -> LabRunIndex:
    """Index every run package found on disk; ignore JSON that is not one."""

    items: list[tuple[str | None, dict[str, Any]]] = []
    skipped: list[str] = []
    for path in collect_package_files(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped.append(str(path))
            continue
        if not isinstance(payload, dict) or payload.get("package_kind") != LAB_PACKAGE_KIND:
            skipped.append(str(path))
            continue
        items.append((str(path), payload))
    return _assemble(items, created_at=created_at, skipped_sources=skipped)


def render_run_index_text(index: LabRunIndex) -> str:
    """Human-readable listing: one block per comparability group."""

    lines: list[str] = [
        f"Indice de corridas · {index.package_count} paquete(s) · "
        f"{len(index.groups)} grupo(s) comparable(s) · "
        f"{index.rejected_count} rechazado(s)"
    ]
    lines.append(
        "Regla: comparable solo con igual "
        + ", ".join(index.comparability_rule.fields)
        + "."
    )
    for position, group in enumerate(index.groups, start=1):
        lines.append("")
        lines.append(
            f"[{position}] {group.experiment} · {group.subject_kind} · "
            f"{group.protocol_version}"
        )
        lines.append(
            f"    corridas {group.run_count} "
            f"({group.sealed_count} selladas, {group.unsealed_count} sin sello) · "
            f"instrumento {', '.join(group.instrument_versions)}"
        )
        lines.append("    digests: " + ", ".join(d[:16] for d in group.artifact_sha256))
        for warning in group.warnings:
            lines.append(f"    aviso: {warning}")
    if index.warnings:
        lines.append("")
        for warning in index.warnings:
            lines.append(f"indice: {warning}")
    if index.rejected:
        lines.append("")
        lines.append("Rechazados (fuera de todo grupo):")
        for rejected in index.rejected:
            label = rejected.source or rejected.artifact_sha256 or "(sin origen)"
            lines.append(f"  {label}: {rejected.problems[0]}")
    return "\n".join(lines)


def write_run_index(index: LabRunIndex, path: Path) -> Path:
    """Write the index as stable, diff-friendly JSON."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "COMPARABILITY_FIELDS",
    "DECLARED_LIMITS",
    "HETEROGENEITY_CHECKS",
    "RULE_EXPLANATION",
    "RUN_INDEX_KIND",
    "RUN_INDEX_SCHEMA",
    "LabComparabilityGroup",
    "LabComparabilityRule",
    "LabIndexEntry",
    "LabRejectedPackage",
    "LabRunIndex",
    "build_run_index",
    "build_run_index_from_paths",
    "collect_package_files",
    "render_run_index_text",
    "write_run_index",
]
