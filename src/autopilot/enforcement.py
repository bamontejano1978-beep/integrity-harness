"""Runtime enforcement adapter: map recommended autonomy to tool postures.

Integrity's evaluator and monitor **recommend** autonomy levels; they never
mutate a production system (see ``CONTRACTS_DSL_SPEC_v0.1.md`` §6.2 and §7.5).
This module is the thin bridge on the other side of that boundary: it runs
**in the client's runtime** and enforces the client's own declared tool
policy at the posture derived from Integrity's latest evaluation.

Posture mapping (``COACH < SUPERVISE < RESTRICT < QUARANTINE``):

- ``COACH``      → ``observe``           — allow every tool call, log everything.
- ``SUPERVISE``  → ``require_approval``  — sensitive tools need human approval.
- ``RESTRICT``   → ``allowlist_only``    — only declared allowlist tools respond.
- ``QUARANTINE`` → ``deny_all``          — no tool execution at all.

Design rules:

- **Fail closed.** An unrecognized autonomy level maps to ``deny_all``, and a
  recommendation carrying ``block_release=True`` forces ``deny_all`` regardless
  of the autonomy level.
- **No silent autonomy increases.** Mirroring ``ai_act_monitor.py``, an
  autonomy *increase* is only applied when the caller declares sandbox
  recovery evidence; otherwise the previous (more restrictive) autonomy is
  kept and the transition is recorded with a ``recovery_without_sandbox``
  guardrail.
- **Deterministic and side-effect free.** Decisions are pure functions of
  (autonomy, posture policy, tool name). Logging, human approval capture, and
  actual tool interception remain the client runtime's responsibility.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autopilot.contracts.models import AutonomyLevel
from autopilot.evaluator import ContractEvaluation

ADAPTER_NAME = "enforcement_adapter"
ADAPTER_VERSION = "0.1.0"


class _GuardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuardDecisionPayload(_GuardModel):
    """Immutable record of one guard screening, signable and auditable."""

    schema_version: Literal["0.1"] = "0.1"
    decision_id: str
    recorded_at: str
    evaluation_source: str
    recommended_autonomy: AutonomyLevel
    effective_autonomy: AutonomyLevel
    posture: EnforcementPosture
    block_release: bool
    sandbox_evidence_declared: bool = False
    policy_source: str
    decisions: list[GuardToolDecision]
    denied_count: Annotated[int, Field(ge=0)]


class GuardToolDecision(_GuardModel):
    tool: str = Field(min_length=1, max_length=255)
    allowed: bool
    requires_approval: bool
    reason: str = Field(min_length=1, max_length=1000)


class GuardSignature(_GuardModel):
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    signed_payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    value_base64: str
    public_key_pem: str


class SignedGuardDecisionReceipt(_GuardModel):
    payload: GuardDecisionPayload
    signature: GuardSignature


class GuardDecisionVerification(_GuardModel):
    valid: bool
    key_matches: bool
    digest_matches: bool
    signature_valid: bool
    problems: list[str]


class EnforcementPosture(str, Enum):
    """Runtime posture derived from the recommended autonomy level."""

    observe = "observe"
    require_approval = "require_approval"
    allowlist_only = "allowlist_only"
    deny_all = "deny_all"


POSTURE_FOR_AUTONOMY: dict[AutonomyLevel, EnforcementPosture] = {
    AutonomyLevel.COACH: EnforcementPosture.observe,
    AutonomyLevel.SUPERVISE: EnforcementPosture.require_approval,
    AutonomyLevel.RESTRICT: EnforcementPosture.allowlist_only,
    AutonomyLevel.QUARANTINE: EnforcementPosture.deny_all,
}


def posture_for_autonomy(level: AutonomyLevel | str) -> EnforcementPosture:
    """Return the posture for ``level``, failing closed on unknown values."""

    if isinstance(level, AutonomyLevel):
        return POSTURE_FOR_AUTONOMY[level]
    try:
        return POSTURE_FOR_AUTONOMY[AutonomyLevel(level)]
    except ValueError:
        return EnforcementPosture.deny_all


@dataclass(frozen=True)
class ToolPolicy:
    """Client-declared tool policy consumed by the adapter.

    ``sensitive_tools`` gate at ``SUPERVISE``; ``allowlisted_tools`` gate at
    ``RESTRICT``. Declaring a sensitive tool outside the allowlist is not an
    error: at ``RESTRICT`` the allowlist is the operative gate.
    """

    sensitive_tools: frozenset[str]
    allowlisted_tools: frozenset[str]


@dataclass(frozen=True)
class ToolDecision:
    """Outcome of screening one tool call against the current posture.

    ``allowed`` means executable immediately. ``requires_approval`` means the
    client runtime must capture attributable human approval before executing.
    """

    tool: str
    autonomy: AutonomyLevel
    posture: EnforcementPosture
    allowed: bool
    requires_approval: bool
    reason: str


class ToolExecutionDenied(RuntimeError):
    """Raised by :meth:`EnforcementAdapter.require_allowed` on a denied call."""

    def __init__(self, decision: ToolDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


class ToolPolicyFormatError(ValueError):
    """Raised when a client tool-policy document is structurally invalid."""


def load_tool_policy(path: str | Path) -> ToolPolicy:
    """Load a client tool policy from a YAML (or JSON) mapping.

    Recognized fields:

    - ``sensitive_tools``: tools requiring human approval at ``SUPERVISE``.
    - ``allowlisted_tools``: tools permitted at ``RESTRICT``.

    Unknown fields are rejected. Entries must be non-empty strings, and a
    tool may not appear in both sections (ambiguous at ``RESTRICT``).
    """

    resolved = Path(path)
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except OSError as exc:
        raise ToolPolicyFormatError(f"cannot read tool policy {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolPolicyFormatError(f"{resolved} must contain a mapping")

    allowed_fields = {"sensitive_tools", "allowlisted_tools"}
    unknown = sorted(str(key) for key in payload if key not in allowed_fields)
    if unknown:
        raise ToolPolicyFormatError(f"unknown field(s): {', '.join(unknown)}")

    def _entries(field: str) -> list[str]:
        value = payload.get(field, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ToolPolicyFormatError(f"{field} must be a list of non-empty strings")
        return value

    sensitive = _entries("sensitive_tools")
    allowlisted = _entries("allowlisted_tools")
    overlap = sorted(set(sensitive) & set(allowlisted))
    if overlap:
        raise ToolPolicyFormatError(
            f"tools cannot be both sensitive and allowlisted: {', '.join(overlap)}"
        )
    return ToolPolicy(
        sensitive_tools=frozenset(sensitive),
        allowlisted_tools=frozenset(allowlisted),
    )


@dataclass(frozen=True)
class AutonomyTransition:
    """Recorded outcome of an autonomy update applied to the adapter."""

    previous: AutonomyLevel
    proposed: AutonomyLevel
    applied: bool
    guardrail: str | None


_ORDER = {
    AutonomyLevel.COACH: 0,
    AutonomyLevel.SUPERVISE: 1,
    AutonomyLevel.RESTRICT: 2,
    AutonomyLevel.QUARANTINE: 3,
}


def _coerce_autonomy(level: AutonomyLevel | str) -> AutonomyLevel:
    """Coerce ``level`` to :class:`AutonomyLevel`, failing closed on unknowns."""

    if isinstance(level, AutonomyLevel):
        return level
    try:
        return AutonomyLevel(level)
    except ValueError:
        return AutonomyLevel.QUARANTINE


def _parse_aware_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, rejecting naive (timezone-less) values."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


# Fraction of a grant's total lifetime below which an expiry warning fires
# (10% by default; clamped to a 60 s floor for very short grants).
APPROVAL_EXPIRY_WARNING_FRACTION = 0.1


class EnforcementAdapter:
    """Holds the current autonomy and screens tool calls for the client runtime.

    String autonomy values are accepted and coerced; unknown values fail
    closed to ``QUARANTINE``. Policies load from client YAML via
    :func:`load_tool_policy`; evaluations may come from the evaluator or from
    an evaluation JSON (duck-typed: ``recommended_autonomy`` +
    ``block_release``).
    """

    def __init__(self, policy: ToolPolicy, autonomy: AutonomyLevel | str = AutonomyLevel.COACH) -> None:
        self._policy = policy
        self._autonomy = _coerce_autonomy(autonomy)
        self._approvals: dict[str, SignedApprovalReceipt] = {}
        self._revoked_approvals: set[str] = set()

    @classmethod
    def from_evaluation(
        cls,
        evaluation: ContractEvaluation,
        policy: ToolPolicy,
    ) -> EnforcementAdapter:
        """Build an adapter from an evaluator recommendation.

        ``block_release=True`` forces ``QUARANTINE`` regardless of the
        recommended level: a release-blocking evaluation must not leave the
        runtime at a permissive posture.
        """

        autonomy = evaluation.recommended_autonomy
        if evaluation.block_release:
            autonomy = AutonomyLevel.QUARANTINE
        return cls(policy, autonomy)

    @property
    def autonomy(self) -> AutonomyLevel:
        return self._autonomy

    @property
    def posture(self) -> EnforcementPosture:
        return posture_for_autonomy(self._autonomy)

    def decide(self, tool: str) -> ToolDecision:
        """Screen one tool call against the current posture."""

        posture = self.posture
        if posture is EnforcementPosture.deny_all:
            return ToolDecision(
                tool=tool,
                autonomy=self._autonomy,
                posture=posture,
                allowed=False,
                requires_approval=False,
                reason=(
                    f"{self._autonomy.value} denies all tool execution "
                    f"({ADAPTER_NAME} {ADAPTER_VERSION})"
                ),
            )
        if posture is EnforcementPosture.allowlist_only:
            allowed = tool in self._policy.allowlisted_tools
            return ToolDecision(
                tool=tool,
                autonomy=self._autonomy,
                posture=posture,
                allowed=allowed,
                requires_approval=False,
                reason=(
                    f"{self._autonomy.value} permits only allowlisted tools; "
                    f"'{tool}' is {'allowlisted' if allowed else 'not allowlisted'}"
                ),
            )
        if posture is EnforcementPosture.require_approval:
            if tool in self._policy.sensitive_tools:
                grant = self._effective_approval(tool)
                if grant is not None:
                    payload = grant.payload
                    return ToolDecision(
                        tool=tool,
                        autonomy=self._autonomy,
                        posture=posture,
                        allowed=True,
                        requires_approval=False,
                        reason=(
                            f"{self._autonomy.value} executes '{tool}' under "
                            f"approved human grant {payload.approval_id} "
                            f"granted by {payload.granted_by}"
                        ),
                    )
                return ToolDecision(
                    tool=tool,
                    autonomy=self._autonomy,
                    posture=posture,
                    allowed=False,
                    requires_approval=True,
                    reason=(
                        f"{self._autonomy.value} requires human approval for "
                        f"sensitive tool '{tool}'"
                    ),
                )
            return ToolDecision(
                tool=tool,
                autonomy=self._autonomy,
                posture=posture,
                allowed=True,
                requires_approval=False,
                reason=(
                    f"{self._autonomy.value} observes non-sensitive tool '{tool}'"
                ),
            )
        return ToolDecision(
            tool=tool,
            autonomy=self._autonomy,
            posture=posture,
            allowed=True,
            requires_approval=False,
            reason=f"{self._autonomy.value} observes and logs tool '{tool}'",
        )

    def require_allowed(self, tool: str) -> ToolDecision:
        """Return the decision or raise :class:`ToolExecutionDenied`."""

        decision = self.decide(tool)
        if not decision.allowed:
            raise ToolExecutionDenied(decision)
        return decision

    def register_approval(self, receipt: SignedApprovalReceipt) -> str:
        """Register a signed, attributable human approval for one tool.

        The receipt signature is verified **before** registration; an invalid
        receipt is rejected. Registration never changes the posture and never
        bypasses ``deny_all``: grants only take effect while the runtime is at
        ``SUPERVISE`` (``require_approval``).
        """

        verification = verify_tool_approval(receipt)
        if not verification.valid:
            raise ValueError(
                "approval receipt signature is invalid: " + "; ".join(verification.problems)
            )
        self._approvals[receipt.payload.approval_id] = receipt
        return receipt.payload.approval_id

    def revoke_approval(self, receipt: SignedApprovalRevocationReceipt) -> str:
        """Revoke a previously registered approval; the signature is verified first."""

        verification = verify_tool_approval_revocation(receipt)
        if not verification.valid:
            raise ValueError(
                "approval revocation signature is invalid: "
                + "; ".join(verification.problems)
            )
        if receipt.payload.approval_id not in self._approvals:
            raise ValueError(
                f"unknown approval_id: {receipt.payload.approval_id}"
            )
        self._revoked_approvals.add(receipt.payload.approval_id)
        return receipt.payload.approval_id

    def registered_approvals(self) -> list[str]:
        """Return the registered approval ids (revoked ones included)."""

        return sorted(self._approvals)

    def approval_expiry_warnings(
        self,
        *,
        now: datetime | None = None,
        warning_fraction: float = APPROVAL_EXPIRY_WARNING_FRACTION,
    ) -> list[ApprovalExpiryWarning]:
        """Report registered grants nearing (or past) their expiry deadline.

        Purely advisory: the client runtime may surface these to operators so
        approvals can be renewed before they lapse mid-operation. A lapsed
        grant already stops authorizing on its own (see
        ``_effective_approval``); this clock exists so the runtime does not
        discover the lapse from a denied tool call. Exempted from warnings:
        revoked grants, grants without ``expires_at``, and grants outside the
        warning window (more than ``warning_fraction`` of their lifetime
        remaining, with a 60 s floor for very short grants).
        """

        moment = now if now is not None else datetime.now(timezone.utc)
        warnings: list[ApprovalExpiryWarning] = []
        for receipt in self._approvals.values():
            payload = receipt.payload
            if payload.approval_id in self._revoked_approvals:
                continue
            if payload.expires_at is None:
                continue
            try:
                deadline = _parse_aware_timestamp(payload.expires_at)
                granted = _parse_aware_timestamp(payload.granted_at)
            except ValueError:
                continue
            remaining = (deadline - moment).total_seconds()
            threshold = max(60.0, (deadline - granted).total_seconds() * warning_fraction)
            if remaining > threshold:
                continue
            warnings.append(
                ApprovalExpiryWarning(
                    approval_id=payload.approval_id,
                    tool=payload.tool,
                    expires_at=payload.expires_at,
                    expires_in_seconds=remaining,
                )
            )
        return sorted(warnings, key=lambda warning: warning.expires_in_seconds)

    def _effective_approval(self, tool: str) -> SignedApprovalReceipt | None:
        """Return the latest effective grant for ``tool``, or ``None``."""

        candidates = [
            receipt
            for receipt in self._approvals.values()
            if receipt.payload.tool == tool
            and receipt.payload.approval_id not in self._revoked_approvals
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda receipt: receipt.payload.granted_at)
        if latest.payload.expires_at is not None:
            try:
                deadline = _parse_aware_timestamp(latest.payload.expires_at)
            except ValueError:
                return None
            if datetime.now(timezone.utc) >= deadline:
                return None
        return latest

    def update(
        self,
        proposed: AutonomyLevel | str,
        *,
        sandbox_evidence: bool = False,
    ) -> AutonomyTransition | None:
        """Apply an autonomy recommendation, gating increases on sandbox evidence.

        Returns ``None`` when the proposed level equals the current one.
        An increase without declared sandbox recovery evidence is **not**
        applied (fail-closed, mirroring ``ai_act_monitor.py``); a decrease is
        always applied immediately.
        """

        proposed_level = _coerce_autonomy(proposed)
        if proposed_level is self._autonomy:
            return None
        increase = _ORDER[proposed_level] < _ORDER[self._autonomy]
        if increase and not sandbox_evidence:
            return AutonomyTransition(
                previous=self._autonomy,
                proposed=proposed_level,
                applied=False,
                guardrail="recovery_without_sandbox",
            )
        previous = self._autonomy
        self._autonomy = proposed_level
        return AutonomyTransition(
            previous=previous,
            proposed=proposed_level,
            applied=True,
            guardrail=None,
        )


def _guard_canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _guard_public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_guard_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("guard private key must be Ed25519")
    return key


def _load_guard_public_key(path: str | Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("guard public key must be Ed25519")
    return key


def build_guard_decision_payload(
    *,
    adapter: EnforcementAdapter,
    evaluation_source: str,
    policy_source: str,
    decisions: list[ToolDecision],
    block_release: bool,
    sandbox_evidence_declared: bool = False,
    recorded_at: str | None = None,
    recommended_autonomy: AutonomyLevel | None = None,
) -> GuardDecisionPayload:
    """Assemble an immutable payload from one guard screening."""

    timestamp = recorded_at or datetime.now(timezone.utc).isoformat()
    decisions_payload = [
        GuardToolDecision(
            tool=decision.tool,
            allowed=decision.allowed,
            requires_approval=decision.requires_approval,
            reason=decision.reason,
        )
        for decision in decisions
    ]
    seed = _guard_canonical_bytes(
        {
            "evaluation_source": evaluation_source,
            "policy_source": policy_source,
            "recorded_at": timestamp,
            "decisions": [item.model_dump(mode="json") for item in decisions_payload],
            "recommended_autonomy": (recommended_autonomy or adapter.autonomy).value,
        }
    )
    return GuardDecisionPayload(
        decision_id=f"GUARD-{hashlib.sha256(seed).hexdigest()[:24].upper()}",
        recorded_at=timestamp,
        evaluation_source=evaluation_source,
        recommended_autonomy=recommended_autonomy or adapter.autonomy,
        effective_autonomy=adapter.autonomy,
        posture=adapter.posture,
        block_release=block_release,
        sandbox_evidence_declared=sandbox_evidence_declared,
        policy_source=policy_source,
        decisions=decisions_payload,
        denied_count=sum(1 for item in decisions_payload if not item.allowed),
    )


def sign_guard_decision(
    payload: GuardDecisionPayload,
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> SignedGuardDecisionReceipt:
    """Sign a guard decision payload with Ed25519 (same scheme as monitor cycles)."""

    private_key = _load_guard_private_key(private_key_path)
    public_key = _load_guard_public_key(public_key_path)
    if private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) != public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ):
        raise ValueError("guard private and public keys do not match")
    encoded = _guard_canonical_bytes(payload.model_dump(mode="json"))
    signature = private_key.sign(encoded)
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return SignedGuardDecisionReceipt(
        payload=payload,
        signature=GuardSignature(
            key_id=_guard_public_key_id(public_key),
            signed_payload_sha256=hashlib.sha256(encoded).hexdigest(),
            value_base64=base64.b64encode(signature).decode("ascii"),
            public_key_pem=public_pem,
        ),
    )


def verify_guard_decision(
    receipt: SignedGuardDecisionReceipt,
    trusted_public_key_path: str | Path | None = None,
) -> GuardDecisionVerification:
    """Verify a guard receipt, optionally requiring a configured signing key.

    Without ``trusted_public_key_path`` this verifies self-consistency only;
    the embedded public key is not an identity trust root.
    """

    verification = _verify_signed_receipt(
        receipt.payload.model_dump(mode="json"),
        receipt.signature,
        "guard",
    )
    if trusted_public_key_path is None:
        return verification
    problems = list(verification.problems)
    trusted = _embedded_key_matches_trust_key(
        receipt.signature.public_key_pem, trusted_public_key_path
    )
    if not trusted:
        problems.append("guard signing key does not match configured trust key")
    return GuardDecisionVerification(
        valid=verification.valid and trusted,
        key_matches=verification.key_matches and trusted,
        digest_matches=verification.digest_matches,
        signature_valid=verification.signature_valid,
        problems=problems,
    )


# ---------------------------------------------------------------------------
# Human approval capture (APPROVAL REQUIRED verdicts, granted and attributed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalExpiryWarning:
    """Advisory notice that a registered approval grant is nearing expiry.

    Warnings never change any verdict: the affected grant keeps authorizing
    until its deadline, and a lapsed grant stops authorizing on its own.
    """

    approval_id: str
    tool: str
    expires_at: str
    expires_in_seconds: float


class ToolApprovalGrant(_GuardModel):
    """Immutable, attributable record of one human approval decision.

    A grant authorizes **one tool name** while the runtime posture is
    ``require_approval`` (SUPERVISE). It never bypasses ``deny_all``:
    QUARANTINE decisions stay denied regardless of any grant, and an expired
    or revoked grant stops authorizing immediately.
    """

    schema_version: Literal["0.1"] = "0.1"
    approval_id: str
    granted_at: str
    tool: str = Field(min_length=1, max_length=255)
    requested_by: str = Field(min_length=1, max_length=255)
    granted_by: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    justification: str = Field(min_length=20, max_length=2000)
    expires_at: str | None = None
    evaluation_source: str

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            _parse_aware_timestamp(value)
        except ValueError as exc:
            raise ValueError(f"expires_at must be a timezone-aware ISO-8601 timestamp: {exc}") from exc
        return value


class ToolApprovalRevocation(_GuardModel):
    """Attributable withdrawal of a previously granted tool approval."""

    schema_version: Literal["0.1"] = "0.1"
    revocation_id: str
    approval_id: str
    revoked_at: str
    revoked_by: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    reason: str = Field(min_length=20, max_length=2000)


class SignedApprovalReceipt(_GuardModel):
    payload: ToolApprovalGrant
    signature: GuardSignature


class SignedApprovalRevocationReceipt(_GuardModel):
    payload: ToolApprovalRevocation
    signature: GuardSignature


def _embedded_key_matches_trust_key(
    public_key_pem: str,
    trusted_public_key_path: str | Path,
) -> bool:
    """Whether an embedded PEM public key is byte-identical to the trusted one.

    This only binds the signature to a *configured* key: it does not
    establish that the key belongs to the evaluator, which remains an
    unresolved trust root.
    """

    try:
        expected_key = _load_guard_public_key(trusted_public_key_path)
        embedded_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(embedded_key, Ed25519PublicKey):
        return False
    return embedded_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == expected_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _verify_signed_receipt(payload: dict, signature: GuardSignature, label: str):
    """Shared verification core: digest, key binding, and Ed25519 signature."""

    problems: list[str] = []
    encoded = _guard_canonical_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    digest_matches = digest == signature.signed_payload_sha256
    if not digest_matches:
        problems.append("signed payload digest does not match")
    try:
        key = serialization.load_pem_public_key(signature.public_key_pem.encode())
    except (ValueError, TypeError):
        key = None
    key_matches = isinstance(key, Ed25519PublicKey) and (
        signature.key_id == _guard_public_key_id(key)
    )
    if not key_matches:
        problems.append("embedded public key does not match key_id")
    signature_valid = False
    if isinstance(key, Ed25519PublicKey):
        try:
            raw_signature = base64.b64decode(signature.value_base64, validate=True)
            key.verify(raw_signature, encoded)
            signature_valid = True
        except (ValueError, InvalidSignature):
            signature_valid = False
    if not signature_valid:
        problems.append(f"Ed25519 {label} signature is invalid")
    return GuardDecisionVerification(
        valid=digest_matches and key_matches and signature_valid,
        key_matches=key_matches,
        digest_matches=digest_matches,
        signature_valid=signature_valid,
        problems=problems,
    )


def build_tool_approval_grant(
    *,
    tool: str,
    granted_by: str,
    justification: str,
    evaluation_source: str,
    requested_by: str = "client-runtime",
    expires_at: str | None = None,
    granted_at: str | None = None,
) -> ToolApprovalGrant:
    """Assemble an approval grant with a deterministic ``approval_id``."""

    timestamp = granted_at or datetime.now(timezone.utc).isoformat()
    grant = ToolApprovalGrant(
        approval_id="pending",
        granted_at=timestamp,
        tool=tool,
        requested_by=requested_by,
        granted_by=granted_by,
        justification=justification,
        expires_at=expires_at,
        evaluation_source=evaluation_source,
    )
    seed = _guard_canonical_bytes(
        {
            "tool": grant.tool,
            "requested_by": grant.requested_by,
            "granted_by": grant.granted_by,
            "granted_at": grant.granted_at,
            "justification": grant.justification,
            "evaluation_source": grant.evaluation_source,
        }
    )
    return grant.model_copy(
        update={"approval_id": f"APPROVAL-{hashlib.sha256(seed).hexdigest()[:24].upper()}"}
    )


def build_tool_approval_revocation(
    *,
    approval_id: str,
    revoked_by: str,
    reason: str,
    revoked_at: str | None = None,
) -> ToolApprovalRevocation:
    """Assemble an approval revocation with a deterministic ``revocation_id``."""

    timestamp = revoked_at or datetime.now(timezone.utc).isoformat()
    seed = _guard_canonical_bytes(
        {"approval_id": approval_id, "revoked_by": revoked_by, "revoked_at": timestamp}
    )
    return ToolApprovalRevocation(
        revocation_id=f"REVOCATION-{hashlib.sha256(seed).hexdigest()[:24].upper()}",
        approval_id=approval_id,
        revoked_at=timestamp,
        revoked_by=revoked_by,
        reason=reason,
    )


def _sign_guard_model(
    payload: _GuardModel | dict[str, Any], private_key_path: str | Path, public_key_path: str | Path
):
    """Sign any guard payload (pydantic model or plain mapping) with the key pair."""

    private_key = _load_guard_private_key(private_key_path)
    public_key = _load_guard_public_key(public_key_path)
    if private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) != public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ):
        raise ValueError("guard private and public keys do not match")
    body = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    encoded = _guard_canonical_bytes(body)
    signature = private_key.sign(encoded)
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return GuardSignature(
        key_id=_guard_public_key_id(public_key),
        signed_payload_sha256=hashlib.sha256(encoded).hexdigest(),
        value_base64=base64.b64encode(signature).decode("ascii"),
        public_key_pem=public_pem,
    )


def sign_tool_approval(
    payload: ToolApprovalGrant,
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> SignedApprovalReceipt:
    """Sign an approval grant (same Ed25519 scheme as guard decisions)."""

    return SignedApprovalReceipt(
        payload=payload,
        signature=_sign_guard_model(payload, private_key_path, public_key_path),
    )


def sign_tool_approval_revocation(
    payload: ToolApprovalRevocation,
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> SignedApprovalRevocationReceipt:
    """Sign an approval revocation (same Ed25519 scheme as guard decisions)."""

    return SignedApprovalRevocationReceipt(
        payload=payload,
        signature=_sign_guard_model(payload, private_key_path, public_key_path),
    )


def verify_tool_approval(receipt: SignedApprovalReceipt) -> GuardDecisionVerification:
    """Verify a signed approval receipt (digest, key binding, signature)."""

    return _verify_signed_receipt(
        receipt.payload.model_dump(mode="json"),
        receipt.signature,
        "approval",
    )


def verify_tool_approval_revocation(
    receipt: SignedApprovalRevocationReceipt,
) -> GuardDecisionVerification:
    """Verify a signed approval-revocation receipt."""

    return _verify_signed_receipt(
        receipt.payload.model_dump(mode="json"),
        receipt.signature,
        "approval-revocation",
    )


# ---------------------------------------------------------------------------
# Signed evaluation snapshots (the guard's authenticated input)
# ---------------------------------------------------------------------------
#
# The guard server reloads an evaluation file before every screening. That
# file is the only input crossing the recommendation boundary, and until now
# nothing bound it to a producer: anyone able to write the file could move the
# guard. A snapshot wraps the recommendation in an Ed25519-signed, time-bounded
# envelope, and the guard verifies it against a key configured *outside* the
# snapshot.
#
# What this does and does not buy:
#
# - It authenticates *who signed* and *when*, so an unsigned or stale file can
#   no longer silently steer the posture, and a replayed old snapshot expires.
# - It does **not** prove the signer is the evaluator: the trust key is still
#   configured by the operator, so the root of trust is unresolved.
# - It does not authenticate the trajectories behind the recommendation; the
#   evaluation itself remains unsigned.


class EvaluationSnapshotPayload(_GuardModel):
    """Immutable, time-bounded recommendation handed to the guard server."""

    schema_version: Literal["0.1"] = "0.1"
    snapshot_id: str
    issued_at: str
    expires_at: str
    evaluation_source: str
    recommended_autonomy: AutonomyLevel
    block_release: bool
    sandbox_evidence_declared: bool = False

    @field_validator("issued_at")
    @classmethod
    def validate_issued_at(cls, value: str) -> str:
        try:
            _parse_aware_timestamp(value)
        except ValueError as exc:
            raise ValueError(
                f"issued_at must be a timezone-aware ISO-8601 timestamp: {exc}"
            ) from exc
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str) -> str:
        try:
            _parse_aware_timestamp(value)
        except ValueError as exc:
            raise ValueError(
                f"expires_at must be a timezone-aware ISO-8601 timestamp: {exc}"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "EvaluationSnapshotPayload":
        if _parse_aware_timestamp(self.expires_at) <= _parse_aware_timestamp(self.issued_at):
            raise ValueError("expires_at must be strictly after issued_at")
        return self


class SignedEvaluationSnapshot(_GuardModel):
    payload: EvaluationSnapshotPayload
    signature: GuardSignature


class EvaluationSnapshotVerification(_GuardModel):
    valid: bool
    key_matches: bool
    digest_matches: bool
    signature_valid: bool
    trusted: bool
    current: bool
    problems: list[str]


def build_evaluation_snapshot(
    *,
    evaluation_source: str,
    recommended_autonomy: AutonomyLevel | str,
    block_release: bool,
    sandbox_evidence_declared: bool = False,
    ttl_seconds: float = 900.0,
    issued_at: str | None = None,
) -> EvaluationSnapshotPayload:
    """Assemble a time-bounded snapshot payload with a deterministic id.

    ``ttl_seconds`` bounds how long the snapshot may steer the guard; an
    expired snapshot is refused rather than replayed indefinitely.
    """

    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued = _parse_aware_timestamp(issued_at) if issued_at else datetime.now(timezone.utc)
    expires = issued + timedelta(seconds=ttl_seconds)
    seed = _guard_canonical_bytes(
        {
            "evaluation_source": evaluation_source,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
            "recommended_autonomy": _coerce_autonomy(recommended_autonomy).value,
            "block_release": block_release,
        }
    )
    return EvaluationSnapshotPayload(
        snapshot_id=f"SNAPSHOT-{hashlib.sha256(seed).hexdigest()[:24].upper()}",
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
        evaluation_source=evaluation_source,
        recommended_autonomy=_coerce_autonomy(recommended_autonomy),
        block_release=block_release,
        sandbox_evidence_declared=sandbox_evidence_declared,
    )


def sign_evaluation_snapshot(
    payload: EvaluationSnapshotPayload,
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> SignedEvaluationSnapshot:
    """Sign a snapshot with Ed25519 (same scheme as guard decisions)."""

    return SignedEvaluationSnapshot(
        payload=payload,
        signature=_sign_guard_model(payload, private_key_path, public_key_path),
    )


def verify_evaluation_snapshot(
    receipt: SignedEvaluationSnapshot,
    trusted_public_key_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> EvaluationSnapshotVerification:
    """Verify a signed snapshot: signature, optional trust key, and freshness.

    Without ``trusted_public_key_path`` the embedded key is accepted on
    self-consistency alone and ``trusted`` stays ``False``: the caller must
    not treat an unanchored key as an identity.
    """

    core = _verify_signed_receipt(
        receipt.payload.model_dump(mode="json"),
        receipt.signature,
        "evaluation-snapshot",
    )
    problems = list(core.problems)

    trusted = False
    if trusted_public_key_path is not None:
        trusted = _embedded_key_matches_trust_key(
            receipt.signature.public_key_pem, trusted_public_key_path
        )
        if not trusted:
            problems.append("evaluation snapshot signing key is not the configured trust key")

    current = True
    moment = now or datetime.now(timezone.utc)
    issued = _parse_aware_timestamp(receipt.payload.issued_at)
    expires = _parse_aware_timestamp(receipt.payload.expires_at)
    if issued > moment:
        current = False
        problems.append("evaluation snapshot is not yet valid (issued_at is in the future)")
    elif moment >= expires:
        current = False
        problems.append("evaluation snapshot expired (now is at or after expires_at)")

    return EvaluationSnapshotVerification(
        valid=core.valid and current and (trusted or trusted_public_key_path is None),
        key_matches=core.key_matches,
        digest_matches=core.digest_matches,
        signature_valid=core.signature_valid,
        trusted=trusted,
        current=current,
        problems=problems,
    )


# ---------------------------------------------------------------------------
# Signed trajectories (the evaluator's authenticated input)
# ---------------------------------------------------------------------------
#
# The plan (§9, bloqueo 4) records the remaining gap: evaluation snapshots
# authenticate *who recommended and when*, not *what was observed* — the
# trajectories behind every recommendation still arrive unsigned. This
# envelope closes that gap with the same Ed25519 scheme as the ledger: a
# producer wraps one canonical trajectory in a signed envelope, and any
# consumer (CLI or future System 1 sensor) verifies it before evaluation.
#
# What this does and does not buy:
#
# - It authenticates the producer key and detects any mutation of the
#   trajectory bytes after signing; tampering fails verification.
# - It does **not** authenticate the production events behind the producer:
#   whoever holds the producer key can sign fabricated observations, so the
#   root of trust remains the operator-configured key (bloqueo 2).
# - It does not authenticate the evaluator's recommendation; that is the
#   evaluation snapshot's job (above).


class SignedTrajectory(_GuardModel):
    """Ed25519-signed envelope around one canonical trajectory."""

    schema_version: Literal["0.1"] = "0.1"
    trajectory_id: str
    contract_id: str
    contract_version: str
    producer: str
    payload: dict[str, Any]
    signature: GuardSignature


class TrajectoryVerification(_GuardModel):
    """Result of verifying a signed trajectory envelope."""

    valid: bool
    key_matches: bool
    digest_matches: bool
    signature_valid: bool
    trusted: bool
    problems: list[str]


def sign_trajectory_payload(
    payload: dict[str, Any],
    private_key_path: str | Path,
    public_key_path: str | Path,
    *,
    producer: str = "trajectory-producer",
) -> SignedTrajectory:
    """Wrap and sign a canonical trajectory payload (ledger Ed25519 scheme).

    ``payload`` must be the trajectory mapping itself (same document that
    ``load_trajectory`` would parse). The envelope records the trajectory's
    identity fields so a mismatch between envelope and payload is detectable.
    """

    trajectory_id = payload.get("trajectory_id")
    contract_id = payload.get("contract_id")
    contract_version = payload.get("contract_version")
    if not isinstance(trajectory_id, str) or not trajectory_id:
        raise ValueError("payload must contain a non-empty trajectory_id")
    if not isinstance(contract_id, str) or not contract_id:
        raise ValueError("payload must contain a non-empty contract_id")
    if not isinstance(contract_version, str) or not contract_version:
        raise ValueError("payload must contain a non-empty contract_version")
    return SignedTrajectory(
        trajectory_id=trajectory_id,
        contract_id=contract_id,
        contract_version=contract_version,
        producer=producer,
        payload=payload,
        signature=_sign_guard_model(payload, private_key_path, public_key_path),
    )


def verify_trajectory_envelope(
    envelope: SignedTrajectory,
    trusted_public_key_path: str | Path | None = None,
) -> TrajectoryVerification:
    """Verify a signed trajectory: digest, embedded key, signature, trust key.

    Without ``trusted_public_key_path`` the embedded key is accepted on
    self-consistency alone and ``trusted`` stays ``False`` — the caller must
    not treat an unanchored key as an identity.
    """

    core = _verify_signed_receipt(envelope.payload, envelope.signature, "trajectory")
    problems = list(core.problems)

    identity_problems = [
        f"envelope {field} does not match the signed payload"
        for field, expected, actual in (
            ("trajectory_id", envelope.trajectory_id, envelope.payload.get("trajectory_id")),
            ("contract_id", envelope.contract_id, envelope.payload.get("contract_id")),
            (
                "contract_version",
                envelope.contract_version,
                envelope.payload.get("contract_version"),
            ),
        )
        if expected != actual
    ]
    problems.extend(identity_problems)

    trusted = False
    if trusted_public_key_path is not None:
        trusted = _embedded_key_matches_trust_key(
            envelope.signature.public_key_pem, trusted_public_key_path
        )
        if not trusted:
            problems.append("trajectory signing key is not the configured trust key")

    return TrajectoryVerification(
        valid=core.valid and not identity_problems and (trusted or trusted_public_key_path is None),
        key_matches=core.key_matches,
        digest_matches=core.digest_matches,
        signature_valid=core.signature_valid,
        trusted=trusted,
        problems=problems,
    )


def load_signed_trajectory(
    path: str | Path,
    trusted_public_key_path: str | Path,
) -> SignedTrajectory:
    """Load and verify a signed trajectory from disk, returning the envelope.

    The file must be a :class:`SignedTrajectory` envelope whose signature
    verifies against ``trusted_public_key_path``. Any structural, identity,
    cryptographic or trust problem raises ``ValueError`` — the caller must
    refuse to evaluate, never proceed on an unauthenticated trajectory.
    The caller remains responsible for binding ``envelope.payload`` to the
    trajectory document it actually evaluates (exact-payload equality).
    """

    resolved = Path(path)
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{resolved} not found: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"{resolved} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or "payload" not in document or "signature" not in document:
        raise ValueError(
            f"{resolved} is not a signed trajectory envelope; a trusted verification key is "
            "configured, so an unsigned trajectory is refused"
        )
    try:
        envelope = SignedTrajectory.model_validate(document)
    except Exception as exc:
        raise ValueError(f"{resolved} is not a valid signed trajectory: {exc}") from exc
    verification = verify_trajectory_envelope(envelope, trusted_public_key_path)
    if not verification.valid:
        raise ValueError(
            f"{resolved} signed trajectory is not trusted: "
            f"{'; '.join(verification.problems)}"
        )
    return envelope


# ---------------------------------------------------------------------------
# Signed recovery evidence (the guard's authenticated relaxation input)
# ---------------------------------------------------------------------------
#
# Evaluation snapshots may tighten the guard's autonomy dynamically but never
# relax it (``adapter.update`` blocks every increase without sandbox evidence).
# This envelope is the missing half: a time-bounded, separately signed claim
# that a recovery was actually run and observed, authorizing exactly one
# increase above the autonomy the evaluation snapshot currently recommends.
#
# Separation of keys is deliberate: the recovery signer must not be the same
# key that signs evaluations (or the runtime that is being confined), so the
# guard takes its recovery trust key on a different CLI flag and the two roles
# can be held by different parties.
#
# What this does and does not buy:
#
# - It authenticates that the configured recovery authority signed a recovery
#   claim, scoped to one evaluation snapshot (``target_snapshot_id``), bounded
#   in time, and capped at one autonomy level.
# - It does **not** attest who produced the recovery run itself; the trust
#   root remains the operator-configured key, as with evaluation snapshots.


class RecoveryEvidencePayload(_GuardModel):
    """Immutable, time-bounded claim that a recovery was executed and passed."""

    schema_version: Literal["0.1"] = "0.1"
    evidence_id: str
    issued_at: str
    expires_at: str
    target_snapshot_id: Annotated[str, Field(min_length=8, max_length=64)]
    target_autonomy: AutonomyLevel
    recovery_run_id: Annotated[str, Field(min_length=3, max_length=64)]

    @field_validator("issued_at")
    @classmethod
    def validate_issued_at(cls, value: str) -> str:
        try:
            _parse_aware_timestamp(value)
        except ValueError as exc:
            raise ValueError(
                f"issued_at must be a timezone-aware ISO-8601 timestamp: {exc}"
            ) from exc
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str) -> str:
        try:
            _parse_aware_timestamp(value)
        except ValueError as exc:
            raise ValueError(
                f"expires_at must be a timezone-aware ISO-8601 timestamp: {exc}"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "RecoveryEvidencePayload":
        if _parse_aware_timestamp(self.expires_at) <= _parse_aware_timestamp(self.issued_at):
            raise ValueError("expires_at must be strictly after issued_at")
        return self


class SignedRecoveryEvidence(_GuardModel):
    payload: RecoveryEvidencePayload
    signature: GuardSignature


class RecoveryEvidenceVerification(_GuardModel):
    valid: bool
    signature_valid: bool
    trusted: bool
    current: bool
    problems: list[str]


def build_recovery_evidence(
    *,
    target_snapshot_id: str,
    target_autonomy: AutonomyLevel | str,
    recovery_run_id: str,
    ttl_seconds: float = 3600.0,
    issued_at: str | None = None,
) -> RecoveryEvidencePayload:
    """Assemble a time-bounded recovery-evidence payload with a deterministic id.

    ``ttl_seconds`` bounds how long the evidence may authorize relaxation; an
    expired envelope is refused rather than replayed indefinitely.
    """

    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued = _parse_aware_timestamp(issued_at) if issued_at else datetime.now(timezone.utc)
    expires = issued + timedelta(seconds=ttl_seconds)
    seed = _guard_canonical_bytes(
        {
            "target_snapshot_id": target_snapshot_id,
            "target_autonomy": _coerce_autonomy(target_autonomy).value,
            "recovery_run_id": recovery_run_id,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
        }
    )
    return RecoveryEvidencePayload(
        evidence_id=f"RECOVERY-{hashlib.sha256(seed).hexdigest()[:24].upper()}",
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
        target_snapshot_id=target_snapshot_id,
        target_autonomy=_coerce_autonomy(target_autonomy),
        recovery_run_id=recovery_run_id,
    )


def sign_recovery_evidence(
    payload: RecoveryEvidencePayload,
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> SignedRecoveryEvidence:
    """Sign recovery evidence with Ed25519 (same scheme as guard decisions)."""

    return SignedRecoveryEvidence(
        payload=payload,
        signature=_sign_guard_model(payload, private_key_path, public_key_path),
    )


def verify_recovery_evidence(
    receipt: SignedRecoveryEvidence,
    trusted_public_key_path: str | Path,
    *,
    now: datetime | None = None,
) -> RecoveryEvidenceVerification:
    """Verify recovery evidence: signature, trust key, and freshness.

    Unlike evaluation snapshots, recovery evidence always requires a
    configured trust key: an envelope nobody anchored is never permission.
    """

    core = _verify_signed_receipt(
        receipt.payload.model_dump(mode="json"),
        receipt.signature,
        "recovery-evidence",
    )
    problems = list(core.problems)

    trusted = _embedded_key_matches_trust_key(
        receipt.signature.public_key_pem, trusted_public_key_path
    )
    if not trusted:
        problems.append("recovery evidence signing key is not the configured trust key")

    current = True
    moment = now or datetime.now(timezone.utc)
    issued = _parse_aware_timestamp(receipt.payload.issued_at)
    expires = _parse_aware_timestamp(receipt.payload.expires_at)
    if issued > moment:
        current = False
        problems.append("recovery evidence is not yet valid (issued_at is in the future)")
    elif moment >= expires:
        current = False
        problems.append("recovery evidence expired (now is at or after expires_at)")

    return RecoveryEvidenceVerification(
        valid=core.valid and current and trusted,
        signature_valid=core.signature_valid,
        trusted=trusted,
        current=current,
        problems=problems,
    )
