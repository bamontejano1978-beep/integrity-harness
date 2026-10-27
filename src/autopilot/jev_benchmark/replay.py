"""Recorded-verdict replay: rehearse the full benchmark pipeline cheaply.

Two collaborators around the ``ClassifierClient`` protocol:

- ``RecordingClassifier`` wraps any classifier (a real Jev adapter, a cheap-LLM
  adapter, or a mock) and transparently records every decision it produces
  into an append-only JSONL transcript. Each record carries a ``view_digest``:
  the SHA-256 of the exact ``observable()`` projection the decision was made
  on.
- ``ReplayClassifier`` is a ``ClassifierClient`` that answers exclusively from
  a transcript. A recorded run replays with identical decisions, so the
  report pipeline (runner → report JSON → fill script) can be rehearsed
  end-to-end without calling any model and without spending Jev credit.

Integrity rules:

- One decision per ``case_id``. Recording a two-run benchmark collapses the
  pair only if both runs agreed on every field — a flaky source surfaces at
  record time instead of poisoning the transcript.
- Replay verifies the ``view_digest`` of every case it serves: if the case's
  observable projection changed after recording, the whole transcript is
  void and replay raises immediately (fail closed) rather than serving
  stale decisions that would silently contaminate the report.
- Missing cases raise ``ClassifierUnavailable`` (the runner records the error
  and continues, exactly like a live classifier failure).
- Replay is read-only: it never falls back to live inference and never
  mutates the transcript.

Transcripts are byte-deterministic: records are sorted by ``case_id`` and
carry a logical timestamp (the ``GENERATION_EPOCH`` convention — never the
wall clock). Nothing here authorizes production deployment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autopilot.jev_benchmark.classifier import (
    ClassifierClient,
    ClassifierError,
    ClassifierUnavailable,
    Decision,
)
from autopilot.jev_benchmark.metrics import Verdict

TRANSCRIPT_VERSION = 1
# Logical timestamp (same convention as generator.GENERATION_EPOCH): a
# transcript must be byte-deterministic, so it never carries the wall clock.
REPLAY_EPOCH = "2026-09-20T12:00:00Z"

_REQUIRED_RECORD_FIELDS = (
    "case_id",
    "verdict",
    "confidence",
    "rationale",
    "view_digest",
)


class ReplayIntegrityError(ClassifierError):
    """The transcript cannot serve this case without lying about a decision."""


def _view_digest(observable: dict[str, Any]) -> str:
    canonical = json.dumps(
        observable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """One recorded verdict plus the identity of the view it answered."""

    case_id: str
    verdict: str
    confidence: float
    rationale: str
    view_digest: str

    def to_record(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "view_digest": self.view_digest,
            "recorded_at": REPLAY_EPOCH,
        }


class RecordingClassifier(ClassifierClient):
    """Wrap a classifier and record every decision it produces.

    The wrapped client sees exactly what it would see unwrapped; the recorder
    adds only the transcript side-effect. Classifying the same case twice
    (the runner's two-run protocol) is fine as long as both decisions agree;
    ``save()`` refuses to write a transcript with conflicting records.
    """

    def __init__(self, inner: ClassifierClient) -> None:
        self._inner = inner
        self.name = inner.name
        self.version = inner.version
        self._records: list[RecordedDecision] = []

    def classify(self, case: Any) -> Decision:  # noqa: ANN401 - protocol boundary
        observable = self.observable(case)
        digest = _view_digest(observable)
        key = observable.get("case_id")
        decision = self._inner.classify(case)
        self._records.append(
            RecordedDecision(
                case_id=key if isinstance(key, str) else "",
                verdict=decision.verdict.value,
                confidence=decision.confidence,
                rationale=decision.rationale,
                view_digest=digest,
            )
        )
        return decision

    @property
    def records(self) -> tuple[RecordedDecision, ...]:
        return tuple(self._records)

    def save(self, path: Path) -> Path:
        """Write the JSONL transcript (header + one record per case)."""

        if not self._records:
            raise ClassifierError(
                "nothing recorded; refusing to write an empty transcript"
            )
        seen: dict[str, RecordedDecision] = {}
        for record in self._records:
            if not record.case_id:
                raise ClassifierError(
                    "recorded decision without a 'case_id' observable key"
                )
            previous = seen.get(record.case_id)
            if previous is None:
                seen[record.case_id] = record
                continue
            if (
                previous.verdict != record.verdict
                or previous.confidence != record.confidence
                or previous.rationale != record.rationale
                or previous.view_digest != record.view_digest
            ):
                raise ClassifierError(
                    f"inconsistent recorded decisions for {record.case_id}; "
                    "a transcript holds exactly one decision per case — the "
                    "source classifier was unstable between runs"
                )
        header = {
            "transcript_version": TRANSCRIPT_VERSION,
            "recorded_at": REPLAY_EPOCH,
            "source": {"name": self._inner.name, "version": self._inner.version},
            "decision_count": len(seen),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
        lines += [
            json.dumps(record.to_record(), ensure_ascii=False, sort_keys=True)
            for record in sorted(seen.values(), key=lambda item: item.case_id)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def load_transcript(path: Path) -> tuple[dict[str, Any], list[RecordedDecision]]:
    """Parse and validate a transcript; returns (header, records).

    Every integrity problem — malformed lines, unknown verdicts, out-of-range
    confidence, duplicate case ids — raises ``ReplayIntegrityError`` instead
    of yielding a partially-trusted transcript.
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ReplayIntegrityError(f"empty transcript: {path}")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ReplayIntegrityError(f"malformed transcript header: {path}") from exc
    if header.get("transcript_version") != TRANSCRIPT_VERSION:
        raise ReplayIntegrityError(
            f"unsupported transcript version: {header.get('transcript_version')!r}"
        )
    records: list[RecordedDecision] = []
    for line in lines[1:]:
        if not line.strip():
            raise ReplayIntegrityError(f"blank line inside transcript: {path}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayIntegrityError(f"malformed transcript record: {path}") from exc
        missing = [field for field in _REQUIRED_RECORD_FIELDS if field not in payload]
        if missing:
            raise ReplayIntegrityError(
                f"record missing fields {missing}: {path}"
            )
        try:
            verdict = Verdict(payload["verdict"])
        except ValueError as exc:
            raise ReplayIntegrityError(
                f"unknown verdict {payload['verdict']!r}: {path}"
            ) from exc
        confidence = payload["confidence"]
        if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            raise ReplayIntegrityError(f"confidence out of range: {path}")
        records.append(
            RecordedDecision(
                case_id=str(payload["case_id"]),
                verdict=verdict.value,
                confidence=float(confidence),
                rationale=str(payload.get("rationale", "")),
                view_digest=str(payload["view_digest"]),
            )
        )
    case_ids = [record.case_id for record in records]
    if len(set(case_ids)) != len(case_ids):
        raise ReplayIntegrityError(f"duplicate case_id in transcript: {path}")
    return header, records


class ReplayClassifier(ClassifierClient):
    """Answer exclusively from a recorded transcript — never from a model.

    ``name``/``version`` expose the *recorded* source (prefixed ``replay:``)
    so report provenance stays honest about what produced the decisions.
    """

    def __init__(self, transcript: Path) -> None:
        header, records = load_transcript(transcript)
        self._by_case = {record.case_id: record for record in records}
        source = header.get("source", {})
        self.name = f"replay:{source.get('name', 'unknown')}"
        self.version = str(source.get("version", "0.0.0"))
        self.transcript_path = transcript
        self.served = 0

    def classify(self, case: Any) -> Decision:  # noqa: ANN401 - protocol boundary
        observable = self.observable(case)
        digest = _view_digest(observable)
        key = observable.get("case_id")
        if not isinstance(key, str):
            raise ReplayIntegrityError(
                "observable() did not provide a 'case_id' string"
            )
        record = self._by_case.get(key)
        if record is None:
            raise ClassifierUnavailable(f"no recorded decision for case {key}")
        if record.view_digest != digest:
            raise ReplayIntegrityError(
                f"recorded view for {key} does not match the current case view "
                f"(expected {record.view_digest[:12]}…, got {digest[:12]}…); "
                "the case changed after recording — the transcript is void"
            )
        self.served += 1
        return Decision(
            verdict=Verdict(record.verdict),
            confidence=record.confidence,
            rationale=record.rationale,
        )


__all__ = [
    "REPLAY_EPOCH",
    "TRANSCRIPT_VERSION",
    "RecordedDecision",
    "ReplayClassifier",
    "ReplayIntegrityError",
    "RecordingClassifier",
    "load_transcript",
]
