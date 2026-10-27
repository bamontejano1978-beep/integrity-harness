"""Swarm evaluation M1: paired agents over a mediated channel (swarm report v0.1).

Implements the M1 prototype from ``docs/product/evaluacion_enjambres_20260918_v0.1.md``:
two executable agents share one mediated, hash-chained channel. The claimant's turns
reach both members; each member's reply becomes shared channel traffic and, while the
delegation budget lasts, is absorbed into the other member's raw session before
re-evaluation with the unchanged contract engine. Contagion is the headline artifact:
an agent that passes in isolation but fails when a non-compliant peer answers in the
same mediated conversation.

Attribution and evidence:

- Injected traffic keeps ``swarm-peer-*`` / ``swarm-subject-*`` refs, so every clause
  evidence ref names the conversation item and the channel log resolves the actor.
- Prohibition state (e.g. ``licensed_adjuster_approval``) is conversation-wide in a
  mediated exchange and therefore travels on the injected message metadata; injected
  content is classified by the same deterministic adapter rules as the member's own
  traffic.
- The channel is hash-chained (payload digest + previous-entry digest) for tamper
  evidence, and it keeps recording after the delegation budget is exhausted.

M1 semantics notes: member replies are taken from their isolated sessions (deterministic
replay, no extra provider calls), cross-visibility is symmetric, so clause satisfaction
can be shared across members (a compliant member's explanation also satisfies the other
member's conditional-response clause). Contagion only counts *new* clause failures
relative to the member's isolated baseline, which stays sound under this semantics.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    model_serializer,
    model_validator,
)

from autopilot.adapter import AdapterConfig, MessageRole, RawMessage, adapt_session
from autopilot.agent_cohort import _canonical_hash
from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import ContractDocument, ExtensibleModel, SnakeName
from autopilot.evaluator import ClauseStatus, evaluate_contract
from autopilot.insurance_agent import (
    AgentEvaluationBundle,
    InsuranceAgentConfig,
    load_agent_config,
    load_agent_scenario,
    run_agent_evaluation,
)
from autopilot.trajectory import parse_timestamp

SWARM_RUNNER_VERSION = "0.2.0"

PEER_ATTR = "peer_id"
TURN_ATTR = "turn_index"
ROUND_ATTR = "swarm_round"
MEDIATED_ATTR = "swarm_mediated"
MEDIATED_VALUE = True
#: P3 (M2): the entry that carried a member's traffic into the mediated channel.
#: Present on the payload of every channel message and on injected-traffic
#: metadata, so clause evidence can be resolved back to its channel entry.
CHANNEL_SEQ_ATTR = "swarm_channel_seq"
#: P3 (M2): declarative data labels on the payload content, classified by the
#: same deterministic adapter rules that classify the member's own traffic.
#: Labels travel, never content: the invariant layer checks label-vs-context.
DATA_LABELS_ATTR = "data_labels"


class SwarmAgentRole(str, Enum):
    subject = "subject"
    peer = "peer"


class SwarmAgentSpec(ExtensibleModel):
    source_id: SnakeName
    role: SwarmAgentRole
    config_path: Annotated[str, Field(min_length=1, max_length=500)]


class SwarmPairConfig(ExtensibleModel):
    swarm_version: Literal["0.1"]
    swarm_id: Annotated[str, Field(min_length=1, max_length=255)]
    subject: SwarmAgentSpec
    peer: SwarmAgentSpec
    scenario: Annotated[str, Field(min_length=1, max_length=500)]
    max_rounds: Annotated[int, Field(ge=1, le=8, strict=True)] = 2
    max_delegation_rounds: Annotated[int, Field(ge=0, le=8, strict=True)] = 1

    @model_validator(mode="after")
    def _validate_pairing(self) -> SwarmPairConfig:
        if self.subject.source_id == self.peer.source_id:
            raise ValueError("subject and peer source ids must differ")
        if self.max_delegation_rounds > self.max_rounds:
            raise ValueError("max_delegation_rounds cannot exceed max_rounds")
        return self


class SwarmChannelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: Annotated[int, Field(ge=1, strict=True)]
    round: Annotated[int, Field(ge=1, strict=True)]
    #: The mediated channel is the swarm's shared primitive, so N-agent social
    #: experiments widen this vocabulary additively (``autopilot.bystander``,
    #: ``autopilot.asch``, ``autopilot.cascade``) without changing any pair-run
    #: artefact: ``incident``/``bystander_observation`` carry the witnessed event
    #: and a non-acting bystander; ``task_prompt``/``majority_answer``/
    #: ``subject_answer`` carry the objective question, the majority's answers
    #: and the subject's; ``cascade_decision`` carries one agent's decision in a
    #: sequential cascade, where every later agent reads the earlier ones.
    kind: Literal[
        "peer_message",
        "subject_message",
        "delegation_open",
        "delegation_close",
        "incident",
        "bystander_observation",
        "task_prompt",
        "majority_answer",
        "subject_answer",
        "cascade_decision",
    ]
    source_agent: SnakeName
    target_agent: SnakeName
    payload: dict
    payload_sha256: str
    prev_entry_sha256: str | None = None
    entry_sha256: str


class SwarmPairRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: list[SwarmChannelEntry]
    rounds_completed: int
    delegation_rounds_used: int


class SwarmMemberEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    role: SwarmAgentRole
    agent_id: str
    candidate_version: str
    candidate_digest: str
    isolated_status: str
    paired_status: str
    isolated_failed_clause_ids: list[str]
    paired_failed_clause_ids: list[str]
    contagion_clause_ids: list[str] = Field(default_factory=list)
    contagion_index: float = Field(ge=0.0, le=1.0)
    contagion_clause_count: int = Field(ge=0)


class SwarmReportSignature(BaseModel):
    """Ed25519 signature over the report and digests of its evidence files."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]
    signed_payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    value_base64: str
    signed_at: str


class SwarmPairReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: v0.2 adds ``contagion_clause_count`` and the fixed contagion-index
    #: semantics; v0.1 artefacts stay valid as historical records.
    schema_version: Literal["0.1", "0.2"] = "0.2"
    swarm_id: str
    scenario_id: str
    report_id: str
    channel_entries: int
    rounds_completed: int
    delegation_rounds_used: int
    members: list[SwarmMemberEvaluation]
    contagion_detected: StrictBool
    contagion_clause_ids: list[str] = Field(default_factory=list)
    runner_version: str = SWARM_RUNNER_VERSION
    swarm_contract_id: str | None = None
    contract_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    system_verdict: dict[str, Any] | None = None
    channel_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    channel_head_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    member_evaluation_sha256: dict[str, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] | None = None
    signature: SwarmReportSignature | None = None

    @model_serializer(mode="wrap")
    def omit_unsigned_evidence_fields(self, handler):
        """Keep unsigned M1 report bytes/schema unchanged when fields are absent."""

        payload = handler(self)
        for name in (
            "swarm_contract_id",
            "contract_sha256",
            "system_verdict",
            "channel_sha256",
            "channel_head_sha256",
            "member_evaluation_sha256",
            "signature",
        ):
            if getattr(self, name) is None:
                payload.pop(name, None)
        return payload


class SwarmPairExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: SwarmPairReport
    run: SwarmPairRun
    subject_isolated: AgentEvaluationBundle
    peer_isolated: AgentEvaluationBundle
    subject_paired: AgentEvaluationBundle
    peer_paired: AgentEvaluationBundle


#: P3 (M2): deterministic label rules over reply content. Labels travel on the
#: channel payload, never content. Deliberately minimal for v0.2 - the same
#: vocabulary the insurance vertical's own classification uses ("pago") - and
#: the invariant layer is only as good as this labelling: a limit the M2 plan
#: declares (§4) rather than hides. Extending the rules is additive.
DATA_LABEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pago", ("approve", "payout", "payment", "coverage", "refund", "charge")),
    ("reclamacion", ("claim", "reclamaci")),
)


def _data_labels_for(content: str) -> list[str]:
    """Declarative data labels for one message's content (deterministic)."""

    lowered = content.lower()
    return [
        label for label, words in DATA_LABEL_RULES if any(word in lowered for word in words)
    ]


def _canonical_json_bytes(value: Any) -> bytes:
    """Canonical UTF-8 JSON bytes for signature and evidence digests."""

    import json

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_swarm_private_key(path: str | Path) -> Ed25519PrivateKey:

    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"could not load unencrypted Ed25519 private key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("swarm signing key must be Ed25519")
    return key


def _load_swarm_public_key(path: str | Path) -> Ed25519PublicKey:

    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"could not load Ed25519 public key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("swarm verification key must be Ed25519")
    return key


def generate_swarm_keypair(
    private_key_path: str | Path, public_key_path: str | Path
) -> str:
    """Create a signing key pair without replacing existing key material."""

    private_path = Path(private_key_path)
    public_path = Path(public_key_path)
    if private_path.resolve() == public_path.resolve():
        raise ValueError("private and public key paths must differ")
    if private_path.exists() or public_path.exists():
        raise FileExistsError("swarm key generation never overwrites existing keys")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
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
    return _public_key_id(public_key)


def _member_evaluation_digests(
    member_paths: dict[str, str | Path] | None,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for source_id, path in sorted((member_paths or {}).items()):
        document = Path(path).read_bytes()
        digests[source_id] = hashlib.sha256(document).hexdigest()
    return digests


def _report_signing_payload(report: SwarmPairReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"signature"})


def sign_swarm_report(
    report: SwarmPairReport,
    *,
    channel: SwarmPairRun,
    member_evaluation_paths: dict[str, str | Path] | None,
    private_key_path: str | Path,
    public_key_path: str | Path,
    signed_at: str | None = None,
) -> SwarmPairReport:
    """Bind report, full channel and paired-evaluation files under Ed25519."""

    private_key = _load_swarm_private_key(private_key_path)
    public_key = _load_swarm_public_key(public_key_path)
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) != public_bytes:
        raise ValueError("swarm private and public keys do not match")
    if report.channel_entries != len(channel.channel):
        raise ValueError("report channel_entries does not match the supplied channel")
    channel_document = [
        entry.model_dump(mode="json", exclude_none=True) for entry in channel.channel
    ]
    channel_sha256 = hashlib.sha256(_canonical_json_bytes(channel_document)).hexdigest()
    channel_head = channel.channel[-1].entry_sha256 if channel.channel else "0" * 64
    member_digests = _member_evaluation_digests(member_evaluation_paths)
    signed_at_value = signed_at or datetime.now(timezone.utc).isoformat()
    try:
        moment = datetime.fromisoformat(signed_at_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("signed_at must be an RFC 3339 timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError("signed_at must include a timezone")
    unsigned = report.model_copy(
        update={
            "channel_sha256": channel_sha256,
            "channel_head_sha256": channel_head,
            "member_evaluation_sha256": member_digests,
            "signature": None,
        }
    )
    payload_bytes = _canonical_json_bytes(_report_signing_payload(unsigned))
    signature = SwarmReportSignature(
        key_id=_public_key_id(public_key),
        signed_payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        value_base64=base64.b64encode(private_key.sign(payload_bytes)).decode("ascii"),
        signed_at=moment.isoformat(),
    )
    return unsigned.model_copy(update={"signature": signature})


class SwarmReportVerification(BaseModel):
    """Detailed report, key, channel and member-artifact verification state."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    report_id_valid: bool
    payload_digest_valid: bool
    signature_valid: bool
    key_id_valid: bool
    channel_digest_valid: bool
    channel_head_valid: bool
    channel_chain_valid: bool
    member_evaluations_valid: bool
    contract_valid: bool
    problems: list[str]


def verify_swarm_report(
    report: SwarmPairReport,
    *,
    channel: SwarmPairRun,
    public_key_path: str | Path,
    member_evaluation_paths: dict[str, str | Path] | None = None,
    swarm_contract_path: str | Path | None = None,
) -> SwarmReportVerification:
    """Verify report signature plus every bound channel/member artifact."""

    problems: list[str] = []
    signature = report.signature
    if signature is None:
        return SwarmReportVerification(
            valid=False,
            report_id_valid=False,
            payload_digest_valid=False,
            signature_valid=False,
            key_id_valid=False,
            channel_digest_valid=False,
            channel_head_valid=False,
            channel_chain_valid=False,
            member_evaluations_valid=False,
            contract_valid=False,
            problems=["swarm report is unsigned"],
        )
    key = _load_swarm_public_key(public_key_path)
    key_id_valid = signature.key_id == _public_key_id(key)
    if not key_id_valid:
        problems.append("signing key does not match the configured verification key")
    payload_bytes = _canonical_json_bytes(_report_signing_payload(report))
    digest_valid = hashlib.sha256(payload_bytes).hexdigest() == signature.signed_payload_sha256
    if not digest_valid:
        problems.append("signed report payload digest does not match")
    signature_valid = False
    try:
        signature_bytes = base64.b64decode(signature.value_base64, validate=True)
        key.verify(signature_bytes, payload_bytes)
        signature_valid = True
    except (ValueError, InvalidSignature):
        problems.append("Ed25519 report signature is invalid")

    report_id_valid = report.report_id == "SWARM-" + hashlib.sha256(
        _canonical_json_bytes(
            {
                "swarm_id": report.swarm_id,
                "scenario_id": report.scenario_id,
                "channel": [
                    entry.model_dump(mode="json", exclude_none=True)
                    for entry in channel.channel
                ],
                "members": [member.model_dump(mode="json") for member in report.members],
                "runner_version": report.runner_version,
            }
        )
    ).hexdigest()[:24].upper()
    if not report_id_valid:
        problems.append("report_id does not match report, channel and member evidence")
    channel_document = [
        entry.model_dump(mode="json", exclude_none=True) for entry in channel.channel
    ]
    channel_digest_valid = (
        report.channel_sha256
        == hashlib.sha256(_canonical_json_bytes(channel_document)).hexdigest()
    )
    if not channel_digest_valid:
        problems.append("channel digest does not match the report")
    expected_head = channel.channel[-1].entry_sha256 if channel.channel else "0" * 64
    channel_head_valid = (
        report.channel_entries == len(channel.channel)
        and report.channel_head_sha256 == expected_head
        and channel.rounds_completed == report.rounds_completed
        and channel.delegation_rounds_used == report.delegation_rounds_used
    )
    if not channel_head_valid:
        problems.append("channel entry count or chain head does not match the report")
    channel_problems = verify_swarm_channel(channel)
    channel_chain_valid = not channel_problems
    problems.extend(channel_problems)

    expected_member_digests = _member_evaluation_digests(member_evaluation_paths)
    member_evaluations_valid = report.member_evaluation_sha256 == expected_member_digests
    if not member_evaluations_valid:
        problems.append("member evaluation artifact digests do not match the report")
    if swarm_contract_path is None:
        contract_valid = report.swarm_contract_id is None and report.contract_sha256 is None
        if not contract_valid:
            problems.append("report binds a swarm contract but none was supplied for verification")
    else:
        from autopilot.swarm_contract import load_swarm_contract

        contract = load_swarm_contract(swarm_contract_path)
        # v0.2 reports were produced across a serialization change: older
        # artifacts omitted unset/None defaults, while the current serializer
        # includes them. Accept canonical current and historical forms without
        # mutating the signed report payload to achieve a match.
        contract_digests = {
            hashlib.sha256(
                _canonical_json_bytes(contract.model_dump(mode="json"))
            ).hexdigest(),
            hashlib.sha256(
                _canonical_json_bytes(contract.model_dump(mode="json", exclude_none=True))
            ).hexdigest(),
            hashlib.sha256(
                _canonical_json_bytes(contract.model_dump(mode="json", exclude_unset=True))
            ).hexdigest(),
        }
        contract_valid = (
            report.swarm_contract_id == contract.swarm_contract_id
            and report.contract_sha256 in contract_digests
        )
        if not contract_valid:
            problems.append("swarm contract identity or digest does not match the report")
    return SwarmReportVerification(
        valid=(
            report_id_valid
            and digest_valid
            and signature_valid
            and key_id_valid
            and channel_digest_valid
            and channel_head_valid
            and channel_chain_valid
            and member_evaluations_valid
            and contract_valid
        ),
        report_id_valid=report_id_valid,
        payload_digest_valid=digest_valid,
        signature_valid=signature_valid,
        key_id_valid=key_id_valid,
        channel_digest_valid=channel_digest_valid,
        channel_head_valid=channel_head_valid,
        channel_chain_valid=channel_chain_valid,
        member_evaluations_valid=member_evaluations_valid,
        contract_valid=contract_valid,
        problems=problems,
    )


def verify_swarm_channel(channel: SwarmPairRun) -> list[str]:
    """Recompute payload digests and the hash chain for every channel entry."""

    problems: list[str] = []
    previous: str | None = None
    for expected_seq, entry in enumerate(channel.channel, start=1):
        if entry.seq != expected_seq:
            problems.append(f"channel sequence discontinuity at entry {expected_seq}")
        if entry.prev_entry_sha256 != previous:
            problems.append(f"channel previous hash mismatch at entry {entry.seq}")
        payload_digest = _canonical_hash(entry.payload)
        if entry.payload_sha256 != payload_digest:
            problems.append(f"channel payload digest mismatch at entry {entry.seq}")
        expected_hash = _canonical_hash(
            {
                "seq": entry.seq,
                "round": entry.round,
                "kind": entry.kind,
                "source_agent": entry.source_agent,
                "target_agent": entry.target_agent,
                "payload_sha256": payload_digest,
                "prev_entry_sha256": previous,
            }
        )
        if entry.entry_sha256 != expected_hash:
            problems.append(f"channel entry digest mismatch at entry {entry.seq}")
        previous = entry.entry_sha256
    return problems


def load_swarm_report(path: str | Path) -> SwarmPairReport:
    """Load a JSON swarm report, rejecting malformed envelopes."""

    try:
        return SwarmPairReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"invalid swarm report {path}: {exc}") from exc


def load_swarm_channel(path: str | Path) -> SwarmPairRun:
    """Load the channel JSON artifact written by ``swarm-evaluate``."""

    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("channel JSON must be a list of entries")
        parsed = [SwarmChannelEntry.model_validate(item) for item in entries]
        return SwarmPairRun(
            channel=parsed,
            rounds_completed=max((entry.round for entry in parsed), default=0),
            delegation_rounds_used=sum(entry.kind == "delegation_open" for entry in parsed),
        )
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"invalid swarm channel {path}: {exc}") from exc


def load_swarm_member_evaluation_paths(
    directory: str | Path, report: SwarmPairReport
) -> dict[str, Path]:
    """Resolve each member evaluation file to its declared source ID."""

    root = Path(directory)
    if len(report.members) != 2:
        raise ValueError("swarm pair report must contain exactly two members")
    return {
        member.source_id: root / prefix / "evaluation.json"
        for prefix, member in zip(("subject", "peer"), report.members, strict=True)
    }


def _load_text_file(path: str | Path) -> tuple[str, Path]:
    resolved = Path(path)
    try:
        return resolved.read_text(encoding="utf-8"), resolved
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc


def load_swarm_pair(path: str | Path) -> tuple[SwarmPairConfig, Path]:
    """Load a swarm pair configuration; the returned base dir holds referenced files."""

    text, resolved = _load_text_file(path)
    try:
        return SwarmPairConfig.model_validate(load_yaml_mapping(text)), resolved.parent
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def _after(anchor_at: str, round_no: int, turn_index: int) -> str:
    shifted = parse_timestamp(anchor_at) + timedelta(seconds=round_no * 7 + turn_index)
    return shifted.isoformat().replace("+00:00", "Z")


def _failed_clauses(bundle: AgentEvaluationBundle) -> list[str]:
    return sorted(
        result.clause_id
        for result in bundle.evaluation.clause_results
        if result.status is ClauseStatus.failed
    )


def _member_evaluation(
    source_id: str,
    spec: SwarmAgentSpec,
    config: InsuranceAgentConfig,
    isolated: AgentEvaluationBundle,
    paired: AgentEvaluationBundle,
) -> SwarmMemberEvaluation:
    isolated_failed = _failed_clauses(isolated)
    paired_failed = _failed_clauses(paired)
    contagion = sorted(set(paired_failed) - set(isolated_failed))
    return SwarmMemberEvaluation(
        source_id=source_id,
        role=spec.role,
        agent_id=config.agent_id,
        candidate_version=config.candidate_version,
        candidate_digest=isolated.agent_run.manifest.candidate_digest,
        isolated_status=isolated.evaluation.status.value,
        paired_status=paired.evaluation.status.value,
        isolated_failed_clause_ids=isolated_failed,
        paired_failed_clause_ids=paired_failed,
        contagion_clause_ids=contagion,
        # v0.2 semantics (M2 plan P5-fix): the index is the share of the
        # member's *current* failures that are new. The old denominator (the
        # isolated baseline) was 0.0 exactly for the headline case - a clean
        # member that fails only when paired - reporting contagion_index 0.0
        # for two contagion clauses. This one stays in [0.0, 1.0] by
        # construction, so the field constraint needs no change.
        contagion_index=(len(contagion) / len(paired_failed)) if paired_failed else 0.0,
        contagion_clause_count=len(contagion),
    )


def _user_turns(session) -> dict[int, tuple[str, dict]]:
    """Extract per-turn claimant content and metadata from a raw session."""

    turns: dict[int, tuple[str, dict]] = {}
    for message in session.messages:
        if message.role is not MessageRole.user or not message.ref.startswith("turn-"):
            continue
        digits = message.ref[len("turn-") :].split("-", maxsplit=1)[0]
        if digits.isdigit():
            turns[int(digits)] = (message.content, dict(message.metadata))
    return turns


def _assistant_replies(session) -> dict[int, tuple[str, dict]]:
    """Extract per-turn member replies (content and full metadata) from a raw session."""

    replies: dict[int, tuple[str, dict]] = {}
    for message in session.messages:
        if message.role is not MessageRole.assistant or not message.ref.startswith("turn-"):
            continue
        tail = message.ref[len("turn-") :]
        if not tail.endswith("-assistant"):
            continue
        digits = tail[: -len("-assistant")]
        if digits.isdigit():
            replies[int(digits)] = (message.content, dict(message.metadata))
    return replies


def _anchor_observed_at(session, turn_index: int) -> str:
    for message in session.messages:
        if message.role is MessageRole.assistant and message.ref == f"turn-{turn_index:03d}-assistant":
            return message.observed_at
    raise DSLValidationError(
        [ValidationIssue("session", f"missing assistant reply for turn {turn_index:03d}")]
    )


def _evaluate_paired(
    contract: ContractDocument,
    adapter: AdapterConfig,
    isolated: AgentEvaluationBundle,
    *,
    injected: dict[int, list[RawMessage]],
) -> AgentEvaluationBundle:
    """Re-evaluate a member's raw session with mediated counterpart traffic added.

    Injected messages keep their ``swarm-*-`` refs and swarm metadata for evidence
    attribution; the contract engine classifies their content with the same
    deterministic adapter rules used for the member's own traffic.
    """

    session = isolated.agent_run.raw_session.model_copy(deep=True)
    for turn_index in sorted(injected):
        session.messages.extend(injected[turn_index])
    session.messages.sort(key=lambda message: message.observed_at)
    trajectory = adapt_session(contract, adapter, session)
    evaluation = evaluate_contract(contract, trajectory)
    return AgentEvaluationBundle(
        agent_run=isolated.agent_run,
        adapter_sha256=isolated.adapter_sha256,
        trajectory=trajectory,
        evaluation=evaluation,
        operational_evaluation=isolated.operational_evaluation,
    )


def run_swarm_pair(
    contract: ContractDocument,
    adapter: AdapterConfig,
    config: SwarmPairConfig,
    base_dir: Path,
) -> SwarmPairExecution:
    """Execute one mediated pair session and evaluate both members."""

    if adapter.adapter.contract_id != contract.contract.id:
        raise DSLValidationError(
            [ValidationIssue("adapter.contract_id", "does not match the contract id")]
        )

    subject_cfg = load_agent_config(base_dir / config.subject.config_path)
    peer_cfg = load_agent_config(base_dir / config.peer.config_path)
    scenario = load_agent_scenario(base_dir / config.scenario)

    isolated = {
        config.subject.source_id: run_agent_evaluation(contract, adapter, subject_cfg, scenario),
        config.peer.source_id: run_agent_evaluation(contract, adapter, peer_cfg, scenario),
    }
    subject_session = isolated[config.subject.source_id].agent_run.raw_session
    peer_session = isolated[config.peer.source_id].agent_run.raw_session

    user_turns = _user_turns(subject_session)
    if not user_turns:
        raise DSLValidationError(
            [ValidationIssue("scenario", "has no user turns for the mediated channel")]
        )
    peer_replies = _assistant_replies(peer_session)
    subject_replies = _assistant_replies(subject_session)
    missing = sorted(set(user_turns) - set(peer_replies) - set(subject_replies))
    if missing:
        raise DSLValidationError(
            [ValidationIssue("scenario", f"missing member replies for turns {missing}")]
        )

    subject_id = str(config.subject.source_id)
    peer_id = str(config.peer.source_id)
    delegation_budget = config.max_delegation_rounds
    entries: list[SwarmChannelEntry] = []
    subject_injected: dict[int, list[RawMessage]] = {}
    peer_injected: dict[int, list[RawMessage]] = {}
    rounds_completed = 0
    delegation_rounds_used = 0

    def _append(kind: str, round_no: int, source: str, target: str, payload: dict) -> None:
        # P3 (M2): every entry cites itself - the payload carries its own seq so
        # clause evidence built from this traffic resolves back to the channel.
        payload = {**payload, CHANNEL_SEQ_ATTR: len(entries) + 1}
        payload_sha256 = _canonical_hash(payload)
        prev_entry_sha256 = entries[-1].entry_sha256 if entries else None
        entry_sha256 = _canonical_hash(
            {
                "seq": len(entries) + 1,
                "round": round_no,
                "kind": kind,
                "source_agent": source,
                "target_agent": target,
                "payload_sha256": payload_sha256,
                "prev_entry_sha256": prev_entry_sha256,
            }
        )
        entries.append(
            SwarmChannelEntry(
                seq=len(entries) + 1,
                round=round_no,
                kind=kind,
                source_agent=source,
                target_agent=target,
                payload=payload,
                payload_sha256=payload_sha256,
                prev_entry_sha256=prev_entry_sha256,
                entry_sha256=entry_sha256,
            )
        )

    for round_no in range(1, config.max_rounds + 1):
        rounds_completed = round_no
        for turn_index in sorted(user_turns):
            user_text, _user_meta = user_turns[turn_index]
            peer_content, peer_meta = peer_replies[turn_index]
            subject_content, subject_meta = subject_replies[turn_index]

            # The claimant's turn reaches both members; their replies are shared
            # channel traffic, recorded whether or not the budget allows absorption.
            _append(
                "peer_message",
                round_no,
                peer_id,
                subject_id,
                {
                    "turn": turn_index,
                    "claimant_content": user_text,
                    "reply_content": peer_content,
                    "guard_intervened": bool(peer_meta.get("guard_intervened", False)),
                    "data_labels": _data_labels_for(peer_content),
                },
            )
            _append(
                "subject_message",
                round_no,
                subject_id,
                peer_id,
                {
                    "turn": turn_index,
                    "claimant_content": user_text,
                    "reply_content": subject_content,
                    "guard_intervened": bool(subject_meta.get("guard_intervened", False)),
                    "data_labels": _data_labels_for(subject_content),
                },
            )

            if delegation_budget <= 0:
                continue
            delegation_budget -= 1
            delegation_rounds_used += 1
            _append(
                "delegation_open",
                round_no,
                subject_id,
                peer_id,
                {"turn": turn_index, "round": round_no},
            )
            # The delegation envelope that authorizes the absorption below.
            delegation_seq = len(entries)
            shared_peer_meta = {
                key: value for key, value in peer_meta.items() if key != "guard_intervened"
            }
            subject_injected.setdefault(turn_index, []).append(
                RawMessage(
                    ref=f"swarm-peer-r{round_no}-turn-{turn_index:03d}-assistant",
                    role=MessageRole.assistant,
                    observed_at=_after(
                        _anchor_observed_at(subject_session, turn_index), round_no, turn_index
                    ),
                    content=peer_content,
                    metadata={
                        PEER_ATTR: peer_id,
                        TURN_ATTR: turn_index,
                        ROUND_ATTR: round_no,
                        MEDIATED_ATTR: MEDIATED_VALUE,
                        CHANNEL_SEQ_ATTR: delegation_seq,
                        **shared_peer_meta,
                    },
                )
            )
            shared_subject_meta = {
                key: value for key, value in subject_meta.items() if key != "guard_intervened"
            }
            peer_injected.setdefault(turn_index, []).append(
                RawMessage(
                    ref=f"swarm-subject-r{round_no}-turn-{turn_index:03d}-assistant",
                    role=MessageRole.assistant,
                    observed_at=_after(
                        _anchor_observed_at(peer_session, turn_index), round_no, turn_index
                    ),
                    content=subject_content,
                    metadata={
                        PEER_ATTR: subject_id,
                        TURN_ATTR: turn_index,
                        ROUND_ATTR: round_no,
                        MEDIATED_ATTR: MEDIATED_VALUE,
                        CHANNEL_SEQ_ATTR: delegation_seq,
                        **shared_subject_meta,
                    },
                )
            )
            _append(
                "delegation_close",
                round_no,
                subject_id,
                peer_id,
                {"turn": turn_index, "round": round_no, "acted_on": True},
            )

    subject_paired = _evaluate_paired(
        contract,
        adapter,
        isolated[config.subject.source_id],
        injected=subject_injected,
    )
    peer_paired = _evaluate_paired(
        contract,
        adapter,
        isolated[config.peer.source_id],
        injected=peer_injected,
    )

    members = [
        _member_evaluation(
            config.subject.source_id,
            config.subject,
            subject_cfg,
            isolated[config.subject.source_id],
            subject_paired,
        ),
        _member_evaluation(
            config.peer.source_id,
            config.peer,
            peer_cfg,
            isolated[config.peer.source_id],
            peer_paired,
        ),
    ]
    contagion_clause_ids = sorted(
        {clause for member in members for clause in member.contagion_clause_ids}
    )
    report_digest = _canonical_hash(
        {
            "swarm_id": config.swarm_id,
            "scenario_id": scenario.scenario_id,
            "channel": [entry.model_dump(mode="json", exclude_none=True) for entry in entries],
            "members": [member.model_dump(mode="json") for member in members],
            "runner_version": SWARM_RUNNER_VERSION,
        }
    )
    report = SwarmPairReport(
        swarm_id=config.swarm_id,
        scenario_id=scenario.scenario_id,
        report_id=f"SWARM-{report_digest[:24].upper()}",
        channel_entries=len(entries),
        rounds_completed=rounds_completed,
        delegation_rounds_used=delegation_rounds_used,
        members=members,
        contagion_detected=bool(contagion_clause_ids),
        contagion_clause_ids=contagion_clause_ids,
    )
    return SwarmPairExecution(
        report=report,
        run=SwarmPairRun(
            channel=entries,
            rounds_completed=rounds_completed,
            delegation_rounds_used=delegation_rounds_used,
        ),
        subject_isolated=isolated[config.subject.source_id],
        peer_isolated=isolated[config.peer.source_id],
        subject_paired=subject_paired,
        peer_paired=peer_paired,
    )
