"""Cryptographically signed human review decisions for sandbox reports."""

from __future__ import annotations

import base64
import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import pydantic_issues
from autopilot.contracts.models import (
    IDENTIFIER_PATTERN,
    SEMVER_PATTERN,
    SNAKE_NAME_PATTERN,
    ContractDocument,
)
from autopilot.sandbox import SandboxReport, SandboxStatus
from autopilot.trajectory import parse_timestamp

REVIEWER_NAME = "signed_staged_release_reviewer"
REVIEWER_VERSION = "0.1.0"

# B2 chain demo constants (consumed by the chain test contract).
contract_id_for_chain = "CHAIN-DEMO-CONTRACT"
contract_version_for_chain = "0.1.0"
clause_id_for_chain = "CHAIN-DEMO-CLAUSE-DISCLOSE-001"
CHAIN_DEMO_ACTORS: tuple[str, ...] = (
    "head_of_ai_risk",
    "deputy_ai_governance",
    "claims_deputy_x",
)


class ReviewDecision(str, Enum):
    approve_staged_release = "approve_staged_release"
    reject = "reject"


class ReviewerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    role: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]


class SignatureBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    signed_payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    value_base64: str


class DelegationLevel(BaseModel):
    """One level of a B2 hierarchical approval chain.

    Each level records the actor and role that produced this signature, the
    16-hex-digit SHA-256 prefix of the public key that signed it, the SHA-256
    of the canonical bytes the level signed (``signed_payload_sha256``), and
    the Ed25519 signature itself (``value_base64``).

    The leaf level (``levels[-1]``) signs the canonical unsigned review
    payload (same as ``ReleaseReview.signature``). Every higher level signs
    a payload that wraps the lower level's canonical JSON representation
    together with the contract / clause context.
    """

    model_config = ConfigDict(extra="forbid")

    actor: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    role: Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    signed_payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    value_base64: str


class DelegationChain(BaseModel):
    """B2 additive pre-stage hierarchical signing chain.

    ``expected_chain_actors`` captures the ``delegate_to`` ordering declared
    in the DSL contract at sign time so the verifier can attest to the
    policy binding even before re-reading the live contract. ``levels`` is
    the self-contained cryptographic evidence in ``root → middle → leaf``
    order; v0.1 reviews omit the field entirely and retain single-signature
    semantics.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    contract_version: Annotated[str, Field(pattern=SEMVER_PATTERN)]
    clause_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
    expected_chain_actors: list[
        Annotated[str, Field(pattern=SNAKE_NAME_PATTERN)]
    ] = Field(min_length=1)
    levels: list[DelegationLevel] = Field(min_length=2)

    @field_validator("levels")
    @classmethod
    def levels_are_actor_unique(
        cls, value: list[DelegationLevel]
    ) -> list[DelegationLevel]:
        actors = [level.actor for level in value]
        duplicates = sorted({actor for actor in actors if actors.count(actor) > 1})
        if duplicates:
            raise ValueError(
                "chain levels must list each actor exactly once: "
                + ", ".join(duplicates)
            )
        return value


class ReleaseReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    review_id: str
    decided_at: str
    reference: Annotated[str, Field(min_length=3, max_length=255)]
    reviewer: ReviewerIdentity
    decision: ReviewDecision
    rationale: Annotated[str, Field(min_length=20, max_length=4000)]
    sandbox_report: SandboxReport
    staged_release_authorized: bool
    production_deployment_allowed: Literal[False] = False
    automatic_application: Literal[False] = False
    automatic_deployment: Literal[False] = False
    requires_separate_deployment_authorization: Literal[True] = True
    signature: SignatureBlock
    reviewer_name: str = REVIEWER_NAME
    reviewer_version: str = REVIEWER_VERSION
    disclaimer: str = (
        "This signed decision may authorize staged-release preparation only. "
        "It does not apply a change or authorize production deployment."
    )
    # B2 additive pre-stage: optional hierarchical signing chain that wraps
    # the leaf signature. When set, ``levels[-1]`` MUST be structurally
    # identical to ``signature`` (the leaf is the delegate). The chain is
    # ordered root → middle → leaf and is verified from the bottom up:
    # the leaf signs the unsigned review payload, each higher level
    # signs a payload that wraps the lower level's canonical
    # representation plus the contract / clause context.
    delegation_chain: DelegationChain | None = None

    @field_validator("decided_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @model_validator(mode="after")
    def chain_leaf_matches_signature(self) -> ReleaseReview:
        if self.delegation_chain is None:
            return self
        leaf_level = self.delegation_chain.levels[-1]
        signature = self.signature
        if (
            leaf_level.signed_payload_sha256 != signature.signed_payload_sha256
            or leaf_level.value_base64 != signature.value_base64
            or leaf_level.key_id != signature.key_id
        ):
            raise ValueError(
                "delegation_chain.levels[-1] must structurally equal signature "
                "(same key_id, signed_payload_sha256, and value_base64)"
            )
        if leaf_level.actor != self.reviewer.actor:
            raise ValueError(
                "delegation_chain.levels[-1].actor must equal reviewer.actor"
            )
        return self


class ReviewVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    valid: bool
    key_id_matches: bool
    payload_digest_matches: bool
    review_id_matches: bool
    signature_valid: bool
    semantics_valid: bool
    staged_release_authorized: bool
    production_deployment_allowed: Literal[False] = False
    problems: list[str]
    # B2 chain fields (``None`` in v0.1 single-signature mode).
    delegation_chain_present: bool | None = None
    chain_actors_match_reviewer: bool | None = None
    delegation_chain_valid: bool | None = None
    chain_actor_count: int | None = None
    chain_dsl_chain_matches_live_contract: bool | None = None
    chain_separation_of_duties_ok: bool | None = None


def _canonical_bytes(payload: dict | list | str | int | float | bool | None) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _key_id(public_key: Ed25519PublicKey) -> str:
    encoded = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(encoded).hexdigest()[:16]


def _unsigned_payload(
    *,
    decided_at: str,
    reference: str,
    reviewer: ReviewerIdentity,
    decision: ReviewDecision,
    rationale: str,
    sandbox_report: SandboxReport,
    staged_release_authorized: bool,
) -> dict:
    return {
        "schema_version": "0.1",
        "decided_at": decided_at,
        "reference": reference,
        "reviewer": reviewer.model_dump(mode="json"),
        "decision": decision.value,
        "rationale": rationale,
        "sandbox_report": sandbox_report.model_dump(mode="json"),
        "staged_release_authorized": staged_release_authorized,
        "production_deployment_allowed": False,
        "automatic_application": False,
        "automatic_deployment": False,
        "requires_separate_deployment_authorization": True,
        "reviewer_name": REVIEWER_NAME,
        "reviewer_version": REVIEWER_VERSION,
    }


def _payload_from_review(review: ReleaseReview) -> dict:
    return _unsigned_payload(
        decided_at=review.decided_at,
        reference=review.reference,
        reviewer=review.reviewer,
        decision=review.decision,
        rationale=review.rationale,
        sandbox_report=review.sandbox_report,
        staged_release_authorized=review.staged_release_authorized,
    )


def _level_signing_payload(
    *,
    contract_id: str,
    contract_version: str,
    clause_id: str,
    level_actor: str,
    level_role: str,
    lower_signature_canonical_repr: str,
) -> bytes:
    return _canonical_bytes({
        "contract_id": contract_id,
        "contract_version": contract_version,
        "clause_id": clause_id,
        "level_actor": level_actor,
        "level_role": level_role,
        "lower_signature_canonical_repr": lower_signature_canonical_repr,
    })


def _level_canonical_repr(level: DelegationLevel) -> bytes:
    return _canonical_bytes(level.model_dump(mode="json"))


def _sign_leaf_level(
    *,
    unsigned_payload_bytes: bytes,
    private_key: Ed25519PrivateKey,
    actor: str,
    role: str,
) -> DelegationLevel:
    public_key = private_key.public_key()
    digest = hashlib.sha256(unsigned_payload_bytes).hexdigest()
    signature = base64.b64encode(private_key.sign(unsigned_payload_bytes)).decode("ascii")
    return DelegationLevel(
        actor=actor,
        role=role,
        key_id=_key_id(public_key),
        signed_payload_sha256=digest,
        value_base64=signature,
    )


def _sign_chain_level(
    *,
    contract_id: str,
    contract_version: str,
    clause_id: str,
    level_actor: str,
    level_role: str,
    lower_signature_canonical_repr: bytes,
    private_key: Ed25519PrivateKey,
) -> DelegationLevel:
    public_key = private_key.public_key()
    payload = _level_signing_payload(
        contract_id=contract_id,
        contract_version=contract_version,
        clause_id=clause_id,
        level_actor=level_actor,
        level_role=level_role,
        lower_signature_canonical_repr=lower_signature_canonical_repr.decode("utf-8"),
    )
    digest = hashlib.sha256(payload).hexdigest()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    return DelegationLevel(
        actor=level_actor,
        role=level_role,
        key_id=_key_id(public_key),
        signed_payload_sha256=digest,
        value_base64=signature,
    )


def generate_review_keypair(
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> str:
    """Create an Ed25519 key pair without overwriting either target."""

    private_path = Path(private_key_path)
    public_path = Path(public_key_path)
    issues: list[ValidationIssue] = []
    if private_path.exists():
        issues.append(ValidationIssue("private_key", f"already exists: {private_path}"))
    if public_path.exists():
        issues.append(ValidationIssue("public_key", f"already exists: {public_path}"))
    if private_path.resolve() == public_path.resolve():
        issues.append(ValidationIssue("public_key", "must differ from the private-key path"))
    if issues:
        raise DSLValidationError(issues)

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with private_path.open("xb") as handle:
            handle.write(private_bytes)
        with public_path.open("xb") as handle:
            handle.write(public_bytes)
    except OSError as exc:
        raise DSLValidationError(
            [ValidationIssue("key_file", f"could not write key pair: {exc}")]
        ) from exc
    return _key_id(public_key)


def _load_private_key(path: str | Path) -> Ed25519PrivateKey:
    resolved = Path(path)
    try:
        payload = resolved.read_bytes()
        key = serialization.load_pem_private_key(payload, password=None)
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("private_key", f"not found: {resolved}")]
        ) from exc
    except (OSError, ValueError) as exc:
        raise DSLValidationError(
            [ValidationIssue("private_key", "is not a valid unencrypted PEM private key")]
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise DSLValidationError(
            [ValidationIssue("private_key", "must contain an Ed25519 private key")]
        )
    return key


def _load_public_key(path: str | Path) -> Ed25519PublicKey:
    resolved = Path(path)
    try:
        payload = resolved.read_bytes()
        key = serialization.load_pem_public_key(payload)
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("public_key", f"not found: {resolved}")]
        ) from exc
    except (OSError, ValueError) as exc:
        raise DSLValidationError(
            [ValidationIssue("public_key", "is not a valid PEM public key")]
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise DSLValidationError(
            [ValidationIssue("public_key", "must contain an Ed25519 public key")]
        )
    return key


def _resolve_public_keys(
    public_keys: Mapping[str, str | Path | Ed25519PublicKey] | str | Path | Ed25519PublicKey,
) -> dict[str, Ed25519PublicKey]:
    """Normalize the polymorphic ``public_keys`` argument to a dict.

    Single-key form (Path | str | Ed25519PublicKey) is mapped to the
    sentinel ``_leaf`` key for use inside single-signature verification; for
    chain mode callers MUST pass a Mapping[str, ...].
    """

    if isinstance(public_keys, Mapping):
        resolved: dict[str, Ed25519PublicKey] = {}
        for actor, value in public_keys.items():
            if isinstance(value, Ed25519PublicKey):
                resolved[actor] = value
                continue
            if isinstance(value, Path) or isinstance(value, str):
                resolved[actor] = _load_public_key(Path(value))
                continue
            raise DSLValidationError(
                [
                    ValidationIssue(
                        "public_keys",
                        f"actor {actor!r}: expected Path, str PEM, or Ed25519PublicKey; "
                        f"got {type(value).__name__}",
                    )
                ]
            )
        return resolved
    if isinstance(public_keys, Ed25519PublicKey):
        return {"_leaf": public_keys}
    return {"_leaf": _load_public_key(Path(public_keys))}


def load_sandbox_report(path: str | Path) -> SandboxReport:
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"not found: {resolved}")]
        ) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("document", f"invalid JSON: {exc.msg}")]
        ) from exc
    try:
        return SandboxReport.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def load_release_review(path: str | Path) -> ReleaseReview:
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"not found: {resolved}")]
        ) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("document", f"invalid JSON: {exc.msg}")]
        ) from exc
    try:
        return ReleaseReview.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def create_release_review(
    sandbox_report: SandboxReport,
    *,
    decision: ReviewDecision,
    reviewer: ReviewerIdentity,
    rationale: str,
    reference: str,
    decided_at: str,
    private_key_path: str | Path,
) -> ReleaseReview:
    """Sign an independent human decision without authorizing deployment."""

    issues: list[ValidationIssue] = []
    try:
        decided = parse_timestamp(decided_at)
    except ValueError as exc:
        issues.append(ValidationIssue("decided_at", str(exc)))
        decided = None
    if decided is not None and decided < parse_timestamp(sandbox_report.generated_at):
        issues.append(
            ValidationIssue("decided_at", "cannot precede sandbox report generation")
        )
    if reviewer.actor == sandbox_report.sandbox_approval.actor:
        issues.append(
            ValidationIssue(
                "reviewer.actor",
                "must differ from the sandbox evaluator for separation of duties",
            )
        )
    approving = decision is ReviewDecision.approve_staged_release
    if approving and sandbox_report.decision is not SandboxStatus.passed:
        issues.append(
            ValidationIssue("decision", "cannot approve a sandbox report that did not pass")
        )
    if approving and not sandbox_report.eligible_for_release_review:
        issues.append(
            ValidationIssue("decision", "sandbox report is not eligible for release review")
        )
    if sandbox_report.release_allowed:
        issues.append(
            ValidationIssue("sandbox_report.release_allowed", "must remain false")
        )
    if sandbox_report.automatic_application or sandbox_report.automatic_release:
        issues.append(
            ValidationIssue(
                "sandbox_report",
                "automatic application and release must remain false",
            )
        )
    if issues:
        raise DSLValidationError(issues)

    if not 20 <= len(rationale) <= 4000:
        raise DSLValidationError(
            [ValidationIssue("rationale", "must contain between 20 and 4000 characters")]
        )
    if not 3 <= len(reference) <= 255:
        raise DSLValidationError(
            [ValidationIssue("reference", "must contain between 3 and 255 characters")]
        )

    payload = _unsigned_payload(
        decided_at=decided_at,
        reference=reference,
        reviewer=reviewer,
        decision=decision,
        rationale=rationale,
        sandbox_report=sandbox_report,
        staged_release_authorized=approving,
    )
    encoded = _canonical_bytes(payload)
    payload_digest = hashlib.sha256(encoded).hexdigest()
    private_key = _load_private_key(private_key_path)
    public_key = private_key.public_key()
    signature = base64.b64encode(private_key.sign(encoded)).decode("ascii")
    return ReleaseReview(
        review_id=f"REVIEW-{payload_digest[:24].upper()}",
        decided_at=decided_at,
        reference=reference,
        reviewer=reviewer,
        decision=decision,
        rationale=rationale,
        sandbox_report=sandbox_report,
        staged_release_authorized=approving,
        signature=SignatureBlock(
            key_id=_key_id(public_key),
            signed_payload_sha256=payload_digest,
            value_base64=signature,
        ),
    )


def _verify_with_public_keys(
    review: ReleaseReview,
    public_keys: dict[str, Ed25519PublicKey],
) -> ReviewVerification:
    """Verify a release review against a resolved ``public_keys`` mapping.

    In single-signature mode (no ``delegation_chain``) the mapping MUST
    contain exactly one entry whose key is ``_leaf``; in chain mode the
    mapping MUST contain a key for every chain actor.
    """

    payload = _payload_from_review(review)
    encoded = _canonical_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    expected_review_id = f"REVIEW-{digest[:24].upper()}"

    problems: list[str] = []
    chain_present = review.delegation_chain is not None
    leaf_level = review.delegation_chain.levels[-1] if chain_present else None

    if not chain_present:
        if list(public_keys.keys()) != ["_leaf"]:
            raise DSLValidationError(
                [
                    ValidationIssue(
                        "public_keys",
                        "single-signature mode requires exactly one key under '_leaf'",
                    )
                ]
            )
        public_key = public_keys["_leaf"]
        key_matches = review.signature.key_id == _key_id(public_key)
        digest_matches = review.signature.signed_payload_sha256 == digest
        review_id_matches = review.review_id == expected_review_id
        if not key_matches:
            problems.append("public key id does not match the signing key id")
        if not digest_matches:
            problems.append("signed payload digest does not match the review payload")
        if not review_id_matches:
            problems.append("review id does not match the review payload")
        try:
            signature_bytes = base64.b64decode(review.signature.value_base64, validate=True)
            public_key.verify(signature_bytes, encoded)
            signature_valid = True
        except (ValueError, InvalidSignature):
            signature_valid = False
            problems.append("Ed25519 signature is invalid")

        expected_authorization = (
            review.decision is ReviewDecision.approve_staged_release
            and review.sandbox_report.decision is SandboxStatus.passed
            and review.sandbox_report.eligible_for_release_review
        )
        semantics_valid = (
            review.staged_release_authorized == expected_authorization
            and review.reviewer.actor != review.sandbox_report.sandbox_approval.actor
            and not review.sandbox_report.release_allowed
            and not review.sandbox_report.automatic_application
            and not review.sandbox_report.automatic_release
            and not review.production_deployment_allowed
            and not review.automatic_application
            and not review.automatic_deployment
        )
        if not semantics_valid:
            problems.append("review authorization semantics are inconsistent")
        valid = all(
            [
                key_matches,
                digest_matches,
                review_id_matches,
                signature_valid,
                semantics_valid,
            ]
        )
        return ReviewVerification(
            review_id=review.review_id,
            valid=valid,
            key_id_matches=key_matches,
            payload_digest_matches=digest_matches,
            review_id_matches=review_id_matches,
            signature_valid=signature_valid,
            semantics_valid=semantics_valid,
            staged_release_authorized=review.staged_release_authorized if valid else False,
            problems=problems,
            delegation_chain_present=False,
            chain_actors_match_reviewer=None,
            delegation_chain_valid=None,
            chain_actor_count=None,
            chain_dsl_chain_matches_live_contract=None,
            chain_separation_of_duties_ok=None,
        )

    # Chain mode: walk from leaf to root.
    chain = review.delegation_chain
    missing_actors = [
        level.actor for level in chain.levels if level.actor not in public_keys
    ]
    if missing_actors:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "public_keys",
                    "missing Ed25519 public keys for chain actors: "
                    + ", ".join(missing_actors),
                )
            ]
        )

    chain_actors_match_reviewer = (
        leaf_level is not None and leaf_level.actor == review.reviewer.actor
    )
    if not chain_actors_match_reviewer:
        problems.append(
            "delegation_chain.levels[-1].actor must equal reviewer.actor"
        )

    sandbox_evaluator = review.sandbox_report.sandbox_approval.actor
    colliders = sorted(
        {
            level.actor
            for level in chain.levels
            if level.actor == sandbox_evaluator
        }
    )
    chain_separation_of_duties_ok = not colliders
    if colliders:
        problems.append(
            "separation of duties violated: chain actor is the sandbox "
            "evaluator (" + ", ".join(colliders) + ")"
        )

    # Re-verify the leaf signature using the leaf's public key.
    leaf_public_key = public_keys[leaf_level.actor]
    key_matches = review.signature.key_id == _key_id(leaf_public_key)
    digest_matches = review.signature.signed_payload_sha256 == digest
    review_id_matches = review.review_id == expected_review_id
    if not key_matches:
        problems.append("public key id does not match the leaf signing key id")
    if not digest_matches:
        problems.append("signed payload digest does not match the review payload")
    if not review_id_matches:
        problems.append("review id does not match the review payload")
    try:
        signature_bytes = base64.b64decode(review.signature.value_base64, validate=True)
        leaf_public_key.verify(signature_bytes, encoded)
        leaf_signature_valid = True
    except (ValueError, InvalidSignature):
        leaf_signature_valid = False
        problems.append("Ed25519 leaf signature is invalid")

    # Walk the chain from leaf up to root. The leaf is verified against
    # the unsigned review payload directly; every higher level is
    # verified against a payload that wraps the immediately-lower level's
    # canonical JSON representation plus the contract / clause context.
    chain_signature_problems: list[str] = []
    # Walk from the leaf upward: index = len(levels) - 1, len(levels) - 2, ..., 0.
    for index in range(len(chain.levels) - 1, -1, -1):
        level = chain.levels[index]
        level_pub_key = public_keys[level.actor]
        if level.key_id != _key_id(level_pub_key):
            chain_signature_problems.append(
                f"chain level {level.actor}: key_id does not match public key"
            )
            continue
        if index == len(chain.levels) - 1:
            # Leaf: signs the unsigned review payload.
            payload_to_verify = encoded
            expected_digest = digest
        else:
            # Non-leaf: signs a payload wrapping the lower level's repr.
            payload_to_verify = _level_signing_payload(
                contract_id=chain.contract_id,
                contract_version=chain.contract_version,
                clause_id=chain.clause_id,
                level_actor=level.actor,
                level_role=level.role,
                lower_signature_canonical_repr=(
                    _level_canonical_repr(chain.levels[index + 1]).decode("utf-8")
                ),
            )
            expected_digest = hashlib.sha256(payload_to_verify).hexdigest()
        if level.signed_payload_sha256 != expected_digest:
            chain_signature_problems.append(
                f"chain level {level.actor}: signed payload digest mismatch"
            )
        try:
            sig = base64.b64decode(level.value_base64, validate=True)
            level_pub_key.verify(sig, payload_to_verify)
        except (ValueError, InvalidSignature):
            chain_signature_problems.append(
                f"chain level {level.actor}: Ed25519 signature is invalid"
            )
    problems.extend(chain_signature_problems)
    delegation_chain_valid = not chain_signature_problems

    semantics_valid = (
        review.staged_release_authorized
        == (
            review.decision is ReviewDecision.approve_staged_release
            and review.sandbox_report.decision is SandboxStatus.passed
            and review.sandbox_report.eligible_for_release_review
        )
        and not review.sandbox_report.release_allowed
        and not review.sandbox_report.automatic_application
        and not review.sandbox_report.automatic_release
        and not review.production_deployment_allowed
        and not review.automatic_application
        and not review.automatic_deployment
        and chain_separation_of_duties_ok
    )
    if not semantics_valid:
        problems.append("review authorization semantics are inconsistent")

    valid = all(
        [
            key_matches,
            digest_matches,
            review_id_matches,
            leaf_signature_valid,
            semantics_valid,
            delegation_chain_valid,
            chain_actors_match_reviewer,
        ]
    )
    return ReviewVerification(
        review_id=review.review_id,
        valid=valid,
        key_id_matches=key_matches,
        payload_digest_matches=digest_matches,
        review_id_matches=review_id_matches,
        signature_valid=leaf_signature_valid,
        semantics_valid=semantics_valid,
        staged_release_authorized=review.staged_release_authorized if valid else False,
        problems=problems,
        delegation_chain_present=True,
        chain_actors_match_reviewer=chain_actors_match_reviewer,
        delegation_chain_valid=delegation_chain_valid,
        chain_actor_count=len(chain.levels),
        chain_dsl_chain_matches_live_contract=None,  # requires live contract
        chain_separation_of_duties_ok=chain_separation_of_duties_ok,
    )


def verify_release_review(
    review: ReleaseReview,
    public_keys: Mapping[str, str | Path | Ed25519PublicKey]
    | str
    | Path
    | Ed25519PublicKey,
) -> ReviewVerification:
    """Verify signature, payload identity, and authorization semantics.

    Poly­morphically dispatches on the type of ``public_keys``. In
    single-signature mode ``public_keys`` MUST be a single ``Path``,
    ``str`` PEM or ``Ed25519PublicKey``; in chain mode it MUST be a
    ``Mapping[str, ...]`` with one entry per chain actor.
    """

    resolved = _resolve_public_keys(public_keys)
    return _verify_with_public_keys(review, resolved)


def verify_release_review_pem(
    review: ReleaseReview,
    public_key_pem: str,
) -> ReviewVerification:
    """Verify a single-signature review using an embedded UTF-8 PEM public key.

    Chain signatures cannot be recorded by :func:`record_release_review`
    because the audit ledger stores a single ``public_key_pem``; for a
    chain-signed review use :func:`verify_release_review` with a
    ``Mapping[str, str]`` of per-actor PEMs.
    """

    try:
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise DSLValidationError(
            [ValidationIssue("public_key_pem", "is not a valid PEM public key")]
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise DSLValidationError(
            [ValidationIssue("public_key_pem", "must contain an Ed25519 public key")]
        )
    return _verify_with_public_keys(review, {"_leaf": key})


def _representative_dsl_chain(contract: ContractDocument, clause_id: str) -> list[str]:
    """Return the DSL chain ordering for ``clause_id`` (or ``[]`` if absent)."""

    for clause in getattr(contract, "clauses", []):
        if clause.id == clause_id:
            delegate_to = getattr(clause, "delegate_to", None)
            if delegate_to is None:
                return []
            return list(delegate_to)
    return []


def verify_delegation_chain_match(
    review: ReleaseReview,
    contract: ContractDocument,
) -> list[str]:
    """Auxiliary DSL-chain attestation (chain mode only).

    Compares ``review.delegation_chain.expected_chain_actors`` against the
    live contract's ``clause.delegate_to`` ordering. Returns a list of
    problems; empty list means the live DSL matches the snapshotted chain.
    Raises ``DSLValidationError`` if the review has no ``delegation_chain``
    because the helper is meaningless in single-signature mode.
    """

    chain = review.delegation_chain
    if chain is None:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "review.delegation_chain",
                    "review has no delegation_chain; verify_delegation_chain_match "
                    "is chain-mode only",
                )
            ]
        )
    live = _representative_dsl_chain(contract, chain.clause_id)
    if list(chain.expected_chain_actors) != live:
        return [
            "delegation_chain.expected_chain_actors does not match "
            f"clause.delegate_to (snapshotted={chain.expected_chain_actors!r}, "
            f"live={live!r})"
        ]
    return []


# =========================================================================
# Standing-approval-window envelope enforcement
# =========================================================================
class EnvelopeSummary(BaseModel):
    """Compact, typed snapshot of the envelope a release intent targets."""

    model_config = ConfigDict(extra="forbid")

    risk_budget: Annotated[int, Field(ge=1, le=10_000)]
    scope: list[Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]]
    expiry: str
    current_usage: Annotated[int, Field(ge=0)]
    target_clause_id: Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]


class EnvelopeEnforcementResult(BaseModel):
    """Outcome of :func:`evaluate_standing_approval`.

    ``in_envelope`` reflects whether every declared invariant holds.
    ``recommended_decision`` is the enforced next step: ``"approve"`` when
    the intent fits the envelope, ``"auto_veto"`` when it is rejected by
    one of the in-envelope invariants, and ``"manual_review"`` when no
    envelope is declared (the autonomous auto-sign path is not applicable
    so the human conductor must decide explicitly).
    """

    model_config = ConfigDict(extra="forbid")

    in_envelope: bool
    recommended_decision: Literal["approve", "auto_veto", "manual_review"]
    problems: list[Annotated[str, Field(min_length=1)]]
    envelope_summary: EnvelopeSummary | None = None
    # True iff ``has_verified_chain=True`` AND at least one envelope
    # invariant failed. The chain signature supersedes the envelope
    # ``auto_veto`` pathway so this flag is the canonical telemetry
    # proving that a chain-signed release bypassed envelope-level
    # safety. ``in_envelope`` stays ``False`` for honesty: the envelope
    # math still didn't fit. The flag is set ONLY when the chain was
    # cryptographically verified by ``enforce_envelope_and_audit``;
    # a forged chain therefore cannot flip it.
    chain_override_active: bool = False


def evaluate_standing_approval(
    *,
    contract: ContractDocument,
    target_clause_id: str,
    current_envelope_usage: int,
    now: str,
    has_verified_chain: bool = False,
) -> EnvelopeEnforcementResult:
    """Decide whether a release intent sits inside the contract's envelope.

    An intent is in-envelope iff (a) ``target_clause_id ∈ scope``,
    (b) ``current_envelope_usage + 1 ≤ risk_budget``, and
    (c) ``now ≤ expiry``. ``now`` is the enforcement-evaluation time — NOT
    the review's ``decided_at`` — so spoofable sign-times cannot back-date
    into an already-expired envelope.

    A contract that does not declare a ``standing_approval_window`` is
    considered "no envelope declared" rather than "in envelope". The
    recommended decision in that case is ``manual_review``: the auto-sign
    path is not applicable and the human conductor must explicitly decide.

    When ``has_verified_chain=True`` (i.e. the review carries a
    cryptographically verified B2 hierarchical chain signature) the
    envelope ``auto_veto`` pathway is supersedable. The conductor, having
    gathered three independent human signatures on the change, has already
    satisfied the separation-of-duties principle the envelope is meant to
    protect. The evaluator still records the failed invariants (prefixed
    ``"chain_override: "`` in :attr:`EnvelopeEnforcementResult.problems`)
    so the audit trail explains why no ``release_denied`` event was
    written, and :attr:`EnvelopeEnforcementResult.chain_override_active`
    is set so downstream consumers can distinguish this case from a
    clean-in-envelope approval. The caller is responsible for verifying
    the chain signature BEFORE flipping ``has_verified_chain=True``.
    """

    window = getattr(contract.contract, "standing_approval_window", None)

    if window is None:
        return EnvelopeEnforcementResult(
            in_envelope=False,
            recommended_decision="manual_review",
            problems=[
                "envelope_not_declared: contract.standing_approval_window is None; "
                "the auto-signing path is not applicable; manual review required"
            ],
            envelope_summary=None,
            chain_override_active=False,
        )

    issues: list[ValidationIssue] = []
    try:
        now_dt = parse_timestamp(now)
    except ValueError as exc:
        issues.append(ValidationIssue("now", str(exc)))
        now_dt = None

    try:
        expiry_dt = parse_timestamp(window.expiry)
    except ValueError as exc:
        issues.append(ValidationIssue("window.expiry", str(exc)))
        expiry_dt = None

    summary = EnvelopeSummary(
        risk_budget=window.risk_budget,
        scope=list(window.scope),
        expiry=window.expiry,
        current_usage=current_envelope_usage,
        target_clause_id=target_clause_id,
    )

    if (
        now_dt is not None
        and expiry_dt is not None
        and now_dt > expiry_dt
    ):
        issues.append(
            ValidationIssue(
                "window.expiry",
                f"now ({now}) is after expiry ({window.expiry})",
            )
        )

    if target_clause_id not in window.scope:
        issues.append(
            ValidationIssue(
                "window.scope",
                f"target_clause_id {target_clause_id!r} not in envelope scope",
            )
        )

    projected = current_envelope_usage + 1
    if projected > window.risk_budget:
        issues.append(
            ValidationIssue(
                "window.risk_budget",
                f"projected usage after release ({projected}) exceeds "
                f"risk_budget ({window.risk_budget})",
            )
        )

    if issues:
        violation_problems = [f"{issue.path}: {issue.message}" for issue in issues]
        if has_verified_chain:
            # Chain supersedes envelope auto_veto: the review carries a
            # cryptographically verified B2 hierarchical approval chain,
            # so the conductor has already met the separation-of-duties
            # principle via 3 independent human signatures. We still
            # record the violations (prefixed) so the audit trail
            # explains why ``release_denied`` was NOT written.
            return EnvelopeEnforcementResult(
                in_envelope=False,
                recommended_decision="approve",
                problems=[
                    f"chain_override: {problem}" for problem in violation_problems
                ],
                envelope_summary=summary,
                chain_override_active=True,
            )
        return EnvelopeEnforcementResult(
            in_envelope=False,
            recommended_decision="auto_veto",
            problems=violation_problems,
            envelope_summary=summary,
            chain_override_active=False,
        )

    return EnvelopeEnforcementResult(
        in_envelope=True,
        recommended_decision="approve",
        problems=[],
        envelope_summary=summary,
        chain_override_active=False,
    )


def release_review_target_clause_id(review: ReleaseReview) -> str | None:
    """Extract the clause_id a release review targets, or ``None``.

    In chain mode ``delegation_chain.clause_id`` is the binding; in
    single-signature mode the caller must resolve the target clause from
    the change card (typically ``incident.clause_id``).
    """

    if review.delegation_chain is not None:
        return review.delegation_chain.clause_id
    return None


def enforce_envelope_and_audit(
    ledger: Any,
    *,
    contract: ContractDocument,
    target_clause_id: str,
    current_envelope_usage: int,
    now: str,
    review_id: str,
    reference: str,
    review: ReleaseReview | None = None,
    verifying_public_keys: Mapping[
        str, str | Path | Ed25519PublicKey
    ]
    | None = None,
):
    """Combined enforce + audit-append for auto-veto releases.

    Returns ``(result, audit_append_or_None)``. Audit appends are
    conditionally emitted:
    * ``auto_veto`` → one ``release_denied`` event (subject = review_id,
      actor = ``system_autopilot``).
    * chain-superseded ``approve`` with ``chain_override_active=True`` →
      one ``release_chain_override`` event (subject = review_id, actor =
      ``system_orchestrator``). Carry-over (D) closes the previous
      audit-trail silence: without this event, downstream verifiers
      could not reconstruct WHY an envelope-violating release was approved
      without rerunning :func:`evaluate_standing_approval` offline.
    * in-envelope clean approve, no-envelope/manual-review, AND clean
      forged-chain veto all do NOT write to the ledger.

    B2 carry-over (C) — trust-boundary closure: when ``review`` carries a
    ``delegation_chain``, the helper REQUIRES ``verifying_public_keys``
    and defensively calls :func:`verify_release_review` to derive
    ``has_verified_chain``. A forged chain therefore cannot flip the
    override branch because ``has_verified_chain`` is set only when
    ``verification.valid and verification.delegation_chain_valid``.

    Hard requirement: chain + no ``verifying_public_keys`` → raises
    :class:`DSLValidationError`. The conductor MUST supply the per-actor
    mapping or call :func:`verify_release_review` upstream; this helper
    no longer accepts the unverified ``has_delegation_chain=True``
    syntactic trust.

    Lazy-imports ``autopilot.audit`` so ``release_review.py`` does not
    introduce a circular dependency on the audit module.
    """

    chain_present = review is not None and review.delegation_chain is not None

    if (
        review is not None
        and review.delegation_chain is not None
        and review.delegation_chain.clause_id != target_clause_id
    ):
        raise DSLValidationError(
            [
                ValidationIssue(
                    "target_clause_id",
                    "must equal review.delegation_chain.clause_id when a "
                    "delegation chain is attached to the review",
                )
            ]
        )

    if chain_present and verifying_public_keys is None:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "verifying_public_keys",
                    "is required when review carries a delegation_chain; "
                    "supply the Mapping[actor, public_key] consumed by "
                    "verify_release_review, or call verify_release_review "
                    "upstream and gate on it before invoking this helper",
                )
            ]
        )

    has_verified_chain = False
    if chain_present and verifying_public_keys is not None:
        verification = verify_release_review(review, verifying_public_keys)
        has_verified_chain = bool(
            verification.valid and verification.delegation_chain_valid
        )

    result = evaluate_standing_approval(
        contract=contract,
        target_clause_id=target_clause_id,
        current_envelope_usage=current_envelope_usage,
        now=now,
        has_verified_chain=has_verified_chain,
    )
    # B2 carry-over (D): the clean-in-envelope approve AND the
    # manual_review branches never write to the ledger. Only the veto
    # AND the chain-override branches do, each with their own event
    # type so downstream consumers can reconstitute the bypass trail.
    if result.envelope_summary is None:  # pragma: no cover - defensive
        return result, None

    if result.recommended_decision == "approve" and result.chain_override_active:
        # Chain-superseded approve: idempotent release_chain_override
        # append. Subject_id = review_id so retries return inserted=False.
        from autopilot.audit import (
            ReleaseChainOverride,
            record_release_chain_override,
        )

        summary = result.envelope_summary
        chain_actor_count = (
            len(review.delegation_chain.levels)
            if review is not None and review.delegation_chain is not None
            else None
        )
        override = ReleaseChainOverride(
            review_id=review_id,
            contract_id=contract.contract.id,
            contract_version=contract.contract.version,
            scope_clause_ids=list(summary.scope),
            target_clause_id=target_clause_id,
            current_usage=summary.current_usage,
            risk_budget=summary.risk_budget,
            expiry=summary.expiry,
            override_at=now,
            actor="system_orchestrator",
            override_reason=list(result.problems),
            reference=reference,
            chain_actor_count=chain_actor_count,
        )
        attempted = record_release_chain_override(ledger, override)
        return result, attempted

    if result.recommended_decision != "auto_veto":
        # Clean approve or manual_review — no audit event.
        return result, None

    from autopilot.audit import ReleaseDenial, record_release_denied

    summary = result.envelope_summary
    denial = ReleaseDenial(
        review_id=review_id,
        contract_id=contract.contract.id,
        contract_version=contract.contract.version,
        scope_clause_ids=list(summary.scope),
        target_clause_id=target_clause_id,
        current_usage=summary.current_usage,
        risk_budget=summary.risk_budget,
        expiry=summary.expiry,
        denied_at=now,
        actor="system_autopilot",
        reason=list(result.problems),
        reference=reference,
    )
    attempted = record_release_denied(ledger, denial)
    return result, attempted


def tamper_delegation_level_for_testing(
    level: DelegationLevel,
    *,
    swap_with: tuple | None = None,
) -> DelegationLevel:
    """Test helper: produce a structurally-invalid copy of a level.

    For ``swap_with=None`` flips a single hex digit in ``key_id``; for
    ``swap_with=(actor, role, public_key_pem)`` replaces the level with
    a different actor+key whose signature won't verify under the public
    key. Used by chain tests via the public API only — the caller is
    responsible for keeping the chain self-consistent (i.e. expecting
    ``len(levels) == len(expected_chain_actors)``).
    """

    if swap_with is None:
        bad_id = list(level.key_id)
        bad_id[0] = "f" if bad_id[0] != "f" else "0"
        return level.model_copy(update={"key_id": "".join(bad_id)})
    actor, role, _ = swap_with
    bad_id = list(level.key_id)
    bad_id[0] = "f" if bad_id[0] != "f" else "0"
    return level.model_copy(update={"actor": actor, "role": role, "key_id": "".join(bad_id)})


# =========================================================================
# B2 chain-aware creation
# =========================================================================
def create_delegated_release_review(
    sandbox_report: SandboxReport,
    *,
    decision: ReviewDecision,
    leaf_reviewer: ReviewerIdentity,
    rationale: str,
    reference: str,
    decided_at: str,
    contract_id: str,
    contract_version: str,
    clause_id: str,
    expected_chain_actors: list[str],
    chain_signers: list[
        tuple[str, str, str | Path]
    ],  # [(actor, role, private_key_path), ...] in root → middle → leaf order
) -> ReleaseReview:
    """Sign a release review through a hierarchical approval chain.

    ``chain_signers`` is ordered root → … → leaf; the last entry is the
    delegate who signs the review itself. ``expected_chain_actors`` is the
    snapshotted ``clause.delegate_to`` ordering from the DSL contract.
    """

    issues: list[ValidationIssue] = []
    if len(chain_signers) < 2:
        issues.append(
            ValidationIssue(
                "chain_signers",
                "must contain at least 2 (root + leaf) entries for a chain",
            )
        )
    if len(expected_chain_actors) != len(chain_signers):
        issues.append(
            ValidationIssue(
                "expected_chain_actors",
                f"length ({len(expected_chain_actors)}) must equal "
                f"chain_signers ({len(chain_signers)})",
            )
        )
    if list(expected_chain_actors) != [actor for actor, _role, _key in chain_signers]:
        issues.append(
            ValidationIssue(
                "expected_chain_actors",
                "must list the same actors (in root → leaf order) as chain_signers",
            )
        )

    approving = decision is ReviewDecision.approve_staged_release
    try:
        decided = parse_timestamp(decided_at)
    except ValueError as exc:
        issues.append(ValidationIssue("decided_at", str(exc)))
        decided = None
    if decided is not None and decided < parse_timestamp(sandbox_report.generated_at):
        issues.append(
            ValidationIssue("decided_at", "cannot precede sandbox report generation")
        )
    if issues:
        raise DSLValidationError(issues)

    leaf_actor, leaf_role, _leaf_key_path = chain_signers[-1]
    if leaf_reviewer.actor != leaf_actor:
        issues.append(
            ValidationIssue(
                "leaf_reviewer.actor",
                "must equal the leaf (last) chain_signers actor",
            )
        )
    if leaf_reviewer.role != leaf_role:
        issues.append(
            ValidationIssue(
                "leaf_reviewer.role",
                "must equal the leaf (last) chain_signers role",
            )
        )
    if leaf_reviewer.actor == sandbox_report.sandbox_approval.actor:
        issues.append(
            ValidationIssue(
                "leaf_reviewer.actor",
                "must differ from the sandbox evaluator for separation of duties",
            )
        )
    chain_actor_set = {actor for actor, _role, _key in chain_signers}
    if sandbox_report.sandbox_approval.actor in chain_actor_set:
        issues.append(
            ValidationIssue(
                "chain_signers",
                "no chain actor may equal the sandbox evaluator",
            )
        )
    if approving and sandbox_report.decision is not SandboxStatus.passed:
        issues.append(
            ValidationIssue("decision", "cannot approve a sandbox report that did not pass")
        )
    if approving and not sandbox_report.eligible_for_release_review:
        issues.append(
            ValidationIssue("decision", "sandbox report is not eligible for release review")
        )
    if sandbox_report.release_allowed:
        issues.append(
            ValidationIssue("sandbox_report.release_allowed", "must remain false")
        )
    if sandbox_report.automatic_application or sandbox_report.automatic_release:
        issues.append(
            ValidationIssue(
                "sandbox_report",
                "automatic application and release must remain false",
            )
        )
    if not 20 <= len(rationale) <= 4000:
        issues.append(
            ValidationIssue("rationale", "must contain between 20 and 4000 characters")
        )
    if not 3 <= len(reference) <= 255:
        issues.append(
            ValidationIssue("reference", "must contain between 3 and 255 characters")
        )
    if issues:
        raise DSLValidationError(issues)

    # Step 1: leaf signs the unsigned review payload (same canonical bytes
    # as the single-signature path).
    leaf_actor, leaf_role, leaf_key_path = chain_signers[-1]
    leaf_private_key = _load_private_key(leaf_key_path)
    unsigned_payload = _unsigned_payload(
        decided_at=decided_at,
        reference=reference,
        reviewer=leaf_reviewer,
        decision=decision,
        rationale=rationale,
        sandbox_report=sandbox_report,
        staged_release_authorized=approving,
    )
    encoded = _canonical_bytes(unsigned_payload)
    leaf_level = _sign_leaf_level(
        unsigned_payload_bytes=encoded,
        private_key=leaf_private_key,
        actor=leaf_actor,
        role=leaf_role,
    )

    # Step 2: sign the remaining levels from leaf+1 up to root.
    levels: list[DelegationLevel] = [leaf_level]
    lower_repr: bytes = _level_canonical_repr(leaf_level)
    for actor, role, key_path in reversed(chain_signers[:-1]):
        level = _sign_chain_level(
            contract_id=contract_id,
            contract_version=contract_version,
            clause_id=clause_id,
            level_actor=actor,
            level_role=role,
            lower_signature_canonical_repr=lower_repr,
            private_key=_load_private_key(key_path),
        )
        levels.append(level)
        lower_repr = _level_canonical_repr(level)
    # ``levels`` is now ordered leaf → middle → root. Reverse to root → middle → leaf.
    levels.reverse()
    chain = DelegationChain(
        contract_id=contract_id,
        contract_version=contract_version,
        clause_id=clause_id,
        expected_chain_actors=list(expected_chain_actors),
        levels=levels,
    )
    payload_digest = hashlib.sha256(encoded).hexdigest()
    return ReleaseReview(
        review_id=f"REVIEW-{payload_digest[:24].upper()}",
        decided_at=decided_at,
        reference=reference,
        reviewer=leaf_reviewer,
        decision=decision,
        rationale=rationale,
        sandbox_report=sandbox_report,
        staged_release_authorized=approving,
        signature=SignatureBlock(
            key_id=leaf_level.key_id,
            signed_payload_sha256=payload_digest,
            value_base64=leaf_level.value_base64,
        ),
        delegation_chain=chain,
    )
