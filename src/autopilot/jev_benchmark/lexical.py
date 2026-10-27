"""Lexical baseline B0 — bag of words over the state, as a shortcut detector.

Why this exists. A benchmark can be passed by reading the surface of its cases
instead of the behaviour they describe. The phase-A projection defect (20 state
texts for 4,000 cases, 100 % of undecidable cases colliding with decidable
ones) was that failure mode one layer down; the fix put the audited trajectory
into the state, but the *diversity* limit survived: the dataset is built from 60
trajectories and the sealed holdout reuses 100 % of the working split's state
texts.

So B0 answers the question every other result must be read against:

    **How far can a classifier get with no semantics at all — just a linear
    model on word buckets?**

It is a real ``ClassifierClient``, replayable through the official pipeline, and
deliberately *not* a competitor to a System One sensor: it is the floor. Two
numbers matter, and the gap between them is the diagnostic:

- **seen-text**: fit on the working split, scored on the same cases. A model
  that memorises state texts scores ~1.0 here no matter how good the benchmark
  is; this is the ceiling a shortcut would reach.
- **unseen-text**: leave-texts-out cross-validation, where whole state texts —
  and therefore whole (mechanism × stratum × domain) cells — are removed from
  the fitting set. This is the honest generalisation floor of a surface-only
  model.

If a sensor's advantage over B0-unseen is small, its result is superficial. If
B0-unseen is high *and* its strongest features are evidence fields (log status,
observation attributes), the benchmark is legible without being shallow — the
desired state of affairs.

Honest limits, up front: a linear bag-of-words model cannot read the rule, only
correlate with it; it is fitted on the working split and never on the sealed
holdout (``fit`` refuses holdout case ids); and its probabilities are a softmax
over buckets, not a calibrated epistemic signal.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from autopilot.jev_benchmark.classifier import (
    ClassifierClient,
    ClassifierUnavailable,
    Decision,
)
from autopilot.jev_benchmark.generator import HOLDOUT_EVERY_NTH, BenchmarkCase, _is_holdout
from autopilot.jev_benchmark.metrics import VERDICT_FOR_GROUND_TRUTH, Verdict
from autopilot.jev_benchmark.projection import render_state

LEXICAL_VERSION = "0.1.0"
#: Hashing trick: a fixed, process-stable bucket space (``zlib.crc32``, never
#: Python's salted ``hash``) keeps the model deterministic across runs.
HASH_BUCKETS = 4096
_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.:/-]*")

#: The label space B0 is fitted on — the benchmark's own compatibility rule
#: (present ↔ RISK, absent ↔ SAFE, uncertain ↔ UNCERTAIN), so the lexical
#: baseline answers in exactly the vocabulary a sensor must answer in.
_CLASSES = (Verdict.SAFE.value, Verdict.UNCERTAIN.value, Verdict.RISK.value)


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _features(tokens: Sequence[str]) -> list[str]:
    """Unigrams and bigrams — enough to pick up phrasing, no syntax."""

    features = list(tokens)
    features.extend(f"{left} {right}" for left, right in zip(tokens, tokens[1:]))
    return features


def _bucket(feature: str, buckets: int) -> int:
    return zlib.crc32(feature.encode("utf-8")) % buckets


@dataclass(frozen=True, slots=True)
class LexicalFitReport:
    """Provenance of one fit: what B0 was allowed to see, and how well it fit."""

    cases: int
    distinct_texts: int
    features: int
    buckets: int
    epochs: int
    training_accuracy: float
    classes: tuple[str, ...]


class LexicalClassifier(ClassifierClient):
    """Bag-of-words logistic regression over the shared state projection.

    Deterministic by construction: hashed buckets, zero-initialised weights, a
    fixed Adam schedule, no RNG anywhere. Same inputs → byte-identical
    decisions, so record/replay works and two-run stability is measured rather
    than assumed.
    """

    version = LEXICAL_VERSION

    def __init__(
        self,
        *,
        buckets: int = HASH_BUCKETS,
        epochs: int = 200,
        learning_rate: float = 0.05,
        l2: float = 1e-4,
        evidence: bool = True,
    ) -> None:
        if buckets <= 0:
            raise ValueError("buckets must be positive")
        if epochs <= 0:
            raise ValueError("epochs must be positive")
        self._buckets = buckets
        self._epochs = epochs
        self._learning_rate = learning_rate
        self._l2 = l2
        #: Ablation switch. ``evidence=False`` reproduces the pre-fix projection
        #: (views only, no audited trajectory) so the shortcut can be located:
        #: if B0 keeps passing without the evidence, the label lives in the
        #: views' furniture; if it collapses, B0 was reading the evidence.
        self._evidence = evidence
        # The ablation is a different classifier for reporting purposes, so the
        # runner's records never conflate the two arms.
        self.name = "lexical_b0" if evidence else "lexical_b0_views_only"
        self._idf: np.ndarray | None = None
        self._weights: np.ndarray | None = None
        self._bias: np.ndarray | None = None
        self._vocabulary: dict[int, list[str]] = {}
        #: Feature vectors depend on the fitted idf, and the state text of a case
        #: is stable, so a per-instance text→vector cache makes repeated scoring
        #: and cross-validation cheap without changing a single number.
        self._vector_cache: dict[str, np.ndarray] = {}
        self.fit_report: LexicalFitReport | None = None

    @property
    def fitted(self) -> bool:
        return self._weights is not None

    def state_text(self, case: Any) -> str:  # noqa: ANN401 - protocol boundary
        """The text this baseline reads — the shared renderer, minus evidence
        when the ablation switch is on."""

        observable = self.observable(case)
        if not self._evidence:
            observable = {**observable, "evidence": {}}
        return render_state(observable)

    # -- fitting -----------------------------------------------------------

    def _vector(self, text: str) -> np.ndarray:
        """TF-IDF hashed bag of words, L2-normalised (cached per state text)."""

        cached = self._vector_cache.get(text)
        if cached is not None:
            return cached

        counts = np.zeros(self._buckets, dtype=np.float32)
        for feature in _features(_tokens(text)):
            counts[_bucket(feature, self._buckets)] += 1.0
        scaled = np.where(counts > 0, 1.0 + np.log(np.maximum(counts, 1.0)), 0.0)
        assert self._idf is not None
        vector = scaled.astype(np.float32) * self._idf
        norm = float(np.linalg.norm(vector))
        vector = vector / norm if norm > 0 else vector
        self._vector_cache[text] = vector
        return vector

    def fit(
        self,
        cases: Iterable[BenchmarkCase],
        *,
        holdout_every_nth: int | None = HOLDOUT_EVERY_NTH,
    ) -> LexicalClassifier:
        """Fit on cases, refusing any sealed-holdout case (fail closed).

        Training on the holdout would destroy the only sealed measurement the
        program has; the check lives here so it cannot happen by accident.
        """

        self._vector_cache.clear()  # a refit changes idf; stale vectors must go
        usable: list[BenchmarkCase] = []
        refused = 0
        for case in cases:
            if _is_holdout(case.case_id, holdout_every_nth):
                refused += 1
                continue
            usable.append(case)
        if refused:
            raise ClassifierUnavailable(
                f"refusing to fit on {refused} holdout case(s): the holdout is sealed"
            )
        if not usable:
            raise ClassifierUnavailable("nothing to fit on")

        texts = [self.state_text(case) for case in usable]
        vocabulary: dict[int, set[str]] = {}
        document_frequency = np.zeros(self._buckets, dtype=np.float32)
        for text in texts:
            buckets = {
                _bucket(feature, self._buckets)
                for feature in _features(_tokens(text))
            }
            for bucket in buckets:
                document_frequency[bucket] += 1.0
            for feature in _features(_tokens(text)):
                vocabulary.setdefault(_bucket(feature, self._buckets), set()).add(feature)
        total = float(len(texts))
        self._idf = (np.log((1.0 + total) / (1.0 + document_frequency)) + 1.0).astype(
            np.float32
        )
        self._vocabulary = {
            bucket: sorted(names) for bucket, names in vocabulary.items()
        }

        matrix = np.stack([self._vector(text) for text in texts])
        labels = np.array(
            [
                _CLASSES.index(VERDICT_FOR_GROUND_TRUTH[case.ground_truth.value].value)
                for case in usable
            ],
            dtype=np.int64,
        )

        weights = np.zeros((self._buckets, len(_CLASSES)), dtype=np.float32)
        bias = np.zeros(len(_CLASSES), dtype=np.float32)
        # Adam with a fixed schedule: deterministic, no shuffling, no RNG.
        first = np.zeros_like(weights)
        second = np.zeros_like(weights)
        first_b = np.zeros_like(bias)
        second_b = np.zeros_like(bias)
        beta1, beta2, epsilon = 0.9, 0.999, 1e-8
        for step in range(1, self._epochs + 1):
            logits = matrix @ weights + bias
            logits -= logits.max(axis=1, keepdims=True)
            exp = np.exp(logits)
            probabilities = exp / exp.sum(axis=1, keepdims=True)
            probabilities[np.arange(len(labels)), labels] -= 1.0
            gradient = matrix.T @ probabilities / len(labels) + self._l2 * weights
            gradient_b = probabilities.mean(axis=0)
            for parameter, gradient_param, first_m, second_m in (
                (weights, gradient, first, second),
                (bias, gradient_b, first_b, second_b),
            ):
                first_m *= beta1
                first_m += (1.0 - beta1) * gradient_param
                second_m *= beta2
                second_m += (1.0 - beta2) * gradient_param**2
                step_size = self._learning_rate * np.sqrt(1.0 - beta2**step) / (
                    1.0 - beta1**step
                )
                parameter -= step_size * first_m / (np.sqrt(second_m) + epsilon)

        self._weights = weights
        self._bias = bias
        predictions = (matrix @ weights + bias).argmax(axis=1)
        self.fit_report = LexicalFitReport(
            cases=len(usable),
            distinct_texts=len(set(texts)),
            features=len(self._vocabulary),
            buckets=self._buckets,
            epochs=self._epochs,
            training_accuracy=float((predictions == labels).mean()),
            classes=_CLASSES,
        )
        return self

    # -- protocol ----------------------------------------------------------

    def probabilities(self, case: Any) -> dict[str, float]:  # noqa: ANN401 - protocol boundary
        if self._weights is None or self._idf is None or self._bias is None:
            raise ClassifierUnavailable("lexical B0 is not fitted")
        logits = self._vector(self.state_text(case)) @ self._weights
        logits = logits + self._bias
        logits = logits - logits.max()
        exp = np.exp(logits)
        shares = exp / exp.sum()
        return {name: float(share) for name, share in zip(_CLASSES, shares)}

    def classify(self, case: Any) -> Decision:  # noqa: ANN401 - protocol boundary
        probabilities = self.probabilities(case)
        verdict_name = max(probabilities, key=probabilities.get)
        return Decision(
            verdict=Verdict(verdict_name),
            confidence=probabilities[verdict_name],
            rationale=f"lexical_b0:argmax({verdict_name.lower()})",
        )

    def trace(self, case: Any) -> dict[str, Any]:  # noqa: ANN401 - protocol boundary
        """Per-case trace in the adapters' shape, for offline diagnostics.

        ``evidence_noul`` is B0's lexical analogue of the evidence question
        (``1 - P(UNCERTAIN)``): the same number the §6 channel diagnostics
        sweep, so the baseline is scored by the same code as a real sensor.
        """

        probabilities = self.probabilities(case)
        verdict = max(probabilities, key=probabilities.get)
        return {
            "raw_choice": verdict,
            "raw_choice_probability": probabilities[verdict],
            "probabilities": dict(probabilities),
            "evidence_noul": 1.0 - probabilities[Verdict.UNCERTAIN.value],
        }

    # -- interpretability --------------------------------------------------

    def top_features(self, *, limit: int = 12) -> dict[str, list[str]]:
        """Strongest features per class, with names — the shortcut readout.

        Hashing is one-way, so the names come from the bucket → feature map
        built at fit time. If the top features of RISK/SAFE/UNCERTAIN are
        evidence fields (``incomplete``, ``complete``, observation attributes),
        B0 is reading the case; if they are domain furniture or case-shaped
        tokens, it is reading the template.
        """

        if self._weights is None:
            raise ClassifierUnavailable("lexical B0 is not fitted")
        report: dict[str, list[str]] = {}
        for index, name in enumerate(_CLASSES):
            order = np.argsort(-self._weights[:, index])[: limit * 3]
            names: list[str] = []
            for bucket in order:
                for feature in self._vocabulary.get(int(bucket), ()):
                    if feature not in names:
                        names.append(feature)
                    if len(names) >= limit:
                        break
                if len(names) >= limit:
                    break
            report[name] = names
        return report


__all__ = [
    "HASH_BUCKETS",
    "LEXICAL_VERSION",
    "LexicalClassifier",
    "LexicalFitReport",
]
