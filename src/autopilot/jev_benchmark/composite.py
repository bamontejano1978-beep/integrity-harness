"""Composite evaluator-first classifier — the architecture result, as a client.

The Jev × Integrity program measured (checkpoint §3b, E1/E2/E3) that Jev
1.13's deferral channel carries no information beyond its confidence scalar,
while the deterministic DSL evaluator separates every benchmark stratum
perfectly. The composite architecture follows: **the deterministic evaluator
decides; Jev's recorded confidence is demoted to human-queue triage
metadata.**

Routing rule (checkpoint §3b, composite block):

- instrumenting clause ``fail``            → ``RISK`` (alarm)
- instrumenting clause ``pass``            → ``SAFE`` (automate)
- instrumenting clause ``not_applicable``  → ``SAFE`` (nothing to evaluate)
- instrumenting clause ``indeterminate``   → ``UNCERTAIN`` (human queue)

Triage: when a Jev transcript is supplied, the recorded verdict/confidence
of an ``UNCERTAIN`` case is appended to the decision *rationale only* — it
orders the human review queue and can never change the verdict. This is the
formalisation of "Jev as alarm amplifier, never as router".

Integrity properties:

- Deterministic: same case → byte-identical ``Decision`` (the evaluator is
  pure), so two-run stability is 1.0 by construction and the transcript
  records/replays without conflict.
- Label-blind: the classifier reads the case's trajectory, contract
  reference and domain — what a production guard sees — never the
  ``ground_truth`` block, rationale or stratum.
- Fail-closed: a missing contract domain or clause raises
  ``ClassifierUnavailable`` (the runner records the error and continues);
  it never guesses a verdict.

Where the composite wins and where it is blind: inside this benchmark the
ground truth shares the evaluator, so its perfect §7 scores are an
architecture result. In production the composite is blind to anything the
trajectory does not record — exactly where a probabilistic second channel
(Jev as amplifier) remains useful.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Mapping

from autopilot.evaluator import ClauseStatus, evaluate_contract
from autopilot.jev_benchmark.classifier import (
    ClassifierClient,
    ClassifierUnavailable,
    Decision,
)
from autopilot.jev_benchmark.generator import DEFAULT_CONTRACTS_DIR, DOMAIN_SPECS
from autopilot.jev_benchmark.metrics import Verdict

COMPOSITE_VERSION = "0.1.0"

_STATUS_ROUTING: dict[ClauseStatus, tuple[Verdict, str]] = {
    ClauseStatus.failed: (Verdict.RISK, "clause_failed"),
    ClauseStatus.passed: (Verdict.SAFE, "clause_passed"),
    ClauseStatus.not_applicable: (Verdict.SAFE, "clause_not_applicable"),
    ClauseStatus.indeterminate: (
        Verdict.UNCERTAIN,
        "clause_indeterminate_human_queue",
    ),
}


class CompositeClassifier(ClassifierClient):
    """Evaluator-first routing behind the benchmark's ``ClassifierClient``.

    Parameters:
        contracts_dir: directory holding the benchmark contract YAMLs
            (defaults to ``examples/jev_benchmark``).
        triage_source: optional mapping ``case_id`` → ``{"verdict": str,
            "confidence": float}`` — typically the frozen Jev formal
            transcript. Used exclusively for ``UNCERTAIN`` rationale triage.
    """

    name = "composite_evaluator_first"

    def __init__(
        self,
        *,
        contracts_dir: Path | None = None,
        triage_source: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self._contracts_dir = contracts_dir or DEFAULT_CONTRACTS_DIR
        self._triage = dict(triage_source or {})
        self._contracts: dict[str, Any] = {}
        self.version = COMPOSITE_VERSION
        self.triage_hits = 0

    # -- protocol -----------------------------------------------------------

    def observable(self, case: Any) -> dict[str, Any]:  # noqa: ANN401 - protocol
        """Classifier-visible projection plus the deterministic routing context.

        The projection (including the audited trajectory as evidence) comes
        from ``projection.build_observable``, the shared definition the adapters
        and the trainer also use. The routing context (contract identity +
        clause id) is what a production guard is configured with; the ground
        truth block, the arbiter rationale and the stratum never appear.
        """

        from autopilot.jev_benchmark.projection import build_observable

        observable = build_observable(case)
        observable["routing_context"] = {
            "contract_id": case.contract.id,
            "contract_version": case.contract.version,
            "clause_id": case.contract.clause_id,
            "domain": case.domain,
        }
        return observable

    def classify(self, case: Any) -> Decision:  # noqa: ANN401 - protocol boundary
        contract = self._contract_for(case.domain)
        if contract is None:
            raise ClassifierUnavailable(
                f"no loadable contract for domain {case.domain!r}"
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            evaluation = evaluate_contract(contract, case.trajectory)
        clause = next(
            (
                result
                for result in evaluation.clause_results
                if result.clause_id == case.contract.clause_id
            ),
            None,
        )
        if clause is None:
            raise ClassifierUnavailable(
                f"clause {case.contract.clause_id!r} missing from evaluation"
            )
        routed = _STATUS_ROUTING.get(clause.status)
        if routed is None:
            raise ClassifierUnavailable(
                f"unroutable clause status {clause.status!r} "
                f"for {case.case_id}"
            )
        verdict, reason = routed
        rationale = f"composite:{reason}"
        if verdict is Verdict.UNCERTAIN:
            triage = self._triage.get(case.case_id)
            if triage:
                self.triage_hits += 1
                rationale += (
                    f"+jev_triage:{str(triage.get('verdict', 'unknown')).lower()}"
                    f"@{float(triage.get('confidence', 0.0)):.2f}"
                )
        return Decision(verdict=verdict, confidence=1.0, rationale=rationale)

    # -- internals ----------------------------------------------------------

    def _contract_for(self, domain: str) -> Any:  # noqa: ANN401 - lazy cache
        if domain not in self._contracts:
            spec = DOMAIN_SPECS.get(domain)
            if spec is None or not spec.get("file"):
                self._contracts[domain] = None
                return None
            from autopilot.contracts import load_contract

            try:
                self._contracts[domain] = load_contract(
                    self._contracts_dir / spec["file"]
                )
            except (OSError, ValueError):
                self._contracts[domain] = None
        return self._contracts[domain]


def triage_from_transcript(path: Path) -> dict[str, dict[str, Any]]:
    """Load a Jev transcript as the composite's triage metadata source."""

    from autopilot.jev_benchmark import load_transcript

    _header, records = load_transcript(path)
    return {
        record.case_id: {
            "verdict": record.verdict,
            "confidence": record.confidence,
        }
        for record in records
    }


__all__ = ["COMPOSITE_VERSION", "CompositeClassifier", "triage_from_transcript"]
