"""MCP-style pre-tool-call guard server: inline enforcement over stdio.

The :mod:`autopilot.enforcement` adapter and the ``guard`` CLI screen tool
calls *offline* — the conductor must invoke them and interpret the result.
This module closes that gap: a long-lived, MCP-style JSON-RPC 2.0 server
(over stdio, line-delimited) that a client agent runtime can wire as its
pre-tool-call hook. Every tool execution request is screened **before** it
reaches the real tool, at the posture derived from Integrity's latest
evaluation.

It is deliberately *MCP-style*, not a certified MCP implementation: the
framing (``initialize``, ``tools/list``, ``tools/call``, ``ping``,
notifications) follows the model-context-protocol conventions, but the
protocol version reported is Integrity's own (``0.1``) and the transport is
plain line-delimited stdio JSON-RPC.

Boundary: the server executes the **client's declared tool policy** at the
latest recommendation reloaded from its evaluation file before each screening.
It does not evaluate trajectories itself or launch tools. More restrictive
updates apply immediately; less restrictive updates remain blocked — including
from a *validly signed* snapshot, since a signature is not sandbox evidence.
When a ledger and matching Ed25519 key pair are configured, effective posture is
restored from verified signed decisions and the most restrictive state is
retained across restarts. The evaluation producer's identity remains outside
this process's trust boundary: ``--evaluation-public-key`` only binds the
signature to a configured key, it does not attest who holds it.

Authenticated relaxation: snapshots can tighten the posture dynamically but
never loosen it. The only path upward is a **signed recovery-evidence
envelope** (:class:`~autopilot.enforcement.SignedRecoveryEvidence`) produced
with ``guard-sign-recovery``, bound to the evaluation snapshot's id, and
verified against ``--recovery-public-key`` — a key deliberately separate from
the evaluation signer and from the runtime being confined. It authorizes
relaxation to the evidence's signed target level, never above ``SUPERVISE``:
unattended operation stays out of recovery's reach, and the envelope only
applies while both the snapshot and the evidence are current.

Fail-closed rules:

- The evaluation file is the only input crossing the recommendation
  boundary. When ``--evaluation-public-key`` is configured it must be a
  signed, unexpired snapshot whose signature binds to that key; anything
  else (unsigned, altered, wrong signer, replayed past ``expires_at``)
  quarantines the server and blocks screening. Without that flag the file
  stays an unauthenticated, advisory input that can only tighten.
- Unknown methods → ``-32601``; malformed JSON → ``-32700``; invalid params →
  ``-32602``. Errors never default to "allow".
- ``tools/call`` results carry ``is_error: true`` whenever execution is not
  authorized (denied **or** awaiting approval).
- When ``--record-ledger`` is configured, every screened call is signed and
  anchored to the append-only audit ledger (``guard_decision_recorded``);
  a signing or ledger failure aborts the call with ``-32000`` rather than
  letting it through unrecorded.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, TextIO

from autopilot.audit import (
    AuditEventType,
    AuditLedger,
    record_guard_decision,
    record_tool_approval,
    record_tool_approval_revocation,
    verify_audit_ledger,
)
from autopilot.contracts.models import AutonomyLevel
from autopilot.enforcement import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    AutonomyTransition,
    EnforcementAdapter,
    SignedEvaluationSnapshot,
    SignedGuardDecisionReceipt,
    ToolDecision,
    ToolPolicy,
    ApprovalExpiryWarning,
    build_guard_decision_payload,
    build_tool_approval_grant,
    build_tool_approval_revocation,
    load_tool_policy,
    sign_guard_decision,
    sign_tool_approval,
    sign_tool_approval_revocation,
    SignedRecoveryEvidence,
    verify_evaluation_snapshot,
    verify_guard_decision,
    verify_recovery_evidence,
    verify_tool_approval,
)
import yaml

SERVER_NAME = "integrity-guard"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "0.1"

# Attributable human actor ids: lowercase snake_case (mirrors the audit
# ledger's actor field convention).
_ACTOR_ID = re.compile(r"[a-z][a-z0-9_]{1,62}")
_AUTONOMY_RESTRICTION = {
    AutonomyLevel.COACH: 0,
    AutonomyLevel.SUPERVISE: 1,
    AutonomyLevel.RESTRICT: 2,
    AutonomyLevel.QUARANTINE: 3,
}

# JSON-RPC 2.0 error codes used by this server.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_SERVER_ERROR = -32000
_INTERNAL_ERROR = -32603

_CALLABLE_METHODS = (
    "tools/list",
    "tools/call",
    "guard/screen",
    "guard/approve",
    "guard/revoke",
    "guard/approvals",
    "ping",
)


class _JsonRpcError(Exception):
    """Internal signal carrying a JSON-RPC error response."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _read_document(evaluation_path: str | Path) -> dict[str, Any]:
    """Read a JSON (or YAML) mapping from disk with a uniform error message."""

    resolved = Path(evaluation_path)
    suffix = resolved.suffix.lower()
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            if suffix == ".json":
                payload = json.load(handle)
            else:
                payload = yaml.safe_load(handle)
    except OSError as exc:
        raise ValueError(f"cannot read evaluation input {resolved}: {exc}") from exc
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot parse evaluation input {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{resolved} must contain a mapping/object")
    return payload


def _evaluation_body(document: dict[str, Any], resolved: Path) -> dict[str, Any]:
    """Extract the recommendation fields, rejecting trajectories with guidance."""

    if "recommended_autonomy" in document and "block_release" in document:
        return document
    if "observations" in document:
        raise ValueError(
            f"{resolved} looks like a trajectory; evaluate it first "
            "(behavioural-autopilot evaluate ...) and pass the evaluation JSON"
        )
    raise ValueError(
        f"{resolved} is missing required field(s): recommended_autonomy, block_release"
    )


def load_evaluation_document(evaluation_path: str | Path) -> dict[str, Any]:
    """Load the guard's evaluation input (JSON, or YAML with the same fields).

    Must carry ``recommended_autonomy`` and ``block_release``. A trajectory
    YAML (``observations`` key, no recommendation fields) is rejected with
    guidance to evaluate it first — the same honest surface as the ``guard``
    CLI.
    """

    resolved = Path(evaluation_path)
    return _evaluation_body(_read_document(resolved), resolved)


def load_signed_evaluation_document(
    evaluation_path: str | Path,
    trusted_public_key_path: str | Path,
) -> dict[str, Any]:
    """Load a **signed, current** evaluation snapshot and return its body.

    The file must be a :class:`SignedEvaluationSnapshot` envelope whose
    signature verifies against ``trusted_public_key_path`` and whose validity
    window has not closed. Any structural, cryptographic, trust or freshness
    problem raises ``ValueError`` — the caller must quarantine, never proceed.
    """

    resolved = Path(evaluation_path)
    document = _read_document(resolved)
    if "payload" not in document or "signature" not in document:
        raise ValueError(
            f"{resolved} is not a signed evaluation snapshot; a trusted signing key is "
            "configured, so an unsigned evaluation is refused"
        )
    try:
        receipt = SignedEvaluationSnapshot.model_validate(document)
    except Exception as exc:
        raise ValueError(f"{resolved} is not a valid signed evaluation snapshot: {exc}") from exc
    verification = verify_evaluation_snapshot(receipt, trusted_public_key_path)
    if not verification.valid:
        raise ValueError(
            f"{resolved} evaluation snapshot is not trusted: "
            f"{'; '.join(verification.problems)}"
        )
    return _evaluation_body(receipt.payload.model_dump(mode="json"), resolved)


class _EvaluationRecommendation:
    """Duck-typed evaluation exposing ``recommended_autonomy``/``block_release``."""

    def __init__(self, recommended_autonomy: str, block_release: bool) -> None:
        self.recommended_autonomy = recommended_autonomy
        self.block_release = block_release


class GuardServer:
    """Stateful, stdio-session guard screening tool calls inline.

    One server instance holds the loaded tool policy, the adapter at the
    evaluation-derived posture, and (optionally) the signing key paths and
    ledger for per-call receipts. Requests are handled one line at a time;
    notifications produce no response.
    """

    def __init__(
        self,
        policy_path: str | Path,
        evaluation_path: str | Path,
        *,
        ledger_path: str | Path | None = None,
        private_key_path: str | Path | None = None,
        public_key_path: str | Path | None = None,
        evaluation_public_key_path: str | Path | None = None,
        recovery_public_key_path: str | Path | None = None,
        recovery_evidence_path: str | Path | None = None,
        bootstrap_autonomy_from_evaluation: bool = False,
    ) -> None:
        policy: ToolPolicy = load_tool_policy(policy_path)
        if (recovery_public_key_path is None) != (recovery_evidence_path is None):
            raise ValueError(
                "authenticated recovery requires --recovery-public-key and "
                "--recovery-evidence together"
            )
        self._policy_path = Path(policy_path)
        self._evaluation_path = Path(evaluation_path)
        self._evaluation_public_key_path = evaluation_public_key_path
        payload, trust_problem, snapshot = self._read_evaluation()
        recommendation = payload["recommended_autonomy"]
        block_release = payload["block_release"]
        if type(recommendation) is not str or type(block_release) is not bool:
            raise ValueError("evaluation requires string recommended_autonomy and boolean block_release")
        # Validate the initial recommendation instead of silently coercing
        # truthy values or allowing an unknown posture to be reported as current.
        initial_level = AutonomyLevel(recommendation)
        self._block_release = block_release
        self._adapter = EnforcementAdapter.from_evaluation(
            _EvaluationRecommendation(initial_level.value, block_release),
            policy,
        )
        self._policy = policy
        self._ledger_path = Path(ledger_path) if ledger_path is not None else None
        self._private_key_path = private_key_path
        self._public_key_path = public_key_path
        self._initialized = False
        self._recommended_autonomy = self._adapter.autonomy
        self._sandbox_evidence_declared = False
        self._last_transition: AutonomyTransition | None = None
        self._evaluation_refresh_error: str | None = None
        self._evaluation_trust_problem: str | None = trust_problem
        self._evaluation_snapshot: dict[str, Any] | None = snapshot
        self._durable_state_problem: str | None = None
        self._recovery_public_key_path = recovery_public_key_path
        self._recovery_evidence_path = (
            Path(recovery_evidence_path) if recovery_evidence_path is not None else None
        )
        self._recovery_problem: str | None = None
        self._recovery_trusted = False
        self._recovery_current = False
        self._recovery_applied: str | None = None
        self._recovery_envelope: SignedRecoveryEvidence | None = None
        self._bootstrap_autonomy_from_evaluation = bootstrap_autonomy_from_evaluation
        if (ledger_path is not None) != (private_key_path is not None) or (
            ledger_path is not None
        ) != (public_key_path is not None):
            raise ValueError(
                "durable autonomy state requires --record-ledger, --private-key and "
                "--public-key together"
            )
        if bootstrap_autonomy_from_evaluation and self._ledger_path is None:
            raise ValueError("autonomy bootstrap requires durable ledger recording and signing keys")
        if self._evaluation_trust_problem is not None:
            # A failed authentication at construction must not be papered over
            # by the bootstrap path: the posture is already QUARANTINE and the
            # screening block below keeps it there.
            self._adapter.update(AutonomyLevel.QUARANTINE)
        if self._ledger_path is not None:
            self._restore_durable_autonomy()
            if self._durable_state_problem is None:
                self._persist_autonomy_state()

    # ------------------------------------------------------------------ state

    @property
    def posture(self) -> str:
        return self._adapter.posture.value

    @property
    def autonomy(self) -> str:
        return self._adapter.autonomy.value

    @property
    def evaluation_trust_problem(self) -> str | None:
        return self._evaluation_trust_problem

    @property
    def evaluation_snapshot(self) -> dict[str, Any] | None:
        return dict(self._evaluation_snapshot) if self._evaluation_snapshot else None

    # --------------------------------------------------------------- protocol

    def _error(self, request_id: Any, code: int, message: str) -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            ensure_ascii=False,
        )

    def handle_request(self, line: str) -> str | None:
        """Handle one raw JSON-RPC request line; return the response or ``None``."""

        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            return self._error(None, _PARSE_ERROR, f"parse error: {exc}")

        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            if request_id is None:
                return None
            return self._error(request_id, _INVALID_REQUEST, "missing method")
        if "id" not in request:
            # Notification: acknowledged silently, no response frame.
            if method == "notifications/initialized":
                self._initialized = True
            return None
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(request_id, _INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except _JsonRpcError as exc:
            return self._error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive boundary
            return self._error(request_id, _INTERNAL_ERROR, f"internal error: {exc}")
        return json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            ensure_ascii=False,
        )

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            self._refresh_evaluation()
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "guard": {
                    "adapter": ADAPTER_NAME,
                    "adapter_version": ADAPTER_VERSION,
                    "recommended_autonomy": self._recommended_autonomy.value,
                    "effective_autonomy": self.autonomy,
                    "posture": self.posture,
                    "block_release": self._block_release,
                    "autonomy_transition": self._transition_record(),
                    "evaluation_refresh_error": self._evaluation_refresh_error,
                    "evaluation_trust_problem": self._evaluation_trust_problem,
                    "evaluation_snapshot": self._evaluation_snapshot,
                    "evaluation_public_key_configured": self._evaluation_public_key_path is not None,
                    "recovery_evidence": self._recovery_evidence_state(),
                    "ledger_recording": self._ledger_path is not None,
                    "durable_autonomy_state": self._ledger_path is not None,
                    "durable_state_problem": self._durable_state_problem,
                    "expiry_warnings": [
                        _warning_record(warning)
                        for warning in self._adapter.approval_expiry_warnings()
                    ],
                },
            }
        if method == "ping":
            self._require_initialized()
            return {"pong": True}
        if method == "tools/list":
            self._require_initialized()
            return {"tools": self._tool_descriptors()}
        if method == "tools/call":
            self._require_initialized()
            return self._tools_call(params)
        if method == "guard/screen":
            self._require_initialized()
            return self._guard_screen(params)
        if method == "guard/approve":
            self._require_initialized()
            return self._guard_approve(params)
        if method == "guard/revoke":
            self._require_initialized()
            return self._guard_revoke(params)
        if method == "guard/approvals":
            self._require_initialized()
            result: dict[str, Any] = {"approvals": self._adapter.registered_approvals()}
            expiry_warnings = self._adapter.approval_expiry_warnings()
            if expiry_warnings:
                result["expiry_warnings"] = [
                    _warning_record(warning) for warning in expiry_warnings
                ]
            return result
        raise _JsonRpcError(_METHOD_NOT_FOUND, f"unknown method: {method}")

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise _JsonRpcError(
                _SERVER_ERROR,
                "server not initialized; send initialize + notifications/initialized first",
            )

    # ---------------------------------------------------------------- methods

    def _tool_descriptors(self) -> list[dict[str, Any]]:
        declared = sorted(self._policy.sensitive_tools | self._policy.allowlisted_tools)
        return [
            {
                "name": tool,
                "description": (
                    "Screened pre-execution by the Integrity guard; execution "
                    "depends on the current enforcement posture."
                ),
            }
            for tool in declared
        ]

    def _restore_durable_autonomy(self) -> None:
        """Restore the most restrictive authenticated posture in the ledger."""

        assert self._ledger_path is not None
        assert self._public_key_path is not None
        if not self._ledger_path.exists():
            if self._bootstrap_autonomy_from_evaluation:
                return
            self._adapter.update(AutonomyLevel.QUARANTINE)
            return

        try:
            ledger = AuditLedger(self._ledger_path)
            events = ledger.events()
            if not events and self._bootstrap_autonomy_from_evaluation:
                return
            if not events:
                self._adapter.update(AutonomyLevel.QUARANTINE)
                return
            chain_verification = verify_audit_ledger(ledger)
        except (OSError, sqlite3.Error) as exc:
            self._durable_state_problem = f"could not read durable autonomy state: {exc}"
            self._adapter.update(AutonomyLevel.QUARANTINE)
            return
        problems = list(chain_verification.problems)
        effective_levels: list[AutonomyLevel] = []
        for event in events:
            if event.event_type is not AuditEventType.guard_decision_recorded:
                continue
            try:
                receipt = SignedGuardDecisionReceipt.model_validate(event.payload)
                verification = verify_guard_decision(receipt, self._public_key_path)
            except (ValueError, OSError, TypeError) as exc:
                problems.append(f"guard decision event {event.event_id} is invalid: {exc}")
                continue
            if not verification.valid:
                problems.extend(
                    f"guard decision event {event.event_id}: {problem}"
                    for problem in verification.problems
                )
                continue
            effective_levels.append(receipt.payload.effective_autonomy)

        if problems:
            self._durable_state_problem = "; ".join(problems)
            self._adapter.update(AutonomyLevel.QUARANTINE)
            return
        if not effective_levels:
            self._durable_state_problem = (
                "ledger has no authenticated guard decisions; starting quarantined"
            )
            self._adapter.update(AutonomyLevel.QUARANTINE)
            return
        if effective_levels:
            persisted_level = max(
                effective_levels,
                key=lambda level: _AUTONOMY_RESTRICTION[level],
            )
            self._adapter.update(persisted_level)

    def _read_evaluation(self) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
        """Read the evaluation input, authenticating it when a trust key is set.

        Returns ``(payload, trust_problem, snapshot)``. ``trust_problem`` is set
        when a configured trust key rejected the input; the payload is then a
        fail-closed ``QUARANTINE`` recommendation rather than anything the
        caller may act on. Without a trust key, ordinary read/parse errors
        still raise so the caller decides how to fail.
        """

        quarantine = {
            "recommended_autonomy": AutonomyLevel.QUARANTINE.value,
            "block_release": True,
        }
        if self._evaluation_public_key_path is None:
            return load_evaluation_document(self._evaluation_path), None, None
        try:
            payload = load_signed_evaluation_document(
                self._evaluation_path, self._evaluation_public_key_path
            )
        except ValueError as exc:
            return quarantine, str(exc), None
        snapshot = {
            "verified": True,
            "snapshot_id": payload.get("snapshot_id"),
            "issued_at": payload.get("issued_at"),
            "expires_at": payload.get("expires_at"),
            "evaluation_source": payload.get("evaluation_source"),
        }
        return payload, None, snapshot

    def _persist_autonomy_state(self) -> None:
        """Append a signed startup snapshot so the configured ledger is durable."""

        if self._ledger_path is None:
            return
        payload = build_guard_decision_payload(
            adapter=self._adapter,
            evaluation_source=str(self._evaluation_path),
            policy_source=str(self._policy_path),
            decisions=[],
            block_release=self._block_release or self._durable_state_problem is not None,
            recommended_autonomy=self._recommended_autonomy,
        )
        receipt = sign_guard_decision(
            payload,
            self._private_key_path,
            self._public_key_path,
        )
        record_guard_decision(AuditLedger(self._ledger_path), receipt)

    def _refresh_evaluation(self) -> None:
        """Refresh the recommendation before screening; uncertainty quarantines."""

        try:
            payload, trust_problem, snapshot = self._read_evaluation()
        except Exception as exc:
            # Parser and I/O errors from any supported evaluation format are
            # all treated as an unknown posture, never as permission to proceed.
            self._evaluation_trust_problem = None
            self._quarantine(str(exc))
            return
        self._evaluation_trust_problem = trust_problem
        self._evaluation_snapshot = snapshot
        if trust_problem is not None:
            # A snapshot the configured trust key did not accept is unknown
            # input, never a permission: quarantine and expose the reason.
            self._quarantine(f"evaluation snapshot is untrusted: {trust_problem}")
            return
        try:
            raw_recommendation = payload["recommended_autonomy"]
            raw_block_release = payload["block_release"]
            if type(raw_recommendation) is not str:
                raise ValueError("recommended_autonomy must be a string")
            if type(raw_block_release) is not bool:
                raise ValueError("block_release must be a boolean")
            proposed = AutonomyLevel(raw_recommendation)
            if raw_block_release:
                proposed = AutonomyLevel.QUARANTINE
            sandbox_evidence = payload.get("sandbox_evidence") is True
            if "sandbox_evidence" in payload and type(payload["sandbox_evidence"]) is not bool:
                raise ValueError("sandbox_evidence must be a boolean")
        except Exception as exc:
            # Parser and I/O errors from any supported evaluation format are
            # all treated as an unknown posture, never as permission to proceed.
            self._evaluation_trust_problem = None
            self._quarantine(str(exc))
            return

        previous = self._adapter.autonomy
        self._recommended_autonomy = proposed
        self._block_release = raw_block_release
        self._sandbox_evidence_declared = sandbox_evidence
        self._evaluation_refresh_error = None
        # Evaluation snapshots are advisory inputs, not authenticated recovery
        # proofs. They may tighten permissions dynamically but never relax them.
        # A valid signature does not change that: it proves who wrote the
        # snapshot, not that sandbox evidence exists.
        self._last_transition = self._adapter.update(
            proposed,
            sandbox_evidence=False,
        )
        if self._durable_state_problem is not None:
            self._last_transition = self._adapter.update(AutonomyLevel.QUARANTINE)
        if self._durable_state_problem is None:
            self._apply_recovery_relaxation(proposed)
        if self._ledger_path is not None and self._adapter.autonomy is not previous:
            try:
                self._persist_autonomy_state()
            except Exception as exc:
                self._durable_state_problem = f"could not persist autonomy state: {exc}"
                self._last_transition = self._adapter.update(AutonomyLevel.QUARANTINE)

    def _recovery_evidence_state(self) -> dict[str, Any] | None:
        """Report the current recovery-evidence envelope (or why it is absent)."""

        if self._recovery_evidence_path is None:
            return None
        envelope = self._recovery_envelope
        if envelope is None:
            envelope = self._read_recovery_evidence()
        if envelope is None:
            return {
                "configured": True,
                "loaded": False,
                "reason": self._recovery_problem,
            }
        payload = envelope.payload
        state: dict[str, Any] = {
            "configured": True,
            "loaded": True,
            "evidence_id": payload.evidence_id,
            "target_snapshot_id": payload.target_snapshot_id,
            "target_autonomy": payload.target_autonomy.value,
            "recovery_run_id": payload.recovery_run_id,
            "issued_at": payload.issued_at,
            "expires_at": payload.expires_at,
            "trusted": self._recovery_trusted,
            "current": self._recovery_current,
            "reason": self._recovery_problem,
        }
        return state

    def _read_recovery_evidence(self) -> SignedRecoveryEvidence | None:
        """Parse the recovery-evidence envelope; problems go to ``_recovery_problem``."""

        try:
            document = _read_document(self._recovery_evidence_path)
            if "payload" not in document or "signature" not in document:
                raise ValueError("not a signed recovery evidence envelope")
            envelope = SignedRecoveryEvidence.model_validate(document)
        except Exception as exc:
            self._recovery_problem = f"cannot read recovery evidence: {exc}"
            self._recovery_envelope = None
            return None
        self._recovery_envelope = envelope
        return envelope

    def _apply_recovery_relaxation(self, snapshot_level: AutonomyLevel) -> None:
        """Apply one level of authenticated recovery, if the envelope allows it.

        Fail-closed everywhere: an unconfigured, unreadable, untrusted,
        expired, foreign-keyed or mismatched envelope never relaxes anything
        and only surfaces a reason. A valid envelope lifts the posture to the
        evidence's signed ``target_autonomy`` — at most to ``SUPERVISE``:
        unattended operation (``COACH``) is never reachable by recovery, and
        nothing relaxes while a durable-state problem stands (the caller
        checks that).
        """

        self._recovery_problem = None
        if self._recovery_evidence_path is None or self._recovery_public_key_path is None:
            return
        envelope = self._read_recovery_evidence()
        if envelope is None:
            return
        verification = verify_recovery_evidence(
            envelope, self._recovery_public_key_path
        )
        self._recovery_trusted = verification.trusted
        self._recovery_current = verification.current
        if not verification.problems:
            self._recovery_problem = None
        else:
            self._recovery_problem = "; ".join(verification.problems)
        if not verification.valid:
            return
        payload = envelope.payload
        if payload.target_snapshot_id != self._evaluation_snapshot["snapshot_id"]:
            self._recovery_problem = (
                "recovery evidence targets snapshot "
                f"{payload.target_snapshot_id}, not the current "
                f"{self._evaluation_snapshot['snapshot_id']}"
            )
            self._recovery_current = False
            return
        ceiling = _AUTONOMY_RESTRICTION[AutonomyLevel.SUPERVISE]
        target_restriction = _AUTONOMY_RESTRICTION[payload.target_autonomy]
        if target_restriction < ceiling:
            # COACH (unattended operation) is never reachable by relaxation:
            # a permissive-but-signed target is refused, never clamped.
            self._recovery_problem = (
                "recovery evidence never authorizes more than supervised "
                f"operation: {payload.target_autonomy.value} is not reachable "
                "by relaxation"
            )
            return
        authorized = target_restriction
        if _AUTONOMY_RESTRICTION[self._adapter.autonomy] <= authorized:
            return
        target_level = next(
            level
            for level, restriction in _AUTONOMY_RESTRICTION.items()
            if restriction == authorized
        )
        self._recovery_applied = target_level.value
        self._last_transition = self._adapter.update(
            target_level,
            sandbox_evidence=True,
        )

    def _quarantine(self, error: str) -> None:
        """Force QUARANTINE for an unknown or untrusted evaluation input."""

        previous = self._adapter.autonomy
        self._recommended_autonomy = AutonomyLevel.QUARANTINE
        self._block_release = True
        self._sandbox_evidence_declared = False
        self._evaluation_refresh_error = error
        self._last_transition = self._adapter.update(AutonomyLevel.QUARANTINE)
        if self._adapter.autonomy is not previous and self._ledger_path is not None:
            try:
                self._persist_autonomy_state()
            except Exception as persist_error:
                self._durable_state_problem = f"could not persist autonomy state: {persist_error}"
                self._adapter.update(AutonomyLevel.QUARANTINE)

    def _transition_record(self) -> dict[str, Any] | None:
        transition = self._last_transition
        if transition is None:
            return None
        return {
            "previous": transition.previous.value,
            "proposed": transition.proposed.value,
            "applied": transition.applied,
            "guardrail": transition.guardrail,
        }

    def _screen_tool(self, tool: str) -> tuple[ToolDecision, dict[str, Any] | None]:
        self._refresh_evaluation()
        if self._evaluation_trust_problem is not None:
            raise _JsonRpcError(
                _SERVER_ERROR,
                "evaluation snapshot is untrusted; screening is blocked",
            )
        if self._durable_state_problem is not None and self._ledger_path is not None:
            raise _JsonRpcError(
                _SERVER_ERROR,
                "durable autonomy state is untrusted; screening is blocked",
            )
        decision = self._adapter.decide(tool)
        audit = self._maybe_record(decision)
        return decision, audit

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _JsonRpcError(_INVALID_PARAMS, "params.name must be a non-empty string")
        decision, audit = self._screen_tool(name)
        authorized = decision.allowed and not decision.requires_approval
        decision_record = {
            "tool": decision.tool,
            "autonomy": decision.autonomy.value,
            "posture": decision.posture.value,
            "allowed": decision.allowed,
            "requires_approval": decision.requires_approval,
            "reason": decision.reason,
            "recommended_autonomy": self._recommended_autonomy.value,
            "effective_autonomy": self.autonomy,
            "autonomy_transition": self._transition_record(),
            "sandbox_evidence_declared": self._sandbox_evidence_declared,
        }
        if self._evaluation_refresh_error is not None:
            decision_record["evaluation_refresh_error"] = self._evaluation_refresh_error
        _trust_fields(decision_record, self)
        if audit is not None:
            decision_record["audit"] = audit
        warnings = self._adapter.approval_expiry_warnings()
        if warnings:
            decision_record["expiry_warnings"] = [
                _warning_record(warning) for warning in warnings
            ]
        text = (
            f"DENY {decision.tool}: {decision.reason}"
            if not decision.allowed and not decision.requires_approval
            else (
                f"APPROVAL REQUIRED {decision.tool}: {decision.reason}"
                if decision.requires_approval
                else f"ALLOW {decision.tool}: {decision.reason}"
            )
        )
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"decision": decision_record},
            "is_error": not authorized,
        }

    def _guard_screen(self, params: dict[str, Any]) -> dict[str, Any]:
        tool = params.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise _JsonRpcError(_INVALID_PARAMS, "params.tool must be a non-empty string")
        decision, audit = self._screen_tool(tool)
        result = {
            "tool": decision.tool,
            "autonomy": decision.autonomy.value,
            "posture": decision.posture.value,
            "allowed": decision.allowed,
            "requires_approval": decision.requires_approval,
            "reason": decision.reason,
            "recommended_autonomy": self._recommended_autonomy.value,
            "effective_autonomy": self.autonomy,
            "autonomy_transition": self._transition_record(),
            "sandbox_evidence_declared": self._sandbox_evidence_declared,
        }
        if self._evaluation_refresh_error is not None:
            result["evaluation_refresh_error"] = self._evaluation_refresh_error
        _trust_fields(result, self)
        if audit is not None:
            result["audit"] = audit
        warnings = self._adapter.approval_expiry_warnings()
        if warnings:
            result["expiry_warnings"] = [
                _warning_record(warning) for warning in warnings
            ]
        return result

    def _guard_approve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Capture one attributable human approval for a sensitive tool.

        The grant is built, signed with the guard's Ed25519 key, verified,
        anchored in the audit ledger (``tool_approval_granted``), and only
        then registered with the adapter. It never bypasses ``deny_all``:
        at QUARANTINE the tool stays denied regardless of any approval.
        Ledger recording requires the server to run with ``--record-ledger``
        and its key pair; otherwise approvals are refused rather than left
        unrecorded (fail-closed).
        """

        self._refresh_evaluation()
        tool = params.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise _JsonRpcError(_INVALID_PARAMS, "params.tool must be a non-empty string")
        if self._adapter.posture.value != "require_approval":
            raise _JsonRpcError(
                _SERVER_ERROR,
                "human approval grants are only accepted while effective autonomy is SUPERVISE",
            )
        if tool not in self._policy.sensitive_tools:
            raise _JsonRpcError(
                _INVALID_PARAMS,
                "approval grants may only target a declared sensitive tool",
            )
        granted_by = params.get("granted_by")
        if not isinstance(granted_by, str) or not _ACTOR_ID.fullmatch(granted_by):
            raise _JsonRpcError(
                _INVALID_PARAMS,
                "params.granted_by must be a snake_case approver id "
                "(lowercase letters, digits, underscores)",
            )
        justification = params.get("justification")
        if not isinstance(justification, str) or len(justification) < 20:
            raise _JsonRpcError(
                _INVALID_PARAMS,
                "params.justification must be a string of at least 20 characters",
            )
        expires_at = params.get("expires_at")
        if expires_at is not None and not isinstance(expires_at, str):
            raise _JsonRpcError(_INVALID_PARAMS, "params.expires_at must be a string")
        self._require_recording()
        grant = build_tool_approval_grant(
            tool=tool,
            granted_by=granted_by,
            justification=justification,
            evaluation_source=str(self._evaluation_path),
            expires_at=expires_at,
        )
        receipt = sign_tool_approval(
            grant, self._private_key_path, self._public_key_path
        )
        verification = verify_tool_approval(receipt)
        if not verification.valid:
            raise _JsonRpcError(
                _SERVER_ERROR,
                "approval receipt failed verification; not registered",
            )
        try:
            appended = record_tool_approval(AuditLedger(self._ledger_path), receipt)
        except (ValueError, OSError) as exc:
            raise _JsonRpcError(
                _SERVER_ERROR, f"approval ledger recording failed: {exc}"
            ) from exc
        approval_id = self._adapter.register_approval(receipt)
        result: dict[str, Any] = {
            "approval_id": approval_id,
            "tool": grant.tool,
            "granted_by": grant.granted_by,
            "granted_at": grant.granted_at,
            "expires_at": grant.expires_at,
            "audit": {
                "ledger": str(self._ledger_path),
                "appended": appended.inserted,
                "signature_key_id": receipt.signature.key_id,
            },
        }
        return result

    def _guard_revoke(self, params: dict[str, Any]) -> dict[str, Any]:
        """Revoke a registered approval (attributable, verified, ledger-anchored)."""

        approval_id = params.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id.startswith("APPROVAL-"):
            raise _JsonRpcError(
                _INVALID_PARAMS, "params.approval_id must be an APPROVAL-<digest> id"
            )
        revoked_by = params.get("revoked_by")
        if not isinstance(revoked_by, str) or not _ACTOR_ID.fullmatch(revoked_by):
            raise _JsonRpcError(
                _INVALID_PARAMS,
                "params.revoked_by must be a snake_case actor id "
                "(lowercase letters, digits, underscores)",
            )
        reason = params.get("reason")
        if not isinstance(reason, str) or len(reason) < 20:
            raise _JsonRpcError(
                _INVALID_PARAMS, "params.reason must be a string of at least 20 characters"
            )
        self._require_recording()
        revocation = build_tool_approval_revocation(
            approval_id=approval_id,
            revoked_by=revoked_by,
            reason=reason,
        )
        receipt = sign_tool_approval_revocation(
            revocation, self._private_key_path, self._public_key_path
        )
        try:
            appended = record_tool_approval_revocation(
                AuditLedger(self._ledger_path), receipt
            )
        except (ValueError, OSError) as exc:
            raise _JsonRpcError(
                _SERVER_ERROR, f"approval revocation recording failed: {exc}"
            ) from exc
        self._adapter.revoke_approval(receipt)
        return {
            "revocation_id": revocation.revocation_id,
            "approval_id": approval_id,
            "revoked_by": revoked_by,
            "audit": {
                "ledger": str(self._ledger_path),
                "appended": appended.inserted,
            },
        }

    def _require_recording(self) -> None:
        if self._ledger_path is None:
            raise _JsonRpcError(
                _SERVER_ERROR,
                "approval capture requires the server to run with --record-ledger "
                "and its Ed25519 key pair; approvals are never left unrecorded",
            )

    def _maybe_record(self, decision: ToolDecision) -> dict[str, Any] | None:
        """Sign and anchor one screening; failures abort the call (fail-closed)."""

        if self._ledger_path is None:
            return None
        if self._durable_state_problem is not None:
            raise _JsonRpcError(
                _SERVER_ERROR,
                "durable autonomy state is untrusted; screening is blocked",
            )
        payload = build_guard_decision_payload(
            adapter=self._adapter,
            evaluation_source=str(self._evaluation_path),
            policy_source=str(self._policy_path),
            decisions=[decision],
            block_release=self._block_release,
            sandbox_evidence_declared=self._sandbox_evidence_declared,
            recommended_autonomy=self._recommended_autonomy,
        )
        try:
            receipt = sign_guard_decision(
                payload, self._private_key_path, self._public_key_path
            )
            appended = record_guard_decision(AuditLedger(self._ledger_path), receipt)
        except (ValueError, OSError) as exc:
            raise _JsonRpcError(
                _SERVER_ERROR, f"guard ledger recording failed: {exc}"
            ) from exc
        return {
            "ledger": str(self._ledger_path),
            "decision_id": receipt.payload.decision_id,
            "signature_key_id": receipt.signature.key_id,
            "appended": appended.inserted,
        }


def _trust_fields(target: dict[str, Any], server: "GuardServer") -> None:
    """Surface evaluation-snapshot trust on every decision surface."""

    if server._evaluation_trust_problem is not None:
        target["evaluation_trust_problem"] = server._evaluation_trust_problem
    if server._evaluation_snapshot is not None:
        target["evaluation_snapshot"] = server._evaluation_snapshot
    recovery = server._recovery_evidence_state()
    if recovery is not None:
        target["recovery_evidence"] = recovery


def _warning_record(warning: ApprovalExpiryWarning) -> dict[str, Any]:
    """Serialize one advisory expiry warning for a JSON-RPC payload."""

    return {
        "approval_id": warning.approval_id,
        "tool": warning.tool,
        "expires_at": warning.expires_at,
        "expires_in_seconds": round(warning.expires_in_seconds, 3),
        "advisory": True,
    }


def serve(
    server: GuardServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the request loop over line-delimited stdio until EOF."""

    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    for line in input_stream:
        stripped = line.strip()
        if not stripped:
            continue
        response = server.handle_request(stripped)
        if response is not None:
            output_stream.write(response + "\n")
            output_stream.flush()
    return 0
