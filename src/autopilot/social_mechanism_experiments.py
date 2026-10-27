"""Deterministic mechanism simulations for the Ringelmann and Sherif protocols.

These are explicit toy policies, not psychological replications or LLM tests.
"""

from __future__ import annotations

import hashlib
import json
from math import sqrt
from statistics import mean, pstdev, stdev
from typing import Any

from autopilot.social_swarm import unit_interval

RINGELMANN_GROUP_SIZES = (1, 2, 4, 8)
RINGELMANN_ACCOUNTABILITY_BONUS = 0.5
RINGELMANN_COORDINATION_LOSS_PER_MEMBER = 0.04
SHERIF_AGENTS = 4
SHERIF_ALPHA = 0.30
SHERIF_PRE_TRIALS = 5
SHERIF_INTERACTION_ROUNDS = 10
SHERIF_PERSISTENCE_TRIALS = 5
SHERIF_REFERENCE = 50.0
SHERIF_PRIVATE_NOISE_HALF_RANGE = 10.0
SHERIF_UPDATE_NOISE_HALF_RANGE = 0.2
SHERIF_EXPOSURE_VALUES = 3


def _uniform(seed: str, *parts: object) -> float:
    return unit_interval(seed, *parts)


def _average(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _sd(values: list[float]) -> float:
    return pstdev(values) if len(values) > 1 else 0.0


def _mean_ci95(values: list[float]) -> dict[str, float]:
    """Normal-approximation interval over independent team-level observations."""
    center = _average(values)
    margin = 1.96 * stdev(values) / sqrt(len(values)) if len(values) > 1 else 0.0
    return {"mean": center, "lower_95": center - margin, "upper_95": center + margin}


def run_ringelmann(
    seeds: int,
    *,
    group_sizes: tuple[int, ...] = RINGELMANN_GROUP_SIZES,
    accountability_bonus: float = RINGELMANN_ACCOUNTABILITY_BONUS,
    coordination_loss_per_member: float = RINGELMANN_COORDINATION_LOSS_PER_MEMBER,
    seed_prefix: str = "ringelmann-v0.1",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run paired team-size/attribution cells; returns summaries and team rows."""
    if seeds < 1:
        raise ValueError("seeds must be positive")
    if not group_sizes or any(n < 1 for n in group_sizes):
        raise ValueError("every group size must be at least 1")
    max_n = max(max(group_sizes), 8)
    rows: list[dict[str, Any]] = []
    for seed_index in range(seeds):
        seed = f"{seed_prefix}-{seed_index:06d}"
        costs = [0.9 + 0.2 * _uniform(seed, "cost", i) for i in range(max_n)]
        for n in group_sizes:
            for attributable in (False, True):
                efforts = []
                for i in range(n):
                    reward_marginal = 1.0 / n
                    if attributable and n > 1:
                        reward_marginal += accountability_bonus
                    # Maximizer of reward_marginal*e - cost*e^2/2 on [0, 1].
                    effort = min(1.0, max(0.0, reward_marginal / costs[i]))
                    efforts.append(effort)
                coordination_efficiency = max(
                    0.0, 1.0 - coordination_loss_per_member * (n - 1)
                )
                additive_output = sum(efforts)
                rows.append(
                    {
                        "seed": seed,
                        "team_size": n,
                        "individual_attribution_visible": attributable,
                        "cost_coefficients": costs[:n],
                        "individual_efforts": efforts,
                        "mean_effort": _average(efforts),
                        "additive_output": additive_output,
                        "synchronized_output": additive_output * coordination_efficiency,
                        "coordination_efficiency_proxy": coordination_efficiency,
                    }
                )
    summaries: list[dict[str, Any]] = []
    for n in group_sizes:
        for attributable in (False, True):
            cell = [
                r
                for r in rows
                if r["team_size"] == n and r["individual_attribution_visible"] == attributable
            ]
            summaries.append(
                {
                    "team_size": n,
                    "individual_attribution_visible": attributable,
                    "teams": len(cell),
                    "mean_individual_effort": _average([r["mean_effort"] for r in cell]),
                    "mean_individual_effort_ci95": _mean_ci95([r["mean_effort"] for r in cell]),
                    "mean_additive_output": _average([r["additive_output"] for r in cell]),
                    "mean_additive_output_per_agent": _average(
                        [r["additive_output"] / n for r in cell]
                    ),
                    "coordination_efficiency_proxy": cell[0]["coordination_efficiency_proxy"],
                    "mean_synchronized_output": _average([r["synchronized_output"] for r in cell]),
                }
            )
    baseline_n = 1 if 1 in group_sizes else group_sizes[0]
    baseline = {
        row["seed"]: row["mean_effort"]
        for row in rows
        if row["team_size"] == baseline_n and not row["individual_attribution_visible"]
    }
    for summary in summaries:
        cell = [
            row
            for row in rows
            if row["team_size"] == summary["team_size"]
            and row["individual_attribution_visible"] == summary["individual_attribution_visible"]
        ]
        paired_changes = [row["mean_effort"] - baseline[row["seed"]] for row in cell]
        summary["paired_effort_change_vs_n1_low_visibility_ci95"] = _mean_ci95(paired_changes)

    return summaries, rows


def _clamp(value: float) -> float:
    return min(100.0, max(0.0, value))


def _noise(seed: str, *parts: object, half_range: float) -> float:
    return (_uniform(seed, *parts) - 0.5) * 2.0 * half_range


def _append_chained_event(channel: list[dict[str, Any]], event: dict[str, Any]) -> None:
    previous = channel[-1]["entry_sha256"] if channel else None
    canonical = json.dumps(
        {"seq": len(channel) + 1, "previous_sha256": previous, "event": event},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    channel.append(
        {
            "seq": len(channel) + 1,
            "previous_sha256": previous,
            **event,
            "entry_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    )


def run_sherif(
    seeds: int,
    *,
    agents: int = SHERIF_AGENTS,
    alpha: float = SHERIF_ALPHA,
    pre_trials: int = SHERIF_PRE_TRIALS,
    interaction_rounds: int = SHERIF_INTERACTION_ROUNDS,
    persistence_trials: int = SHERIF_PERSISTENCE_TRIALS,
    reference: float = SHERIF_REFERENCE,
    private_noise_half_range: float = SHERIF_PRIVATE_NOISE_HALF_RANGE,
    update_noise_half_range: float = SHERIF_UPDATE_NOISE_HALF_RANGE,
    exposure_values: int = SHERIF_EXPOSURE_VALUES,
    seed_prefix: str = "sherif-v0.1",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run matched four-agent estimation teams under three exposure regimes."""
    if seeds < 1:
        raise ValueError("seeds must be positive")
    if agents < 2:
        raise ValueError("agents must be at least 2")
    if interaction_rounds < 1:
        raise ValueError("interaction_rounds must be at least 1")
    conditions = ("solo", "group_visible", "independent_numeric_exposure")
    rows: list[dict[str, Any]] = []
    for seed_index in range(seeds):
        seed = f"{seed_prefix}-{seed_index:06d}"
        initial: list[list[float]] = []
        for agent in range(agents):
            initial.append(
                [
                    _clamp(
                        reference
                        + _noise(
                            seed,
                            "private",
                            agent,
                            trial,
                            half_range=private_noise_half_range,
                        )
                    )
                    for trial in range(pre_trials)
                ]
            )
        initial_state = [_average(values) for values in initial]
        # Each agent receives its own independent numeric references;
        # values are not generated from other agents' responses.
        external_values = [
            [
                _clamp(
                    reference
                    + _noise(
                        seed,
                        "external",
                        agent,
                        item,
                        half_range=private_noise_half_range,
                    )
                )
                for item in range(exposure_values)
            ]
            for agent in range(agents)
        ]
        for condition in conditions:
            state = initial_state.copy()
            interaction: list[list[float]] = []
            social_channel: list[dict[str, Any]] = []
            latest_reported = initial_state.copy()
            for round_index in range(interaction_rounds):
                # Fixed turn order; each agent sees the latest posted estimate
                # from each peer and updates before publishing its own response.
                for agent in range(agents):
                    value = state[agent]
                    visible_peers: list[dict[str, float | int]] = []
                    visible_numeric: list[float] = []
                    if condition == "group_visible":
                        visible_peers = [
                            {"agent_id": peer, "estimate": latest_reported[peer]}
                            for peer in range(agents)
                            if peer != agent
                        ]
                        target = _average([item["estimate"] for item in visible_peers])
                        value = (1.0 - alpha) * value + alpha * target
                    elif condition == "independent_numeric_exposure":
                        visible_numeric = external_values[agent]
                        target = _average(visible_numeric)
                        value = (1.0 - alpha) * value + alpha * target
                    value += _noise(
                        seed,
                        "interaction-update",
                        agent,
                        round_index,
                        half_range=update_noise_half_range,
                    )
                    state[agent] = _clamp(value)
                    latest_reported[agent] = state[agent]
                    _append_chained_event(
                        social_channel,
                        {
                            "round": round_index + 1,
                            "agent_id": agent,
                            "condition": condition,
                            "visible_peer_estimates": visible_peers,
                            "visible_independent_numeric_values": visible_numeric,
                            "reported_estimate": state[agent],
                        },
                    )
                interaction.append(state.copy())
            final_center = _average(state)
            post: list[list[float]] = []
            for agent in range(agents):
                values = []
                value = state[agent]
                for trial in range(persistence_trials):
                    value = _clamp(
                        value
                        + _noise(
                            seed,
                            "persistence-update",
                            agent,
                            trial,
                            half_range=update_noise_half_range,
                        )
                    )
                    values.append(value)
                post.append(values)
            post_state = [_average(values) for values in post]
            rows.append(
                {
                    "seed": seed,
                    "condition": condition,
                    "analyst_reference_only": reference,
                    "pre_estimates": initial,
                    "initial_private_means": initial_state,
                    "interaction_estimates_by_round": interaction,
                    "final_interaction_estimates": state,
                    "final_team_center": final_center,
                    "post_private_estimates": post,
                    "post_private_means": post_state,
                    "pre_dispersion": _sd(initial_state),
                    "final_dispersion": _sd(state),
                    "post_dispersion": _sd(post_state),
                    "final_center_error": abs(final_center - reference),
                    "post_mean_distance_to_prior_center": _average(
                        [abs(x - final_center) for x in post_state]
                    ),
                    "pre_mean_distance_to_prior_center": _average(
                        [abs(x - final_center) for x in initial_state]
                    ),
                    "external_numeric_values_by_agent": external_values
                    if condition == "independent_numeric_exposure"
                    else None,
                    "social_channel": social_channel,
                }
            )
    summaries: list[dict[str, Any]] = []
    for condition in conditions:
        cell = [r for r in rows if r["condition"] == condition]
        summaries.append(
            {
                "condition": condition,
                "teams": len(cell),
                "mean_pre_dispersion": _average([r["pre_dispersion"] for r in cell]),
                "mean_pre_dispersion_ci95": _mean_ci95([r["pre_dispersion"] for r in cell]),
                "mean_final_dispersion": _average([r["final_dispersion"] for r in cell]),
                "mean_final_dispersion_ci95": _mean_ci95([r["final_dispersion"] for r in cell]),
                "mean_post_dispersion": _average([r["post_dispersion"] for r in cell]),
                "mean_final_center_error": _average([r["final_center_error"] for r in cell]),
                "mean_pre_distance_to_prior_center": _average(
                    [r["pre_mean_distance_to_prior_center"] for r in cell]
                ),
                "mean_post_distance_to_prior_center": _average(
                    [r["post_mean_distance_to_prior_center"] for r in cell]
                ),
                "dispersion_by_interaction_round": [
                    _average([_sd(r["interaction_estimates_by_round"][turn]) for r in cell])
                    for turn in range(interaction_rounds)
                ],
            }
        )
    by_seed_condition = {(row["seed"], row["condition"]): row for row in rows}
    for summary in summaries:
        condition = summary["condition"]
        for control in ("solo", "independent_numeric_exposure"):
            dispersion_delta = [
                by_seed_condition[(f"{seed_prefix}-{index:06d}", condition)]["final_dispersion"]
                - by_seed_condition[(f"{seed_prefix}-{index:06d}", control)]["final_dispersion"]
                for index in range(seeds)
            ]
            persistence_delta = [
                by_seed_condition[(f"{seed_prefix}-{index:06d}", condition)][
                    "post_mean_distance_to_prior_center"
                ]
                - by_seed_condition[(f"{seed_prefix}-{index:06d}", control)][
                    "post_mean_distance_to_prior_center"
                ]
                for index in range(seeds)
            ]
            summary[f"paired_final_dispersion_difference_vs_{control}_ci95"] = _mean_ci95(
                dispersion_delta
            )
            summary[f"paired_persistence_distance_difference_vs_{control}_ci95"] = _mean_ci95(
                persistence_delta
            )
        own_phase_delta = [
            row["post_mean_distance_to_prior_center"] - row["pre_mean_distance_to_prior_center"]
            for row in rows
            if row["condition"] == condition
        ]
        summary["paired_post_minus_pre_distance_to_prior_center_ci95"] = _mean_ci95(own_phase_delta)
    return summaries, rows
