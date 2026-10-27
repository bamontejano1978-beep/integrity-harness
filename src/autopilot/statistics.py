"""Statistical harness for the deterministic evaluator (B0 capability C1).

The functions in this module compute a Wilson score interval for a binomial
proportion and a structured false-positive-rate (FPR) report for a cohort of
evaluated trajectories. They are intentionally free of evaluator internals so
that ``evaluator.py`` can import this module without importing ``adapter.py``.

The module is deterministic: for the same inputs it returns byte-identical
outputs. No floating-point randomness is used and no external state is read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    """Wilson score interval for a binomial proportion."""

    observed: int
    sample_size: int
    point_estimate: float
    lower_bound: float
    upper_bound: float
    confidence_level: float
    z_value: float


@dataclass(frozen=True, slots=True)
class CohortFPRReport:
    """Structured FPR report for an evaluated cohort.

    A "negative" trajectory is one that the contract designates as a true
    non-violation; a "false positive" is a negative trajectory that the
    evaluator reports as ``fail``. The Wilson upper bound is the value used
    to close viability gate §7 item 2.
    """

    negative_trajectories: int
    false_positives: int
    point_estimate: float
    upper_bound: float
    confidence_level: float
    design_fpr: float
    meets_target: bool
    target_upper_bound: float


# Common z-values for two-sided confidence intervals. Keeping a small table
# avoids a hard dependency on scipy and keeps the harness deterministic.
_Z_TABLE: dict[float, float] = {
    0.90: 1.6448536269514722,
    0.95: 1.959963984540054,
    0.99: 2.5758293035489004,
}


def z_value_for_confidence(confidence_level: float) -> float:
    """Return the z-value for a two-sided confidence level.

    Only the three standard levels (0.90, 0.95, 0.99) are accepted so that the
    harness stays deterministic and dependency-free. ``confidence_level`` must
    be one of those values exactly (no rounding) to avoid silent drift.
    """

    try:
        return _Z_TABLE[confidence_level]
    except KeyError as exc:  # pragma: no cover - exercised in tests
        raise ValueError(
            f"unsupported confidence level {confidence_level!r}; "
            "expected one of 0.90, 0.95, 0.99"
        ) from exc


def wilson_score_interval(
    observed: int,
    sample_size: int,
    *,
    confidence_level: float = 0.95,
) -> WilsonInterval:
    """Compute the Wilson score interval for a binomial proportion.

    ``observed`` is the number of "successes" (e.g. false positives) and
    ``sample_size`` is the total number of trials. ``sample_size`` must be a
    positive integer and ``observed`` must satisfy ``0 <= observed <=
    sample_size``.
    """

    if not isinstance(sample_size, int) or sample_size <= 0:
        raise ValueError("sample_size must be a positive integer")
    if not isinstance(observed, int) or observed < 0:
        raise ValueError("observed must be a non-negative integer")
    if observed > sample_size:
        raise ValueError("observed cannot exceed sample_size")

    z = z_value_for_confidence(confidence_level)
    n = float(sample_size)
    p_hat = observed / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denominator
    margin = (z / denominator) * math.sqrt(
        (p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n
    )
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return WilsonInterval(
        observed=observed,
        sample_size=sample_size,
        point_estimate=p_hat,
        lower_bound=lower,
        upper_bound=upper,
        confidence_level=confidence_level,
        z_value=z,
    )


def false_positive_rate_report(
    *,
    negative_trajectories: int,
    false_positives: int,
    design_fpr: float,
    target_upper_bound: float = 0.05,
    confidence_level: float = 0.95,
) -> CohortFPRReport:
    """Build an FPR report from a cohort of evaluated negative trajectories.

    ``negative_trajectories`` is the number of trajectories in the cohort that
    are *known* to be non-violating (ground truth negative). ``false_positives``
    is the subset that the evaluator marked as ``fail``. ``design_fpr`` is the
    target design-level FPR (e.g. ``0.02``) — it is recorded in the report for
    auditability but does not affect the Wilson bound calculation.

    The returned report is deterministic given the inputs. ``meets_target`` is
    true when the Wilson upper bound is less than or equal to
    ``target_upper_bound``.
    """

    if not isinstance(negative_trajectories, int) or negative_trajectories <= 0:
        raise ValueError("negative_trajectories must be a positive integer")
    if not isinstance(false_positives, int) or false_positives < 0:
        raise ValueError("false_positives must be a non-negative integer")
    if false_positives > negative_trajectories:
        raise ValueError("false_positives cannot exceed negative_trajectories")
    if not 0.0 <= design_fpr <= 1.0:
        raise ValueError("design_fpr must lie in [0.0, 1.0]")
    if not 0.0 < target_upper_bound <= 1.0:
        raise ValueError("target_upper_bound must lie in (0.0, 1.0]")

    interval = wilson_score_interval(
        false_positives,
        negative_trajectories,
        confidence_level=confidence_level,
    )
    return CohortFPRReport(
        negative_trajectories=negative_trajectories,
        false_positives=false_positives,
        point_estimate=interval.point_estimate,
        upper_bound=interval.upper_bound,
        confidence_level=confidence_level,
        design_fpr=design_fpr,
        meets_target=interval.upper_bound <= target_upper_bound,
        target_upper_bound=target_upper_bound,
    )


__all__ = [
    "CohortFPRReport",
    "WilsonInterval",
    "false_positive_rate_report",
    "wilson_score_interval",
    "z_value_for_confidence",
]
