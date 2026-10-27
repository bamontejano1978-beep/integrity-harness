"""Supervised subprocess boundary with externally enforced capabilities."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

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
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import Conditions, ContractDocument, ExtensibleModel, SemVer
from autopilot.operational_integrity import (
    ObservationSource,
    OperationalIntegrityEvaluation,
    OperationalObservation,
    OperationalObservationKind,
    OperationalTrace,
    evaluate_operational_integrity,
)
from autopilot.trajectory import parse_timestamp

BOUNDARY_NAME = "supervised_python_process_boundary"
DOCKER_BOUNDARY_NAME = "hardened_docker_boundary"
BOUNDARY_VERSION = "0.1.0"

# Puerta C inventory, machine-checkable: the confined worker is this fixed
# package file (hashed into every report) and it inherits no environment
# variable beyond the platform allowlist. No credential is ever handed to it.
WORKER_SOURCE = Path(__file__).with_name("boundary_worker.py")
WORKER_ENVIRONMENT_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
WORKER_FIXED_ENVIRONMENT = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


class BoundaryBackend(str, Enum):
    python_process = "python_process"
    docker = "docker"


class DockerBoundaryConfig(BaseModel):
    """Pinned image and hard resource ceilings for the Docker backend."""

    model_config = ConfigDict(extra="forbid")

    image: Annotated[
        str,
        Field(pattern=r"^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$"),
    ]
    memory_mb: Annotated[int, Field(ge=64, le=32_768, strict=True)] = 256
    cpus: Annotated[float, Field(ge=0.1, le=16.0, strict=True)] = 0.5
    pids_limit: Annotated[int, Field(ge=16, le=4096, strict=True)] = 64
    tmpfs_mb: Annotated[int, Field(ge=1, le=1024, strict=True)] = 16
    user: Annotated[str, Field(pattern=r"^[1-9][0-9]*:[1-9][0-9]*$")] = "65532:65532"
    python_executable: Annotated[str, Field(pattern=r"^[A-Za-z0-9_./-]+$")] = "python3"
    docker_executable: Annotated[str, Field(min_length=1, max_length=500)] = "docker"


class BoundaryRequestKind(str, Enum):
    tool_call = "tool_call"
    claim_payment = "claim_payment"


class WorkerMode(str, Enum):
    cooperative = "cooperative"
    goal_seeking = "goal_seeking"


class PaymentAuthorizationPayload(BaseModel):
    """Signed human approval for one exact payment operation (not a trust root)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    authorization_id: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    execution_id: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    contract_id: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    candidate_version: Annotated[str, Field(min_length=1, max_length=64, strict=True)]
    operation_id: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    operation: Literal["claim_payment"]
    amount_minor: Annotated[int, Field(gt=0, strict=True)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", strict=True)]
    beneficiary_id: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    approved_by: Annotated[str, Field(min_length=1, max_length=255, strict=True)]
    issued_at: str
    expires_at: str

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parse_timestamp(value)
        return value

    @model_validator(mode="after")
    def validate_window(self) -> PaymentAuthorizationPayload:
        if parse_timestamp(self.expires_at) <= parse_timestamp(self.issued_at):
            raise ValueError("expires_at must be later than issued_at")
        return self


class PaymentAuthorizationSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$", strict=True)]
    value_base64: Annotated[str, Field(min_length=1, strict=True)]


class PaymentAuthorizationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: PaymentAuthorizationPayload
    signature: PaymentAuthorizationSignature


class BoundaryRequest(ExtensibleModel):
    ref: Annotated[str, Field(min_length=1, max_length=255)]
    offset_seconds: Annotated[int, Field(ge=1, le=86_400, strict=True)]
    kind: BoundaryRequestKind
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    attributes: Conditions = Field(default_factory=dict)
    payment_authorization: PaymentAuthorizationReceipt | None = None

    @model_validator(mode="after")
    def validate_payment_authorization_placement(self) -> BoundaryRequest:
        if self.kind is not BoundaryRequestKind.claim_payment and self.payment_authorization:
            raise ValueError("payment_authorization is only valid for claim_payment requests")
        return self


class BoundaryExecutionPlan(ExtensibleModel):
    plan_version: Literal["0.1"]
    execution_id: Annotated[str, Field(min_length=1, max_length=255)]
    contract_id: str
    contract_version: SemVer
    candidate_version: SemVer
    started_at: str
    worker_mode: WorkerMode
    stop_after_sequence: Annotated[int, Field(ge=0, le=10_000, strict=True)] | None = None
    stop_condition: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")] = (
        "authorization_revoked"
    )
    requests: list[BoundaryRequest] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_plan(self) -> BoundaryExecutionPlan:
        parse_timestamp(self.started_at)
        refs = [request.ref for request in self.requests]
        if len(refs) != len(set(refs)):
            raise ValueError("request refs must be unique")
        offsets = [request.offset_seconds for request in self.requests]
        if offsets != sorted(offsets) or len(offsets) != len(set(offsets)):
            raise ValueError("request offsets must be unique and strictly increasing")
        if self.stop_after_sequence is not None and self.stop_after_sequence > len(self.requests):
            raise ValueError("stop_after_sequence cannot exceed request count")
        return self


class BoundaryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    request_ref: str
    permitted: bool
    executed: bool
    reason: str
    response: dict[str, Any] = Field(default_factory=dict)


class BoundaryEventType(str, Enum):
    worker_started = "worker_started"
    request_executed = "request_executed"
    request_blocked = "request_blocked"
    stop_issued = "stop_issued"
    worker_terminated = "worker_terminated"
    worker_completed = "worker_completed"
    protocol_error = "protocol_error"


class BoundaryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    event_type: BoundaryEventType
    occurred_at: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class BoundaryStatus(str, Enum):
    passed = "pass"
    failed = "fail"


class BoundarySignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: str
    signed_payload_sha256: str
    value_base64: str


class BoundaryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    report_id: str
    execution_id: str
    contract_id: str
    contract_version: str
    candidate_version: str
    generated_at: str
    boundary_name: str = BOUNDARY_NAME
    boundary_version: str = BOUNDARY_VERSION
    plan_sha256: str
    worker_sha256: str
    boundary_implementation_sha256: str
    status: BoundaryStatus
    terminated: bool
    termination_reason: str | None
    executed_request_count: int = Field(ge=0)
    blocked_request_count: int = Field(ge=0)
    decisions: list[BoundaryDecision]
    events: list[BoundaryEvent]
    chain_head: str
    operational_trace: OperationalTrace
    operational_evaluation: OperationalIntegrityEvaluation
    signature: BoundarySignature
    limitation: str = (
        "The Python subprocess backend enforces the proxy protocol and process termination, "
        "but it is not an OS container and cannot prevent direct filesystem or network syscalls "
        "from arbitrary untrusted code."
    )


class BoundaryVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    report_id_valid: bool
    event_chain_valid: bool
    chain_head_valid: bool
    payload_digest_valid: bool
    signature_valid: bool
    key_id_valid: bool
    semantics_valid: bool
    problems: list[str]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _event_hash(
    sequence: int,
    event_type: BoundaryEventType,
    occurred_at: str,
    payload: dict[str, Any],
    previous_hash: str,
) -> str:
    return _sha256(
        {
            "sequence": sequence,
            "event_type": event_type.value,
            "occurred_at": occurred_at,
            "payload": payload,
            "previous_hash": previous_hash,
        }
    )


def _append_event(
    events: list[BoundaryEvent],
    event_type: BoundaryEventType,
    occurred_at: str,
    payload: dict[str, Any],
) -> None:
    previous_hash = events[-1].event_hash if events else "0" * 64
    sequence = len(events) + 1
    events.append(
        BoundaryEvent(
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
            previous_hash=previous_hash,
            event_hash=_event_hash(
                sequence,
                event_type,
                occurred_at,
                payload,
                previous_hash,
            ),
        )
    )


def _load_private_key(path: str | Path) -> Ed25519PrivateKey:
    resolved = Path(path)
    try:
        key = serialization.load_pem_private_key(resolved.read_bytes(), password=None)
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("private_key", f"not found: {resolved}")]
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
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
        key = serialization.load_pem_public_key(resolved.read_bytes())
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("public_key", f"not found: {resolved}")]
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise DSLValidationError(
            [ValidationIssue("public_key", "is not a valid PEM public key")]
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise DSLValidationError(
            [ValidationIssue("public_key", "must contain an Ed25519 public key")]
        )
    return key


def _key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def sign_payment_authorization(
    payload: PaymentAuthorizationPayload,
    private_key_path: str | Path,
) -> PaymentAuthorizationReceipt:
    """Sign a payment-specific approval; trust in the signer is configured separately."""

    private_key = _load_private_key(private_key_path)
    public_key = private_key.public_key()
    encoded = _canonical_bytes(payload.model_dump(mode="json"))
    signature = PaymentAuthorizationSignature(
        key_id=_key_id(public_key),
        value_base64=base64.b64encode(private_key.sign(encoded)).decode("ascii"),
    )
    return PaymentAuthorizationReceipt(payload=payload, signature=signature)


def _payment_authorization_problem(
    request: BoundaryRequest,
    plan: BoundaryExecutionPlan,
    public_key_path: str | Path | None,
    consumed_authorization_ids: set[str],
    consumed_operation_ids: set[str],
) -> str | None:
    receipt = request.payment_authorization
    if receipt is None:
        return "payment lacks a signed operation-bound human authorization"
    if public_key_path is None:
        return "payment authorization trust key is not configured"
    try:
        public_key = _load_public_key(public_key_path)
    except DSLValidationError:
        return "payment authorization trust key is unavailable or invalid"
    payload = receipt.payload
    if receipt.signature.key_id != _key_id(public_key):
        return "payment authorization signer is not trusted by configured key"
    try:
        signature = base64.b64decode(receipt.signature.value_base64, validate=True)
        public_key.verify(signature, _canonical_bytes(payload.model_dump(mode="json")))
    except (ValueError, InvalidSignature):
        return "payment authorization signature is invalid"

    attrs = request.attributes
    amount = attrs.get("amount_eur")
    if type(amount) not in {int, float}:
        return "payment amount is missing or invalid"
    try:
        decimal_amount = Decimal(str(amount))
        amount_minor = decimal_amount * 100
    except (InvalidOperation, ValueError):
        return "payment amount is missing or invalid"
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        return "payment amount must be positive"
    if amount_minor != amount_minor.to_integral_value():
        return "payment amount must have at most two decimal places"
    if payload.amount_minor != int(amount_minor):
        return "payment authorization amount does not match request"
    currency = attrs.get("currency")
    if type(currency) is not str or payload.currency != currency:
        return "payment authorization currency does not match request"
    if currency != "EUR":
        return "payment currency must be EUR for amount_eur budget enforcement"
    beneficiary_id = attrs.get("beneficiary_id")
    if type(beneficiary_id) is not str or payload.beneficiary_id != beneficiary_id:
        return "payment authorization beneficiary does not match request"
    operation_id = attrs.get("operation_id")
    if type(operation_id) is not str or payload.operation_id != operation_id:
        return "payment authorization operation id does not match request"
    if (
        payload.execution_id != plan.execution_id
        or payload.contract_id != plan.contract_id
        or payload.candidate_version != plan.candidate_version
        or payload.operation != request.name
    ):
        return "payment authorization context does not match request"

    now = datetime.now(timezone.utc)
    issued_at = parse_timestamp(payload.issued_at)
    expires_at = parse_timestamp(payload.expires_at)
    if issued_at > now or expires_at <= now:
        return "payment authorization is not currently valid or has expired"
    if payload.authorization_id in consumed_authorization_ids:
        return "payment authorization replay detected"
    if payload.operation_id in consumed_operation_ids:
        return "payment operation replay detected"
    consumed_authorization_ids.add(payload.authorization_id)
    consumed_operation_ids.add(payload.operation_id)
    return None


def load_boundary_plan(path: str | Path) -> BoundaryExecutionPlan:
    resolved = Path(path)
    try:
        raw = load_yaml_mapping(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc
    try:
        return BoundaryExecutionPlan.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def load_boundary_report(path: str | Path) -> BoundaryReport:
    resolved = Path(path)
    try:
        return BoundaryReport.model_validate_json(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise DSLValidationError(
            [ValidationIssue("boundary_report", "is not a valid boundary report")]
        ) from exc


def _safe_child_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in WORKER_ENVIRONMENT_ALLOWLIST
        if name in os.environ
    }
    environment.update(WORKER_FIXED_ENVIRONMENT)
    return environment


def _container_name(execution_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", execution_id.lower()).strip("-.")
    suffix = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:10]
    prefix = normalized[:45] or "execution"
    return f"autopilot-{prefix}-{suffix}"


def build_docker_command(
    config: DockerBoundaryConfig,
    worker_path: str | Path,
    container_name: str,
) -> list[str]:
    """Build the argv-only hardened Docker command; no shell is involved."""

    resolved_worker = Path(worker_path).resolve()
    mount = (
        f"type=bind,src={resolved_worker},"
        "dst=/opt/autopilot/boundary_worker.py,readonly"
    )
    return [
        config.docker_executable,
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--platform",
        "linux",
        "--name",
        container_name,
        "--hostname",
        "autopilot-worker",
        "--init",
        "--restart",
        "no",
        "--stop-timeout",
        "1",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(config.pids_limit),
        "--memory",
        f"{config.memory_mb}m",
        "--memory-swap",
        f"{config.memory_mb}m",
        "--cpus",
        str(config.cpus),
        "--ulimit",
        "nofile=64:64",
        "--ipc",
        "none",
        "--user",
        config.user,
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={config.tmpfs_mb}m",
        "--mount",
        mount,
        "--workdir",
        "/tmp",
        "--env",
        "PYTHONIOENCODING=utf-8",
        "--env",
        "PYTHONUNBUFFERED=1",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--log-driver",
        "none",
        "--entrypoint",
        config.python_executable,
        config.image,
        "-I",
        "-u",
        "/opt/autopilot/boundary_worker.py",
    ]


def _validate_docker_runtime(config: DockerBoundaryConfig) -> None:
    executable = shutil.which(config.docker_executable)
    if executable is None:
        raise DSLValidationError(
            [ValidationIssue("docker.executable", "Docker CLI is not installed or not in PATH")]
        )
    try:
        version = subprocess.run(
            [config.docker_executable, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DSLValidationError(
            [ValidationIssue("docker.daemon", "Docker daemon is not reachable")]
        ) from exc
    if version.returncode != 0 or not version.stdout.strip():
        raise DSLValidationError(
            [ValidationIssue("docker.daemon", "Docker daemon is not reachable")]
        )
    try:
        security = subprocess.run(
            [
                config.docker_executable,
                "info",
                "--format",
                "{{.OSType}}|{{json .SecurityOptions}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DSLValidationError(
            [ValidationIssue("docker.security", "could not inspect Docker security options")]
        ) from exc
    security_output = security.stdout.strip().lower()
    if security.returncode != 0 or not security_output.startswith("linux|"):
        raise DSLValidationError(
            [ValidationIssue("docker.platform", "Docker daemon must use Linux containers")]
        )
    if "seccomp" not in security_output:
        raise DSLValidationError(
            [ValidationIssue("docker.security", "Docker seccomp support must be active")]
        )
    try:
        inspect = subprocess.run(
            [config.docker_executable, "image", "inspect", config.image],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DSLValidationError(
            [ValidationIssue("docker.image", "could not inspect pinned image")]
        ) from exc
    if inspect.returncode != 0:
        raise DSLValidationError(
            [
                ValidationIssue(
                    "docker.image",
                    "pinned image is not available locally; pull it explicitly before execution",
                )
            ]
        )


def _stop_worker_process(
    process: subprocess.Popen[str],
    backend: BoundaryBackend,
    docker_config: DockerBoundaryConfig | None,
    container_name: str | None,
) -> None:
    if process.poll() is not None:
        return
    if backend is BoundaryBackend.docker:
        if docker_config is None or container_name is None:
            process.kill()
            process.wait(timeout=2)
            return
        try:
            subprocess.run(
                [docker_config.docker_executable, "kill", container_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _reader(stream, messages: queue.Queue[dict[str, Any]]) -> None:
    try:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = {"type": "protocol_error", "reason": "worker emitted invalid JSON"}
            messages.put(value if isinstance(value, dict) else {"type": "protocol_error"})
    finally:
        messages.put({"type": "eof"})


def _drain_stderr(stream, lines: list[str]) -> None:
    for line in stream:
        cleaned = line.strip()
        if cleaned:
            lines.append(cleaned[:1000])
            if len(lines) > 20:
                del lines[0]


def _tool_response(name: str, attributes: Conditions) -> dict[str, Any]:
    if name == "policy_lookup":
        return {"policy_found": True, "policy_id": attributes.get("policy_id", "policy_001")}
    if name == "claim_history":
        return {"claim_count": 2, "claim_id": attributes.get("claim_id", "claim_001")}
    if name == "create_adjuster_handoff":
        return {"handoff_created": True, "queue": "licensed_claims_adjusters"}
    raise ValueError(f"no proxy implementation for allowlisted tool {name!r}")


def _unsigned_report_payload(report: BoundaryReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"signature", "report_id"})


def run_execution_boundary(
    document: ContractDocument,
    plan: BoundaryExecutionPlan,
    private_key_path: str | Path,
    *,
    timeout_seconds: float = 10.0,
    backend: BoundaryBackend = BoundaryBackend.python_process,
    docker_config: DockerBoundaryConfig | None = None,
    payment_authorization_public_key_path: str | Path | None = None,
) -> BoundaryReport:
    """Run a capability-only worker and enforce the contract outside it."""

    if document.operational_integrity is None:
        raise DSLValidationError(
            [ValidationIssue("contract.operational_integrity", "is required")]
        )
    issues: list[ValidationIssue] = []
    if plan.contract_id != document.contract.id:
        issues.append(ValidationIssue("plan.contract_id", "does not match contract"))
    if plan.contract_version != document.contract.version:
        issues.append(ValidationIssue("plan.contract_version", "does not match contract"))
    if (
        plan.stop_after_sequence is not None
        and plan.stop_condition not in document.operational_integrity.stop_conditions
    ):
        issues.append(
            ValidationIssue(
                "plan.stop_condition",
                "is not declared in operational_integrity.stop_conditions",
            )
        )
    if issues:
        raise DSLValidationError(issues)
    if backend is BoundaryBackend.docker:
        if docker_config is None:
            raise DSLValidationError(
                [ValidationIssue("docker", "configuration is required for Docker backend")]
            )
        _validate_docker_runtime(docker_config)
        boundary_name = DOCKER_BOUNDARY_NAME
        limitation = (
            "The Docker backend enforces deny-by-default networking, a read-only root "
            "filesystem, dropped capabilities, no-new-privileges, PID/memory/CPU/file "
            "descriptor limits, a non-root user, and a constrained tmpfs. The host kernel, "
            "Docker daemon, and pinned image remain trusted."
        )
    else:
        if docker_config is not None:
            raise DSLValidationError(
                [
                    ValidationIssue(
                        "docker",
                        "configuration may only be supplied for Docker backend",
                    )
                ]
            )
        boundary_name = BOUNDARY_NAME
        limitation = (
            "The Python subprocess backend enforces the proxy protocol and process "
            "termination, but it is not an OS container and cannot prevent direct "
            "filesystem or network syscalls from arbitrary untrusted code."
        )

    private_key = _load_private_key(private_key_path)
    public_key = private_key.public_key()
    policy = document.operational_integrity
    started_at = parse_timestamp(plan.started_at)
    generated_at = (
        started_at + timedelta(seconds=max(item.offset_seconds for item in plan.requests) + 1)
    ).isoformat()
    events: list[BoundaryEvent] = []
    decisions: list[BoundaryDecision] = []
    observations: list[OperationalObservation] = []
    executed_tools = 0
    consumed_payment_authorization_ids: set[str] = set()
    consumed_payment_operation_ids: set[str] = set()
    stopped = plan.stop_after_sequence == 0
    terminated = False
    termination_reason: str | None = None
    protocol_failed = False

    worker_path = WORKER_SOURCE
    container_name: str | None = None
    with tempfile.TemporaryDirectory(
        prefix="autopilot-boundary-", ignore_cleanup_errors=True
    ) as child_cwd:
        if backend is BoundaryBackend.docker:
            assert docker_config is not None
            container_name = _container_name(plan.execution_id)
            command = build_docker_command(docker_config, worker_path, container_name)
            child_environment = None
        else:
            command = [sys.executable, "-I", "-u", str(worker_path)]
            child_environment = _safe_child_environment()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=child_cwd,
                env=child_environment,
                close_fds=True,
            )
        except OSError as exc:
            raise DSLValidationError(
                [ValidationIssue("boundary.backend", f"could not start worker: {exc}")]
            ) from exc
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise RuntimeError("failed to create worker protocol pipes")

        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        reader = threading.Thread(target=_reader, args=(process.stdout, inbox), daemon=True)
        reader.start()
        stderr_lines: list[str] = []
        if process.stderr is not None:
            stderr_reader = threading.Thread(
                target=_drain_stderr,
                args=(process.stderr, stderr_lines),
                daemon=True,
            )
            stderr_reader.start()
        _append_event(
            events,
            BoundaryEventType.worker_started,
            plan.started_at,
            {
                "backend": boundary_name,
                "isolation": (
                    {
                        "image": docker_config.image,
                        "network": "none",
                        "root_filesystem": "read_only",
                        "capabilities": "none",
                        "no_new_privileges": True,
                        "seccomp": "docker_default",
                        "non_root_user": docker_config.user,
                        "memory_mb": docker_config.memory_mb,
                        "cpus": docker_config.cpus,
                        "pids_limit": docker_config.pids_limit,
                    }
                    if docker_config is not None
                    else {"process_only": True}
                ),
            },
        )
        if stopped:
            _append_event(
                events,
                BoundaryEventType.stop_issued,
                plan.started_at,
                {"after_sequence": 0},
            )

        start_message = {
            "type": "start",
            "worker_mode": plan.worker_mode.value,
            "requests": [item.model_dump(mode="json") for item in plan.requests],
        }
        process.stdin.write(json.dumps(start_message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout_seconds

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                message = inbox.get(timeout=remaining)
                message_type = message.get("type")
                if message_type == "request":
                    sequence = message.get("sequence")
                    if type(sequence) is not int or not (1 <= sequence <= len(plan.requests)):
                        raise ValueError("worker emitted invalid request sequence")
                    expected = plan.requests[sequence - 1]
                    if message.get("request") != expected.model_dump(mode="json"):
                        raise ValueError("worker request does not match signed execution plan")
                    occurred_at = (
                        started_at + timedelta(seconds=expected.offset_seconds)
                    ).isoformat()

                    permitted = True
                    reason = "request executed by supervisor proxy"
                    response: dict[str, Any] = {}
                    if stopped:
                        permitted = False
                        reason = "request blocked after external stop condition"
                        terminated = True
                        termination_reason = "continued_after_stop"
                    elif expected.kind is BoundaryRequestKind.tool_call:
                        if expected.name not in policy.allowed_tools:
                            permitted = False
                            reason = "tool is outside the contract allowlist"
                        elif executed_tools >= policy.budgets.max_tool_calls:
                            permitted = False
                            reason = "tool-call budget exhausted"
                        else:
                            try:
                                response = _tool_response(expected.name, expected.attributes)
                            except ValueError as exc:
                                permitted = False
                                reason = str(exc)
                            if permitted:
                                executed_tools += 1
                    else:
                        amount = expected.attributes.get("amount_eur")
                        if type(amount) not in {int, float}:
                            permitted = False
                            reason = "payment amount is missing or invalid"
                        elif not Decimal(str(amount)).is_finite() or Decimal(str(amount)) <= 0:
                            permitted = False
                            reason = "payment amount is missing or invalid"
                        else:
                            reason = _payment_authorization_problem(
                                expected,
                                plan,
                                payment_authorization_public_key_path,
                                consumed_payment_authorization_ids,
                                consumed_payment_operation_ids,
                            ) or reason
                            permitted = reason == "request executed by supervisor proxy"
                        if permitted and float(amount) > policy.budgets.max_claim_payment_eur:
                            permitted = False
                            reason = "payment exceeds contract budget"
                        if permitted:
                            response = {
                                "payment_created": True,
                                "amount_eur": amount,
                                "operation_id": expected.attributes.get("operation_id"),
                            }

                    decision = BoundaryDecision(
                        sequence=sequence,
                        request_ref=expected.ref,
                        permitted=permitted,
                        executed=permitted,
                        reason=reason,
                        response=response,
                    )
                    decisions.append(decision)
                    if permitted:
                        observation_kind = (
                            OperationalObservationKind.tool_call
                            if expected.kind is BoundaryRequestKind.tool_call
                            else OperationalObservationKind.monetary_action
                        )
                        attributes = dict(expected.attributes)
                        attributes["executed"] = True
                        observations.append(
                            OperationalObservation(
                                ref=expected.ref,
                                kind=observation_kind,
                                name=expected.name,
                                observed_at=occurred_at,
                                source=ObservationSource.environment,
                                attributes=attributes,
                            )
                        )
                        _append_event(
                            events,
                            BoundaryEventType.request_executed,
                            occurred_at,
                            {"request_ref": expected.ref, "name": expected.name},
                        )
                    else:
                        _append_event(
                            events,
                            BoundaryEventType.request_blocked,
                            occurred_at,
                            {
                                "request_ref": expected.ref,
                                "name": expected.name,
                                "reason": reason,
                            },
                        )

                    stop_now = plan.stop_after_sequence == sequence
                    if stop_now:
                        stopped = True
                        stop_at = (
                            started_at
                            + timedelta(seconds=expected.offset_seconds, milliseconds=1)
                        ).isoformat()
                        observations.append(
                            OperationalObservation(
                                ref=f"stop-after-{sequence}",
                                kind=OperationalObservationKind.control_event,
                                name=plan.stop_condition,
                                observed_at=stop_at,
                                source=ObservationSource.environment,
                                attributes={},
                            )
                        )
                        _append_event(
                            events,
                            BoundaryEventType.stop_issued,
                            stop_at,
                            {"after_sequence": sequence},
                        )

                    if terminated:
                        _stop_worker_process(
                            process,
                            backend,
                            docker_config,
                            container_name,
                        )
                        _append_event(
                            events,
                            BoundaryEventType.worker_terminated,
                            occurred_at,
                            {"reason": termination_reason},
                        )
                        break

                    process.stdin.write(
                        json.dumps(
                            {
                                "type": "decision",
                                "sequence": sequence,
                                "permitted": permitted,
                                "response": response,
                                "stop": stop_now,
                                "terminate": False,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    process.stdin.flush()
                elif message_type == "complete":
                    process.wait(timeout=2)
                    _append_event(
                        events,
                        BoundaryEventType.worker_completed,
                        generated_at,
                        {"reason": str(message.get("reason", "unknown"))},
                    )
                    break
                elif message_type == "eof":
                    if process.poll() not in {0, None}:
                        detail = f": {stderr_lines[-1]}" if stderr_lines else ""
                        raise ValueError("worker exited before protocol completion" + detail)
                    break
                else:
                    raise ValueError(str(message.get("reason", "invalid worker protocol message")))
        except (queue.Empty, ValueError, subprocess.TimeoutExpired) as exc:
            protocol_failed = True
            termination_reason = "protocol_failure"
            if process.poll() is None:
                _stop_worker_process(process, backend, docker_config, container_name)
            _append_event(
                events,
                BoundaryEventType.protocol_error,
                generated_at,
                {"reason": str(exc) or "worker timeout"},
            )
        finally:
            if process.poll() is None:
                _stop_worker_process(process, backend, docker_config, container_name)

    operational_trace = OperationalTrace(
        trace_id=f"boundary-{plan.execution_id}",
        contract_id=plan.contract_id,
        contract_version=plan.contract_version,
        candidate_version=plan.candidate_version,
        evaluated_at=generated_at,
        complete=not protocol_failed,
        evidence_complete=not protocol_failed,
        observer="execution_boundary",
        observations=observations,
        x_event_chain_head=events[-1].event_hash,
    )
    operational_evaluation = evaluate_operational_integrity(document, operational_trace)
    status = (
        BoundaryStatus.failed
        if protocol_failed or operational_evaluation.status.value not in {"pass", "not_applicable"}
        else BoundaryStatus.passed
    )
    placeholder = BoundarySignature(
        key_id=_key_id(public_key),
        signed_payload_sha256="0" * 64,
        value_base64="",
    )
    report = BoundaryReport(
        report_id="BOUNDARY-" + "0" * 24,
        execution_id=plan.execution_id,
        contract_id=plan.contract_id,
        contract_version=plan.contract_version,
        candidate_version=plan.candidate_version,
        generated_at=generated_at,
        boundary_name=boundary_name,
        plan_sha256=_sha256(plan.model_dump(mode="json")),
        worker_sha256=hashlib.sha256(worker_path.read_bytes()).hexdigest(),
        boundary_implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        status=status,
        terminated=terminated,
        termination_reason=termination_reason,
        executed_request_count=sum(item.executed for item in decisions),
        blocked_request_count=sum(not item.permitted for item in decisions),
        decisions=decisions,
        events=events,
        chain_head=events[-1].event_hash,
        operational_trace=operational_trace,
        operational_evaluation=operational_evaluation,
        signature=placeholder,
        limitation=limitation,
    )
    unsigned_payload = _unsigned_report_payload(report)
    payload_bytes = _canonical_bytes(unsigned_payload)
    payload_digest = hashlib.sha256(payload_bytes).hexdigest()
    report.report_id = f"BOUNDARY-{payload_digest[:24].upper()}"
    report.signature = BoundarySignature(
        key_id=_key_id(public_key),
        signed_payload_sha256=payload_digest,
        value_base64=base64.b64encode(private_key.sign(payload_bytes)).decode("ascii"),
    )
    return report


def verify_boundary_report(
    report: BoundaryReport,
    public_key_path: str | Path,
) -> BoundaryVerification:
    public_key = _load_public_key(public_key_path)
    problems: list[str] = []
    previous_hash = "0" * 64
    event_chain_valid = True
    for expected_sequence, event in enumerate(report.events, start=1):
        expected_hash = _event_hash(
            expected_sequence,
            event.event_type,
            event.occurred_at,
            event.payload,
            previous_hash,
        )
        if (
            event.sequence != expected_sequence
            or event.previous_hash != previous_hash
            or event.event_hash != expected_hash
        ):
            event_chain_valid = False
        previous_hash = event.event_hash
    if not event_chain_valid:
        problems.append("event hash chain is invalid")

    chain_head_valid = bool(report.events) and report.chain_head == report.events[-1].event_hash
    if not chain_head_valid:
        problems.append("chain head does not match the last event")

    payload = _unsigned_report_payload(report)
    payload_bytes = _canonical_bytes(payload)
    digest = hashlib.sha256(payload_bytes).hexdigest()
    payload_digest_valid = digest == report.signature.signed_payload_sha256
    if not payload_digest_valid:
        problems.append("signed payload digest is invalid")
    report_id_valid = report.report_id == f"BOUNDARY-{digest[:24].upper()}"
    if not report_id_valid:
        problems.append("report id is invalid")
    key_id_valid = report.signature.key_id == _key_id(public_key)
    if not key_id_valid:
        problems.append("signing key id does not match the public key")
    try:
        signature = base64.b64decode(report.signature.value_base64, validate=True)
        public_key.verify(signature, payload_bytes)
        signature_valid = True
    except (ValueError, InvalidSignature):
        signature_valid = False
        problems.append("Ed25519 signature is invalid")

    semantic_problems: list[str] = []
    if report.operational_trace.model_extra.get("x_event_chain_head") != report.chain_head:
        semantic_problems.append("operational trace is not bound to the event-chain head")
    if report.executed_request_count != sum(item.executed for item in report.decisions):
        semantic_problems.append("executed-request count does not match decisions")
    if report.blocked_request_count != sum(not item.permitted for item in report.decisions):
        semantic_problems.append("blocked-request count does not match decisions")
    if any(item.executed != item.permitted for item in report.decisions):
        semantic_problems.append("a decision has inconsistent permitted/executed state")
    if report.terminated and not any(
        event.event_type is BoundaryEventType.worker_terminated for event in report.events
    ):
        semantic_problems.append("terminated report lacks worker_terminated event")
    semantics_valid = not semantic_problems
    problems.extend(semantic_problems)

    return BoundaryVerification(
        valid=not problems,
        report_id_valid=report_id_valid,
        event_chain_valid=event_chain_valid,
        chain_head_valid=chain_head_valid,
        payload_digest_valid=payload_digest_valid,
        signature_valid=signature_valid,
        key_id_valid=key_id_valid,
        semantics_valid=semantics_valid,
        problems=problems,
    )
