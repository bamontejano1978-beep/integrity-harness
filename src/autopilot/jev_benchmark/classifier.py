"""Jev × Integrity phase-A benchmark — classifier protocol and mock.

Design §2/§5.4: the evaluation subject stays abstract behind
``ClassifierClient`` so any real Jev SDK plugs in via an adapter without the
benchmark code coupling to it. ``MockClassifier`` implements the protocol with
planted decisions so generator and runner pipelines are testable without
spending any external credit.

A decision is an explicit result object; a classifier that cannot decide must
raise ``ClassifierUnavailable`` (or return a malformed answer, which the
runner converts) rather than emit a fake verdict.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

from autopilot.jev_benchmark.metrics import Verdict


@dataclass(frozen=True, slots=True)
class Decision:
    """One classifier answer for one benchmark case."""

    verdict: Verdict
    confidence: float = 0.0
    rationale: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must lie in [0.0, 1.0]")


class ClassifierError(RuntimeError):
    """Base error for classifier failures."""


class ClassifierUnavailable(ClassifierError):
    """The classifier could not produce a decision for the case."""


class ClassifierClient(ABC):
    """Protocol every evaluation subject must implement.

    ``case`` is the typed case object produced by the generator (see
    ``generator.BenchmarkCase``). Implementations MUST NOT see the
    ``ground_truth`` block: they receive the case through ``observable()``,
    which exposes only the views a production sensor would see.
    """

    name: str = "classifier"
    version: str = "0.0.0"

    @abstractmethod
    def classify(self, case: Any) -> Decision:  # noqa: ANN401 - protocol boundary
        """Return the decision for one benchmark case."""

    def observable(self, case: Any) -> dict:  # noqa: ANN401 - protocol boundary
        """Return the classifier-visible projection of a case (no labels).

        Delegates to ``projection.build_observable``, the single definition
        shared by the protocol, both adapters and the fine-tune dataset
        builder: the case id (an operational handle, required for planted
        mocks and per-case bookkeeping), the two views, the **audited
        trajectory as evidence**, and the projection version.

        Hiding labels is the boundary that keeps the gold standard out of the
        classifier's sight; hiding the evidence would be a different thing
        entirely — see
        ``src/docs/projection_validity_finding_20260923_v0.1.md``.
        """

        from autopilot.jev_benchmark.projection import build_observable

        return build_observable(case)


class MockClassifier(ClassifierClient):
    """Deterministic mock for pipeline tests.

    ``planted`` maps ``case_id`` → (verdict, confidence, rationale). Unplanted
    cases default to SAFE with the configured confidence, which lets tests
    exercise specific confusion patterns by planting only the cases they care
    about. The mock reads the case id via ``observable_key`` (default
    ``case_id``) so it never needs the labelled fields.
    """

    name = "mock_classifier"
    version = "0.1.0"

    def __init__(
        self,
        planted: Mapping[str, tuple[Verdict, float, str]] | None = None,
        *,
        default_verdict: Verdict = Verdict.SAFE,
        default_confidence: float = 1.0,
        observable_key: str = "case_id",
    ) -> None:
        self.planted: dict[str, tuple[Verdict, float, str]] = dict(planted or {})
        self.default_verdict = default_verdict
        self.default_confidence = default_confidence
        self.observable_key = observable_key

    def classify(self, case: Any) -> Decision:  # noqa: ANN401 - protocol boundary
        observable = self.observable(case)
        key = observable.get(self.observable_key)
        if not isinstance(key, str):
            raise ClassifierUnavailable(
                f"observable() did not provide a {self.observable_key!r} string"
            )
        planted = self.planted.get(key)
        if planted is None:
            return Decision(
                verdict=self.default_verdict,
                confidence=self.default_confidence,
                rationale="mock default",
            )
        verdict, confidence, rationale = planted
        return Decision(verdict=verdict, confidence=confidence, rationale=rationale)


__all__ = [
    "ClassifierClient",
    "ClassifierError",
    "ClassifierUnavailable",
    "Decision",
    "MockClassifier",
]
