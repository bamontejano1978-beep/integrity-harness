"""Citable export packages for Web Lab runs.

A run in the lab produces an *artifact*: parameters, results and a canonical
``artifact_sha256``. That artifact is enough to check integrity, but it is not
enough to cite or archive: it does not say which version of the lab produced
it, what the run may and may not be used to claim, or whether it was sealed.

This module wraps one artifact into a self-contained package:

- identity of the instrument (``execution.lab_version``, schema versions),
- what was studied: ``subject_kind`` and ``protocol_version``, so a reader
  cannot mistake a simulated policy for a model or for a live runtime,
- the artifact exactly as sealed, with its digest,
- the declared claim boundary copied from the artifact,
- a citation block a reader can paste into a reference list,
- the declared limits of what the package does and does not attest,
- an optional ``LabSignature`` and an explicit ``seal_status``.

Declared limits of this design:

1. The digest proves the artifact is internally unchanged since it was stamped.
   It proves nothing about who produced it, because anyone can compute it.
2. Only a seal can be attributed to a key, and even then only if the reader
   trusts that key. ``seal_status`` distinguishes the two cases instead of
   letting an unsealed package look sealed.
3. Sealing happens where the private key lives. A local instance without that
   key exports ``unsealed_digest_only``; it never receives a private key.
4. The package is about one run. It does not certify the mechanism, the
   protocol, or the organisation that requested it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from autopilot import __version__ as AUTOPILOT_VERSION
from autopilot.lab_evidence import (
    LAB_EVIDENCE_SCHEMA,
    LabSignature,
    artifact_digest,
    sign_lab_artifact,
    verify_lab_artifact,
)

LAB_PACKAGE_SCHEMA = "0.1"
LAB_PACKAGE_KIND = "citable_run"

#: What produced the behaviour in the artifact. Required, because a package
#: that omits it invites the reader to assume the strongest case.
SubjectKind = Literal["simulated_policy", "llm_model", "runtime", "trajectory_fixture"]
SUBJECT_KINDS: tuple[SubjectKind, ...] = (
    "simulated_policy",
    "llm_model",
    "runtime",
    "trajectory_fixture",
)

#: Protocol family each deterministic simulation declares today. These are the
#: seed families the runners already use, not new identifiers invented here.
SIMULATION_PROTOCOLS: dict[str, str] = {
    "ringelmann": "ringelmann-v0.1",
    "sherif": "sherif-v0.1",
    "asch": "asch-v0.1",
    "bystander": "bystander-v0.1",
    "cascade": "cascade-v0.1",
}

_SIMULATION_KINDS = frozenset(
    {
        "deterministic_mechanism_simulation",
        "mediated_channel_mechanism_simulation",
        "mediated_channel_composition_evaluation",
    }
)

#: What the package attests, in the wording that travels with every export.
DECLARED_LIMITS: tuple[str, ...] = (
    "The digest attests that the artifact is unchanged since it was stamped; "
    "it does not attest who produced it.",
    "A seal attests that this artifact was sealed by that key at that moment; "
    "it does not attest that the simulation describes people or live systems.",
    "The package covers one run under declared parameters and seed. It does "
    "not certify the mechanism, the protocol version, or the requesting "
    "organisation.",
    "An unsealed package carries integrity only: a reader cannot attribute it "
    "to Integrity Labs.",
)


class LabExecution(BaseModel):
    """How and with what instrument the run was produced."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["live", "static"]
    lab_version: str = Field(min_length=1)


class LabCitation(BaseModel):
    """Ready-to-paste citation for a reference list."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    experiment: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str = Field(min_length=1)


class LabVerificationGuide(BaseModel):
    """Everything a third party needs to re-check the package offline."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["Ed25519", "none"]
    canonicalization: str = Field(min_length=1)
    offline_check: str = Field(min_length=1)


class LabPackage(BaseModel):
    """One run packaged for sharing, archiving and citation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = LAB_PACKAGE_SCHEMA
    package_kind: Literal["citable_run"] = LAB_PACKAGE_KIND
    experiment: str = Field(min_length=1)
    subject_kind: SubjectKind
    protocol_version: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    execution: LabExecution
    artifact: dict[str, Any]
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    claim_boundary: str = Field(min_length=1)
    citation: LabCitation
    seal_status: Literal["sealed_by_lab", "unsealed_digest_only"]
    seal: LabSignature | None = None
    limits: list[str] = Field(min_length=1)
    verification: LabVerificationGuide


class LabPackageVerification(BaseModel):
    """Result of checking a package document, seal included when present."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    experiment: str
    artifact_sha256: str
    digest_matches: bool
    seal_present: bool
    seal_valid: bool | None
    problems: list[str] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_claim_boundary(artifact: Any) -> str:
    claim = artifact.get("claim_boundary") if isinstance(artifact, dict) else None
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError(
            "artifact declares no claim_boundary; refusing to package an undeclared claim"
        )
    return claim.strip()


def _require_consistent_subject(artifact: Any, declared: str) -> None:
    """Refuse a declaration that contradicts what the artifact shows.

    ``subject_kind`` is part of the comparability key of a run, so it cannot be
    a free label: an artifact produced by a deterministic simulation must not
    be packaged as a model run, even if the caller asks for it.
    """

    if not isinstance(artifact, dict):
        return
    try:
        derived = subject_kind_for(artifact)
    except ValueError:
        return
    if derived != declared:
        raise ValueError(
            f"subject_kind {declared!r} contradicts the artifact, which was "
            f"produced by {derived!r}; comparability depends on this field"
        )


def _citation_text(
    experiment: str,
    digest: str,
    created_at: str,
    *,
    subject_kind: str,
    protocol_version: str,
) -> str:
    date = created_at[:10]
    return (
        f"Integrity Labs ({date}). Integrity Web Lab — {experiment} run "
        f"{digest[:16]} ({subject_kind}, protocol {protocol_version}). "
        f"Artifact digest {digest}. Package schema {LAB_PACKAGE_SCHEMA}."
    )


def subject_kind_for(artifact: dict[str, Any]) -> SubjectKind:
    """Read what produced the behaviour from the artifact itself.

    Never guess: an artifact whose kind or contract fields do not identify a
    subject is refused rather than labelled with the lab's most common case.
    """

    kind = str(artifact.get("experiment_kind") or "")
    if kind in _SIMULATION_KINDS:
        return "simulated_policy"
    if artifact.get("contract_id") and artifact.get("trajectory_id"):
        return "trajectory_fixture"
    declared = artifact.get("subject_kind")
    if declared in SUBJECT_KINDS:
        return cast(SubjectKind, declared)
    raise ValueError(
        "artifact does not identify what produced the behaviour; "
        "state subject_kind explicitly"
    )


def protocol_version_for(artifact: dict[str, Any], experiment: str) -> str:
    """Read the protocol identity from the artifact, without inventing one."""

    declared = artifact.get("protocol_version")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    known = SIMULATION_PROTOCOLS.get(experiment)
    if known is not None:
        return known
    contract = artifact.get("contract")
    if isinstance(contract, dict):
        schema = contract.get("schema_version")
        if isinstance(schema, str) and schema.strip():
            return f"swarm-contract-{schema.strip()}"
    contract_id = artifact.get("contract_id")
    contract_version = artifact.get("contract_version")
    if contract_id and contract_version:
        return f"{contract_id}@{contract_version}"
    raise ValueError(
        "artifact declares no protocol version; pass protocol_version explicitly"
    )


def _verification_guide(*, sealed: bool) -> LabVerificationGuide:
    return LabVerificationGuide(
        algorithm="Ed25519" if sealed else "none",
        canonicalization=(
            "artifact_sha256 = SHA-256 of the artifact serialised as JSON with "
            "sorted keys, no whitespace, ASCII escapes, every number formatted "
            "with 17 significant digits, and the fields duration_ms, "
            "artifact_sha256 and signature removed"
        ),
        offline_check=(
            "recompute artifact_digest(package['artifact']) and compare it with "
            "artifact_sha256"
            + (
                "; then verify_lab_artifact(package['artifact'], package['seal'])"
                if sealed
                else "; no seal is present, so the package is not attributable"
            )
        ),
    )


def build_lab_package(
    artifact: dict[str, Any],
    *,
    experiment: str,
    subject_kind: SubjectKind,
    protocol_version: str | None = None,
    execution_mode: Literal["live", "static"] = "live",
    created_at: str | None = None,
    key_dir: Path | None = None,
    signed_at: str | None = None,
) -> LabPackage:
    """Wrap one run into a citable package, sealing it when a key is provided.

    Passing ``key_dir`` means this process holds the laboratory key. Callers on
    a researcher's machine pass nothing and get an unsealed package rather than
    a private key.
    """

    experiment_name = experiment.strip()
    if not experiment_name:
        raise ValueError("experiment must be a non-empty name")
    if subject_kind not in SUBJECT_KINDS:
        raise ValueError(
            f"subject_kind must be one of {', '.join(SUBJECT_KINDS)}"
        )

    digest = artifact_digest(artifact)
    _require_consistent_subject(artifact, subject_kind)
    stamped = artifact.get("artifact_sha256") if isinstance(artifact, dict) else None
    if stamped is not None and stamped != digest:
        raise ValueError(
            "artifact_sha256 does not match the artifact content (mutated after stamping)"
        )

    claim_boundary = _require_claim_boundary(artifact)
    moment = created_at or _now()
    try:
        parsed_moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an RFC 3339 timestamp") from exc
    if parsed_moment.tzinfo is None:
        raise ValueError("created_at must include a timezone")

    seal = (
        sign_lab_artifact(
            artifact,
            experiment=experiment_name,
            key_dir=key_dir,
            signed_at=signed_at,
        )
        if key_dir is not None
        else None
    )

    protocol = (
        protocol_version.strip()
        if isinstance(protocol_version, str) and protocol_version.strip()
        else protocol_version_for(artifact, experiment_name)
    )
    moment_text = parsed_moment.isoformat()

    return LabPackage(
        experiment=experiment_name,
        subject_kind=subject_kind,
        protocol_version=protocol,
        created_at=moment_text,
        execution=LabExecution(mode=execution_mode, lab_version=AUTOPILOT_VERSION),
        artifact=artifact,
        artifact_sha256=digest,
        claim_boundary=claim_boundary,
        citation=LabCitation(
            text=_citation_text(
                experiment_name,
                digest,
                moment_text,
                subject_kind=subject_kind,
                protocol_version=protocol,
            ),
            experiment=experiment_name,
            artifact_sha256=digest,
            created_at=moment_text,
        ),
        seal_status="sealed_by_lab" if seal is not None else "unsealed_digest_only",
        seal=seal,
        limits=list(DECLARED_LIMITS),
        verification=_verification_guide(sealed=seal is not None),
    )


def write_lab_package(package: LabPackage, path: Path) -> Path:
    """Write the package as stable, diff-friendly JSON."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(package.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return path


def verify_lab_package(payload: dict[str, Any]) -> LabPackageVerification:
    """Check one package document without trusting any of its fields.

    Reports every problem it can rather than stopping at the first, so a reader
    sees whether the failure is content, metadata or seal.
    """

    problems: list[str] = []
    experiment = str(payload.get("experiment") or "")
    declared_digest = str(payload.get("artifact_sha256") or "")
    artifact = payload.get("artifact")

    if payload.get("package_kind") != LAB_PACKAGE_KIND:
        problems.append(f"package_kind must be {LAB_PACKAGE_KIND!r}")
    if payload.get("schema_version") != LAB_PACKAGE_SCHEMA:
        problems.append(f"unsupported package schema: {payload.get('schema_version')!r}")
    if not experiment:
        problems.append("experiment is missing")
    if payload.get("subject_kind") not in SUBJECT_KINDS:
        problems.append(
            "subject_kind is missing or not one of " + ", ".join(SUBJECT_KINDS)
        )
    if not str(payload.get("protocol_version") or "").strip():
        problems.append("protocol_version is missing or empty")
    if isinstance(artifact, dict) and payload.get("subject_kind") in SUBJECT_KINDS:
        try:
            derived_subject = subject_kind_for(artifact)
        except ValueError:
            derived_subject = None
        if derived_subject is not None and derived_subject != payload["subject_kind"]:
            problems.append(
                "subject_kind does not match what produced the artifact "
                f"(artifact is {derived_subject!r})"
            )
    if not str(payload.get("claim_boundary") or "").strip():
        problems.append("claim_boundary is missing or empty")
    elif isinstance(artifact, dict):
        artifact_claim = artifact.get("claim_boundary")
        if isinstance(artifact_claim, str) and artifact_claim.strip() and (
            artifact_claim.strip() != str(payload["claim_boundary"]).strip()
        ):
            problems.append(
                "claim_boundary does not match the artifact's declared boundary"
            )

    citation = payload.get("citation")
    if not isinstance(citation, dict):
        problems.append("citation block is missing")
    else:
        if citation.get("artifact_sha256") != declared_digest:
            problems.append("citation artifact_sha256 does not match the package")
        if citation.get("experiment") not in (None, experiment):
            problems.append("citation experiment does not match the package")

    try:
        computed = artifact_digest(artifact)
    except (TypeError, ValueError) as exc:
        return LabPackageVerification(
            valid=False,
            experiment=experiment,
            artifact_sha256=declared_digest,
            digest_matches=False,
            seal_present=payload.get("seal") is not None,
            seal_valid=False,
            problems=problems + [f"artifact is not canonicalisable JSON: {exc}"],
        )

    digest_matches = bool(declared_digest) and computed == declared_digest
    if not digest_matches:
        problems.append(
            "artifact digest does not match artifact_sha256 (the artifact changed)"
        )
    if isinstance(artifact, dict):
        stamped = artifact.get("artifact_sha256")
        if stamped is not None and stamped != computed:
            problems.append("artifact carries a stale artifact_sha256 of its own")

    raw_seal = payload.get("seal")
    seal_present = raw_seal is not None
    seal_valid: bool | None = None
    if seal_present:
        try:
            seal = LabSignature.model_validate(raw_seal)
        except (TypeError, ValueError) as exc:
            problems.append(f"seal is invalid: {exc}")
            seal_valid = False
        else:
            if seal.experiment != experiment:
                problems.append("seal experiment does not match the package")
            verification = verify_lab_artifact(artifact, seal)
            seal_valid = verification.valid
            problems.extend(verification.problems)

    expected_status = "sealed_by_lab" if seal_present else "unsealed_digest_only"
    if payload.get("seal_status") != expected_status:
        problems.append(
            f"seal_status must be {expected_status!r} for this package"
        )

    return LabPackageVerification(
        valid=digest_matches and not problems,
        experiment=experiment,
        artifact_sha256=computed,
        digest_matches=digest_matches,
        seal_present=seal_present,
        seal_valid=seal_valid,
        problems=problems,
    )


__all__ = [
    "DECLARED_LIMITS",
    "LAB_EVIDENCE_SCHEMA",
    "LAB_PACKAGE_KIND",
    "LAB_PACKAGE_SCHEMA",
    "SIMULATION_PROTOCOLS",
    "SUBJECT_KINDS",
    "LabCitation",
    "LabExecution",
    "LabPackage",
    "LabPackageVerification",
    "LabVerificationGuide",
    "SubjectKind",
    "build_lab_package",
    "protocol_version_for",
    "subject_kind_for",
    "verify_lab_package",
    "write_lab_package",
]
