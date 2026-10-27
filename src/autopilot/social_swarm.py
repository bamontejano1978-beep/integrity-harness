"""Shared primitives for the N-agent social-psychology experiments (v0.1).

Two experiments ride on this layer today: the bystander / diffusion-of-
responsibility effect (``autopilot.bystander``) and the Asch conformity
paradigm (``autopilot.asch``). They share one evidence format:

- one **mediated, hash-chained channel** built from ``SwarmChannelEntry`` (the
  same primitive M1/M2 use and ``verify_swarm_channel`` re-checks);
- per-member **deterministic records** (``SocialMember``) with stable draws;
- one **signed report** binding the report payload, the channel digest, the
  chain head and every member record under a single Ed25519 signature — the M2
  signing pattern (``SwarmPairReport`` in ``autopilot.swarm``), generalised
  from two members to N.

Nothing here decides behaviour: the caller owns the policy, and the swarm
contract engine (``swarm_evaluator``) still owns any contract verdict. This
module builds the channel, the digests and the signature envelope, and nothing
else. That keeps the "what is signed" question in one place instead of
duplicating it per experiment.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from pydantic import BaseModel, ConfigDict, Field, model_serializer

from autopilot.agent_cohort import _canonical_hash
from autopilot.swarm import (
    CHANNEL_SEQ_ATTR,
    SwarmChannelEntry,
    SwarmPairRun,
    SwarmReportSignature,
    _canonical_json_bytes,
    _load_swarm_private_key,
    _load_swarm_public_key,
    _public_key_id,
    verify_swarm_channel,
)

SOCIAL_REPORT_VERSION = "0.1.0"

_SHA256 = r"^[a-f0-9]{64}$"
_ZERO_HEAD = "0" * 64


class SocialMember(BaseModel):
    """Base record for one participating member, bound into a signed report.

    A member only has to be identifiable (``source_id``) and canonically
    serialisable: the digest over its JSON dump is what the signature covers.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str


class SocialReport(BaseModel):
    """A signed report over one N-agent mediated run (M2 pattern, N members).

    The signature covers the whole document except the signature block, and the
    document itself binds the channel digest, the chain head and one digest per
    member record. Touching the channel, a member record or the report payload
    therefore breaks verification — exactly as ``SwarmPairReport`` does for a
    pair, with ``group_size`` replacing the two fixed roles.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    experiment_kind: Annotated[str, Field(min_length=1, max_length=60)]
    experiment_id: Annotated[str, Field(min_length=1, max_length=120)]
    seed: Annotated[str, Field(min_length=1, max_length=120)]
    run_index: int
    group_size: int
    report_id: str
    #: Experiment-specific result of the run (verdict, counts, flags).
    payload: dict[str, Any] = Field(default_factory=dict)
    swarm_contract_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    contract_sha256: Annotated[str, Field(pattern=_SHA256)] | None = None
    #: Digest of the whole sweep/batch the run belongs to, when there is one.
    batch_digest: Annotated[str, Field(pattern=_SHA256)] | None = None
    channel_sha256: Annotated[str, Field(pattern=_SHA256)] | None = None
    channel_head_sha256: Annotated[str, Field(pattern=_SHA256)] | None = None
    member_digests: dict[str, Annotated[str, Field(pattern=_SHA256)]] | None = None
    signature: SwarmReportSignature | None = None

    @model_serializer(mode="wrap")
    def omit_unsigned_evidence_fields(self, handler):
        """Keep unsigned report bytes/schema unchanged when fields are absent."""

        document = handler(self)
        for name in (
            "swarm_contract_id",
            "contract_sha256",
            "batch_digest",
            "channel_sha256",
            "channel_head_sha256",
            "member_digests",
            "signature",
        ):
            if getattr(self, name) is None:
                document.pop(name, None)
        return document


class SocialReportVerification(BaseModel):
    """Detailed report, key, channel and member-record verification state."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    report_id_valid: bool
    payload_digest_valid: bool
    signature_valid: bool
    key_id_valid: bool
    channel_digest_valid: bool
    channel_head_valid: bool
    channel_chain_valid: bool
    member_digests_valid: bool
    problems: list[str]


def unit_interval(*parts: object) -> float:
    """A deterministic value in ``[0, 1)`` from the SHA-256 of the given parts.

    The shared source of every experiment's pseudo-random draw: no RNG, no
    clock, so the same seed produces the same cast of members byte for byte.
    """

    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def append_channel_entry(
    entries: list[SwarmChannelEntry],
    kind: str,
    source_agent: str,
    target_agent: str,
    payload: dict,
    *,
    round_no: int = 1,
) -> SwarmChannelEntry:
    """Append one hash-chained entry (the chaining rule the pair runner uses).

    Each payload cites its own ``swarm_channel_seq``, so evidence built from the
    traffic resolves back to the channel entry that carried it.
    """

    seq = len(entries) + 1
    payload = {**payload, CHANNEL_SEQ_ATTR: seq}
    payload_sha256 = _canonical_hash(payload)
    prev_entry_sha256 = entries[-1].entry_sha256 if entries else None
    entry_sha256 = _canonical_hash(
        {
            "seq": seq,
            "round": round_no,
            "kind": kind,
            "source_agent": source_agent,
            "target_agent": target_agent,
            "payload_sha256": payload_sha256,
            "prev_entry_sha256": prev_entry_sha256,
        }
    )
    entry = SwarmChannelEntry(
        seq=seq,
        round=round_no,
        kind=kind,
        source_agent=source_agent,
        target_agent=target_agent,
        payload=payload,
        payload_sha256=payload_sha256,
        prev_entry_sha256=prev_entry_sha256,
        entry_sha256=entry_sha256,
    )
    entries.append(entry)
    return entry


def channel_document(channel: SwarmPairRun) -> list[dict[str, Any]]:
    return [entry.model_dump(mode="json", exclude_none=True) for entry in channel.channel]


def channel_digests(channel: SwarmPairRun) -> tuple[str, str]:
    """Digest over the canonical channel document and its chain head."""

    digest = hashlib.sha256(_canonical_json_bytes(channel_document(channel))).hexdigest()
    head = channel.channel[-1].entry_sha256 if channel.channel else _ZERO_HEAD
    return digest, head


def member_digests(members: Sequence[SocialMember]) -> dict[str, str]:
    return {
        member.source_id: hashlib.sha256(
            _canonical_json_bytes(member.model_dump(mode="json"))
        ).hexdigest()
        for member in members
    }


def report_id_for(
    *,
    experiment_kind: str,
    experiment_id: str,
    seed: str,
    run_index: int,
    group_size: int,
    channel: SwarmPairRun,
    members: Sequence[SocialMember],
    payload: dict[str, Any],
) -> str:
    digest = _canonical_hash(
        {
            "experiment_kind": experiment_kind,
            "experiment_id": experiment_id,
            "seed": seed,
            "run_index": run_index,
            "group_size": group_size,
            "channel": channel_document(channel),
            "members": [member.model_dump(mode="json") for member in members],
            "payload": payload,
        }
    )
    return f"{experiment_kind.upper()}-{digest[:24].upper()}"


def resolve_signed_at(signed_at: str | None) -> datetime:
    """Resolve the signing moment; an explicit one must be RFC 3339 with tz."""

    if signed_at is None:
        return datetime.now(timezone.utc)
    try:
        moment = datetime.fromisoformat(signed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("signed_at must be an RFC 3339 timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError("signed_at must include a timezone")
    return moment


def build_social_report(
    *,
    experiment_kind: str,
    experiment_id: str,
    seed: str,
    run_index: int,
    group_size: int,
    payload: dict[str, Any],
    channel: SwarmPairRun,
    members: Sequence[SocialMember],
    swarm_contract_id: str | None = None,
    contract_sha256: str | None = None,
    batch_digest: str | None = None,
) -> SocialReport:
    """Assemble the unsigned report (report id + evidence digests, no signature)."""

    channel_sha256, channel_head_sha256 = channel_digests(channel)
    return SocialReport(
        experiment_kind=experiment_kind,
        experiment_id=experiment_id,
        seed=seed,
        run_index=run_index,
        group_size=group_size,
        report_id=report_id_for(
            experiment_kind=experiment_kind,
            experiment_id=experiment_id,
            seed=seed,
            run_index=run_index,
            group_size=group_size,
            channel=channel,
            members=members,
            payload=payload,
        ),
        payload=payload,
        swarm_contract_id=swarm_contract_id,
        contract_sha256=contract_sha256,
        batch_digest=batch_digest,
        channel_sha256=channel_sha256,
        channel_head_sha256=channel_head_sha256,
        member_digests=member_digests(members),
        signature=None,
    )


def sign_social_report(
    report: SocialReport,
    *,
    channel: SwarmPairRun,
    members: Sequence[SocialMember],
    private_key_path: str | Path,
    public_key_path: str | Path,
    signed_at: str | None = None,
) -> SocialReport:
    """Recompute the evidence digests and sign the canonical report payload."""

    private_key = _load_swarm_private_key(private_key_path)
    public_key = _load_swarm_public_key(public_key_path)
    if _public_key_id(private_key.public_key()) != _public_key_id(public_key):
        raise ValueError("social report private and public keys do not match")
    if report.report_id != report_id_for(
        experiment_kind=report.experiment_kind,
        experiment_id=report.experiment_id,
        seed=report.seed,
        run_index=report.run_index,
        group_size=report.group_size,
        channel=channel,
        members=members,
        payload=report.payload,
    ):
        raise ValueError("report_id does not match the supplied channel, members and payload")
    channel_sha256, channel_head_sha256 = channel_digests(channel)
    moment = resolve_signed_at(signed_at)
    unsigned = report.model_copy(
        update={
            "channel_sha256": channel_sha256,
            "channel_head_sha256": channel_head_sha256,
            "member_digests": member_digests(members),
            "signature": None,
        }
    )
    payload_bytes = _canonical_json_bytes(unsigned.model_dump(mode="json", exclude={"signature"}))
    signature = SwarmReportSignature(
        key_id=_public_key_id(public_key),
        signed_payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        value_base64=base64.b64encode(private_key.sign(payload_bytes)).decode("ascii"),
        signed_at=moment.isoformat(),
    )
    return unsigned.model_copy(update={"signature": signature})


def verify_social_report(
    report: SocialReport,
    *,
    channel: SwarmPairRun,
    members: Sequence[SocialMember],
    public_key_path: str | Path,
) -> SocialReportVerification:
    """Verify the signature plus every bound channel and member-record digest."""

    problems: list[str] = []
    signature = report.signature
    if signature is None:
        return SocialReportVerification(
            valid=False,
            report_id_valid=False,
            payload_digest_valid=False,
            signature_valid=False,
            key_id_valid=False,
            channel_digest_valid=False,
            channel_head_valid=False,
            channel_chain_valid=False,
            member_digests_valid=False,
            problems=["social report is unsigned"],
        )
    key = _load_swarm_public_key(public_key_path)
    key_id_valid = signature.key_id == _public_key_id(key)
    if not key_id_valid:
        problems.append("signing key does not match the configured verification key")
    payload_bytes = _canonical_json_bytes(report.model_dump(mode="json", exclude={"signature"}))
    digest_valid = hashlib.sha256(payload_bytes).hexdigest() == signature.signed_payload_sha256
    if not digest_valid:
        problems.append("signed report payload digest does not match")
    signature_valid = False
    try:
        signature_bytes = base64.b64decode(signature.value_base64, validate=True)
        key.verify(signature_bytes, payload_bytes)
        signature_valid = True
    except (ValueError, InvalidSignature):
        problems.append("Ed25519 social report signature is invalid")

    report_id_valid = report.report_id == report_id_for(
        experiment_kind=report.experiment_kind,
        experiment_id=report.experiment_id,
        seed=report.seed,
        run_index=report.run_index,
        group_size=report.group_size,
        channel=channel,
        members=members,
        payload=report.payload,
    )
    if not report_id_valid:
        problems.append("report_id does not match report, channel, members and payload")

    channel_sha256, channel_head_sha256 = channel_digests(channel)
    channel_digest_valid = report.channel_sha256 == channel_sha256
    if not channel_digest_valid:
        problems.append("channel digest does not match the report")
    channel_head_valid = (
        report.channel_head_sha256 == channel_head_sha256
    )
    if not channel_head_valid:
        problems.append("channel chain head does not match the report")
    chain_problems = verify_swarm_channel(channel)
    channel_chain_valid = not chain_problems
    problems.extend(chain_problems)

    member_digests_valid = report.member_digests == member_digests(members)
    if not member_digests_valid:
        problems.append("member record digests do not match the report")

    return SocialReportVerification(
        valid=(
            report_id_valid
            and digest_valid
            and signature_valid
            and key_id_valid
            and channel_digest_valid
            and channel_head_valid
            and channel_chain_valid
            and member_digests_valid
        ),
        report_id_valid=report_id_valid,
        payload_digest_valid=digest_valid,
        signature_valid=signature_valid,
        key_id_valid=key_id_valid,
        channel_digest_valid=channel_digest_valid,
        channel_head_valid=channel_head_valid,
        channel_chain_valid=channel_chain_valid,
        member_digests_valid=member_digests_valid,
        problems=problems,
    )


def dump_social_evidence(
    channel: SwarmPairRun, members: Sequence[SocialMember]
) -> dict[str, list[dict[str, Any]]]:
    """The portable evidence for a signed report: channel + member records."""

    return {
        "channel": channel_document(channel),
        "members": [member.model_dump(mode="json") for member in members],
    }


def load_social_evidence(
    document: dict[str, Any], member_model: type[SocialMember]
) -> tuple[SwarmPairRun, list[SocialMember]]:
    """Rebuild (channel, members) from a dumped evidence document."""

    entries = [SwarmChannelEntry.model_validate(item) for item in document["channel"]]
    members = [member_model.model_validate(item) for item in document["members"]]
    channel = SwarmPairRun(
        channel=entries,
        rounds_completed=max((entry.round for entry in entries), default=0),
        delegation_rounds_used=sum(entry.kind == "delegation_open" for entry in entries),
    )
    return channel, members


class SocialDirectoryVerification(BaseModel):
    """Outcome of verifying every signed report under a directory tree."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    checked: int
    skipped: int
    problems: list[str]


def verify_social_reports(
    directory: str | Path,
    *,
    public_key_path: str | Path,
    member_models: Mapping[str, type[SocialMember]],
) -> SocialDirectoryVerification:
    """Verify every ``*.report.json`` under ``directory`` against its evidence.

    ``member_models`` maps an ``experiment_kind`` to the member model its
    evidence was written with. A report of an unknown kind is counted as
    skipped, never silently accepted; a missing or unreadable evidence file is
    a problem, so a half-copied directory fails closed.
    """

    root = Path(directory)
    report_paths = sorted(root.rglob("*.report.json"))
    if not report_paths:
        return SocialDirectoryVerification(
            valid=False,
            checked=0,
            skipped=0,
            problems=[f"no *.report.json under {root}"],
        )
    problems: list[str] = []
    checked = 0
    skipped = 0
    for report_path in report_paths:
        evidence_path = report_path.with_name(
            report_path.name.replace(".report.json", ".evidence.json")
        )
        try:
            report = load_social_report(report_path)
        except ValueError as exc:
            problems.append(f"{report_path.name}: {exc}")
            continue
        member_model = member_models.get(report.experiment_kind)
        if member_model is None:
            skipped += 1
            continue
        if not evidence_path.exists():
            problems.append(f"{report.report_id}: missing {evidence_path.name}")
            continue
        try:
            document = json.loads(evidence_path.read_text(encoding="utf-8"))
            channel, members = load_social_evidence(document, member_model)
            verification = verify_social_report(
                report, channel=channel, members=members, public_key_path=public_key_path
            )
        except (OSError, ValueError) as exc:
            problems.append(f"{report.report_id}: {exc}")
            continue
        checked += 1
        problems.extend(
            f"{report.report_id}: {problem}" for problem in verification.problems
        )
    return SocialDirectoryVerification(
        valid=bool(not problems and checked > 0),
        checked=checked,
        skipped=skipped,
        problems=problems,
    )


def save_social_report(report: SocialReport, path: str | Path) -> None:
    """Write a signed report as JSON; evidence is never overwritten."""

    target = Path(path)
    if target.exists():
        raise FileExistsError(f"{target} already exists; evidence is never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_social_report(path: str | Path) -> SocialReport:
    """Load a signed report, rejecting malformed envelopes."""

    try:
        return SocialReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid social report {path}: {exc}") from exc


__all__ = [
    "SOCIAL_REPORT_VERSION",
    "SocialDirectoryVerification",
    "SocialMember",
    "SocialReport",
    "SocialReportVerification",
    "append_channel_entry",
    "build_social_report",
    "channel_digests",
    "channel_document",
    "dump_social_evidence",
    "load_social_evidence",
    "load_social_report",
    "member_digests",
    "report_id_for",
    "resolve_signed_at",
    "save_social_report",
    "sign_social_report",
    "unit_interval",
    "verify_social_report",
    "verify_social_reports",
]
