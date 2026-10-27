"""Portable evidence signing for Web Lab runs (Ed25519 over a canonical digest).

The lab executes runs and displays them; this module seals one run as an
*artifact*: the payload is canonicalised (dict keys sorted, volatile timing
fields and any previous seal dropped), hashed with SHA-256, and that digest is
signed by the lab's Ed25519 key. The signature block travels inside the
exported JSON together with the raw public key, so the artifact can be checked
without the lab: ``verify_lab_artifact`` needs only the artifact and its
signature.

Declared limits (see ``docs/product/laboratorio_firma_evidencia_v0.1.md``):

- The key is a *laboratory identity* stored under ``data/lab_signing/`` (ignored
  by git). It is not an operator credential and confers no authority.
- The signature attests that this artifact, canonicalised by this tool, was
  sealed at a moment by that key. It does not attest that the simulation is
  truthful, that a person approved anything, or that the run matches a live
  system.
- ``signed_at`` is the signer's clock; the module records it, it does not
  timestamp-authority it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

LAB_EVIDENCE_SCHEMA = "0.1"
LAB_KEY_DIRNAME = "lab_signing"
LAB_PRIVATE_KEY_NAME = "lab_private_key.pem"
LAB_PUBLIC_KEY_NAME = "lab_public_key.pem"

#: Fields excluded from the digest so the same run hashes identically whether it
#: is signed live, exported by the static bundle builder, or re-verified later.
VOLATILE_ARTIFACT_KEYS = frozenset({"duration_ms", "artifact_sha256", "signature"})


class LabSignature(BaseModel):
    """Self-contained seal: what was signed, by whom, when, and the key to check it."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["Ed25519"] = "Ed25519"
    experiment: str = Field(min_length=1, max_length=80)
    key_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signed_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    value_base64: str = Field(min_length=1)
    public_key_base64: str = Field(min_length=1)
    signed_at: str = Field(min_length=1)


class LabVerification(BaseModel):
    """Result of checking one artifact against one signature."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    experiment: str
    artifact_sha256: str
    digest_matches: bool
    signature_valid: bool
    key_id_valid: bool
    problems: list[str] = Field(default_factory=list)


def _canonical_number(value: int | float) -> str:
    """Language-independent decimal form for a JSON number.

    The artifact crosses the browser between runs, and JavaScript re-serialises
    ``1.0`` as ``1``. Formatting every number with 17 significant digits (the
    exact round-trip precision of an IEEE-754 double) removes that difference
    and the Python-vs-JavaScript exponent ambiguity in one move. Integers and
    integral floats share a form; booleans are handled before this function.
    """

    if isinstance(value, float) and value != value:  # NaN never appears in JSON
        return "nan"
    return format(value, ".17g")


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in VOLATILE_ARTIFACT_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _canonical_number(value)
    return value


def canonical_artifact_bytes(artifact: Any) -> bytes:
    """Canonical UTF-8 JSON for an artifact: sorted keys, no whitespace, ASCII."""

    return json.dumps(
        _strip_volatile(artifact),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def artifact_digest(artifact: Any) -> str:
    """SHA-256 of the canonical artifact, ignoring volatile and seal fields."""

    return hashlib.sha256(canonical_artifact_bytes(artifact)).hexdigest()


def attach_artifact_digest(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the lab payload with its ``artifact_sha256`` stamped."""

    return {**artifact, "artifact_sha256": artifact_digest(artifact)}


def default_key_dir(root: Path) -> Path:
    return root / "data" / LAB_KEY_DIRNAME


def _key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"could not load lab private key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("lab signing key must be Ed25519")
    return key


def _load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"could not load lab public key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("lab verification key must be Ed25519")
    return key


def _write_keypair(private_path: Path, public_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_created = False
    public_created = False
    try:
        with private_path.open("xb") as stream:
            private_created = True
            stream.write(private_bytes)
        with public_path.open("xb") as stream:
            public_created = True
            stream.write(public_bytes)
    except OSError:
        if private_created and private_path.exists():
            private_path.unlink()
        if public_created and public_path.exists():
            public_path.unlink()
        raise


def ensure_lab_keypair(key_dir: Path) -> tuple[Path, Path]:
    """Create the lab key pair on first use; never replace existing material.

    A half-present pair (only one file) raises instead of regenerating, so a
    deleted public key cannot silently invalidate every seal already issued.
    """

    private_path = key_dir / LAB_PRIVATE_KEY_NAME
    public_path = key_dir / LAB_PUBLIC_KEY_NAME
    if private_path.exists() and public_path.exists():
        return private_path, public_path
    if private_path.exists() or public_path.exists():
        raise ValueError(
            "incomplete lab signing key pair: move the remaining file aside to regenerate"
        )
    key_dir.mkdir(parents=True, exist_ok=True)
    try:
        _write_keypair(private_path, public_path)
    except FileExistsError:
        # Another request created the pair between the checks above.
        pass
    return private_path, public_path


def _signing_payload(
    *,
    experiment: str,
    artifact_sha256: str,
    key_id: str,
    signed_at: str,
) -> bytes:
    return json.dumps(
        {
            "schema_version": LAB_EVIDENCE_SCHEMA,
            "algorithm": "Ed25519",
            "experiment": experiment,
            "artifact_sha256": artifact_sha256,
            "key_id": key_id,
            "signed_at": signed_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _normalise_signed_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("signed_at must be an RFC 3339 timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError("signed_at must include a timezone")
    return moment.isoformat()


def sign_lab_artifact(
    artifact: Any,
    *,
    experiment: str,
    key_dir: Path,
    signed_at: str | None = None,
) -> LabSignature:
    """Seal one artifact. Refuses an artifact whose own digest already disagrees."""

    digest = artifact_digest(artifact)
    if isinstance(artifact, dict):
        stamped = artifact.get("artifact_sha256")
        if stamped is not None and stamped != digest:
            raise ValueError(
                "artifact_sha256 does not match the artifact content (mutated after stamping)"
            )
    private_path, public_path = ensure_lab_keypair(key_dir)
    private_key = _load_private_key(private_path)
    public_key = _load_public_key(public_path)
    private_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if private_raw != public_raw:
        raise ValueError("lab private and public keys do not match")
    key_id = _key_id(public_key)
    signed_at_value = _normalise_signed_at(signed_at)
    payload = _signing_payload(
        experiment=experiment,
        artifact_sha256=digest,
        key_id=key_id,
        signed_at=signed_at_value,
    )
    return LabSignature(
        experiment=experiment,
        key_id=key_id,
        artifact_sha256=digest,
        signed_payload_sha256=hashlib.sha256(payload).hexdigest(),
        value_base64=base64.b64encode(private_key.sign(payload)).decode("ascii"),
        public_key_base64=base64.b64encode(public_raw).decode("ascii"),
        signed_at=signed_at_value,
    )


def signing_key_info(key_dir: Path) -> dict[str, str]:
    """Public description of the lab signing identity (never the private key)."""

    _, public_path = ensure_lab_keypair(key_dir)
    public_key = _load_public_key(public_path)
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "algorithm": "Ed25519",
        "key_id": _key_id(public_key),
        "public_key_base64": base64.b64encode(raw).decode("ascii"),
        "schema_version": LAB_EVIDENCE_SCHEMA,
    }


def verify_lab_artifact(artifact: Any, signature: LabSignature) -> LabVerification:
    """Recompute the digest and check the seal against its embedded public key."""

    problems: list[str] = []
    try:
        computed = artifact_digest(artifact)
    except (TypeError, ValueError) as exc:  # non-serialisable payload
        return LabVerification(
            valid=False,
            experiment=signature.experiment,
            artifact_sha256=signature.artifact_sha256,
            digest_matches=False,
            signature_valid=False,
            key_id_valid=False,
            problems=[f"artifact is not canonicalisable JSON: {exc}"],
        )

    digest_matches = computed == signature.artifact_sha256
    if not digest_matches:
        problems.append(
            "artifact digest does not match the sealed digest (the artifact changed)"
        )

    try:
        public_raw = base64.b64decode(signature.public_key_base64, validate=True)
        if len(public_raw) != 32:
            raise ValueError("Ed25519 public keys are 32 bytes")
        public_key = Ed25519PublicKey.from_public_bytes(public_raw)
    except (ValueError, TypeError) as exc:
        return LabVerification(
            valid=False,
            experiment=signature.experiment,
            artifact_sha256=signature.artifact_sha256,
            digest_matches=digest_matches,
            signature_valid=False,
            key_id_valid=False,
            problems=problems + [f"embedded public key is invalid: {exc}"],
        )

    key_id_valid = _key_id(public_key) == signature.key_id
    if not key_id_valid:
        problems.append("key_id does not match the embedded public key")

    payload = _signing_payload(
        experiment=signature.experiment,
        artifact_sha256=signature.artifact_sha256,
        key_id=signature.key_id,
        signed_at=signature.signed_at,
    )
    payload_digest_valid = (
        hashlib.sha256(payload).hexdigest() == signature.signed_payload_sha256
    )
    if not payload_digest_valid:
        problems.append("signed payload digest does not match the signature fields")

    signature_valid = False
    try:
        public_key.verify(
            base64.b64decode(signature.value_base64, validate=True), payload
        )
        signature_valid = True
    except (InvalidSignature, ValueError, TypeError) as exc:
        problems.append(f"signature does not verify: {exc}")

    try:
        _normalise_signed_at(signature.signed_at)
    except ValueError as exc:
        problems.append(str(exc))

    return LabVerification(
        valid=digest_matches
        and signature_valid
        and key_id_valid
        and payload_digest_valid
        and not problems,
        experiment=signature.experiment,
        artifact_sha256=computed,
        digest_matches=digest_matches,
        signature_valid=signature_valid,
        key_id_valid=key_id_valid,
        problems=problems,
    )


__all__ = [
    "LAB_EVIDENCE_SCHEMA",
    "LAB_KEY_DIRNAME",
    "LabSignature",
    "LabVerification",
    "attach_artifact_digest",
    "artifact_digest",
    "canonical_artifact_bytes",
    "default_key_dir",
    "ensure_lab_keypair",
    "sign_lab_artifact",
    "signing_key_info",
    "verify_lab_artifact",
]
