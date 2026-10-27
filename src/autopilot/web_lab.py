"""Web Laboratory engine, static bundle builder, and local HTTP server.

Provides a unified workbench for visualizing and executing:
- Social mechanism simulations: Ringelmann and Sherif (with parameter controls,
  confidence intervals, round-by-round trajectories, and sample team traces).
- Swarm paradigms over the mediated N-agent channel: Asch conformity,
  Bystander diffusion / pluralistic ignorance, and Informational Cascade.
- Swarm contract v0.2 evaluation: mediated pairs, system verdict, composition
  rejection, and the hash-chained channel trace behind every finding.
- Core assurance services: Contract & Trajectory Evaluator, Live Clause Canaries,
  and Portable SHA-256 Audit Ledger Verification.

Works in two modes:
1. Static / offline mode via the precomputed bundle in ``lab/assets/js/lab-bundle.js``.
2. Live local workbench mode via ``scripts/serve_web_lab.py`` exposing ``/api/lab/*``
   and ``/api/canary/*``.

Every executed run carries an ``artifact_sha256`` digest, and the live server can
seal it (``POST /api/lab/sign``) or check a seal (``POST /api/lab/verify``) with
its local Ed25519 laboratory key (``lab_evidence.py``).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from autopilot.adapter import load_adapter_config
from autopilot.asch import AschDesign, asch_topology, run_asch_sweep, run_asch_trial
from autopilot.bystander import (
    BystanderDesign,
    bystander_contract,
    run_bystander_sweep,
    run_bystander_trial,
)
from autopilot.canary_live import run_live_clause_comparison
from autopilot.cascade import (
    CascadeDesign,
    cascade_contract,
    run_cascade_sweep,
    run_cascade_trial,
)
from autopilot.clause_canaries import DEFAULT_REGISTRY, CanaryError, load_registry
from autopilot.contracts import load_contract
from autopilot.evaluator import evaluate_contract
from autopilot.lab_evidence import (
    LabSignature,
    artifact_digest,
    attach_artifact_digest,
    default_key_dir,
    sign_lab_artifact,
    signing_key_info,
    verify_lab_artifact,
)
from autopilot.lab_index import LabRejectedPackage, build_run_index
from autopilot.lab_ops import LabAccessLog, LabBasicAuth
from autopilot.lab_package import (
    LabPackage,
    build_lab_package,
    subject_kind_for,
    verify_lab_package,
)
from autopilot.lab_study import build_lab_study, verify_lab_study
from pydantic import ValidationError
from autopilot.lab_provenance import (
    ORIGIN_LIVE_EVALUATION,
    ORIGIN_LIVE_RUN,
    LabArtifactRegistry,
    register_bundle_artifacts,
)
from autopilot.social_mechanism_experiments import (
    RINGELMANN_ACCOUNTABILITY_BONUS,
    RINGELMANN_COORDINATION_LOSS_PER_MEMBER,
    RINGELMANN_GROUP_SIZES,
    SHERIF_AGENTS,
    SHERIF_ALPHA,
    SHERIF_EXPOSURE_VALUES,
    SHERIF_INTERACTION_ROUNDS,
    SHERIF_PERSISTENCE_TRIALS,
    SHERIF_PRE_TRIALS,
    SHERIF_PRIVATE_NOISE_HALF_RANGE,
    SHERIF_REFERENCE,
    SHERIF_UPDATE_NOISE_HALF_RANGE,
    run_ringelmann,
    run_sherif,
)
from autopilot.swarm import load_swarm_pair, run_swarm_pair
from autopilot.swarm_contract import load_swarm_contract
from autopilot.swarm_evaluator import evaluate_swarm_contract
from autopilot.trajectory import load_trajectory

LAB_SCHEMA_VERSION = "0.1"
CLAIM_BOUNDARY_NOTE = (
    "Outputs describe explicit deterministic simulation policies and contract evaluators only; "
    "no inference about human psychology or live LLM behavior."
)

EVALUATOR_PROFILES: tuple[dict[str, str], ...] = (
    {
        "key": "seguros",
        "name": "Seguros (Reclamaciones)",
        "contract": "examples/insurance_claims_v0.1.yaml",
        "pass_trajectory": "examples/insurance_claims_trajectory_pass_v0.1.yaml",
        "fail_trajectory": "examples/insurance_claims_trajectory_fail_v0.1.yaml",
        "summary": "Pago sin autorización y divulgación cruzada de pólizas",
    },
    {
        "key": "banca",
        "name": "Banca y Pagos (PSD2 SCA)",
        "contract": "examples/banking_psd2_sca_v0.1.yaml",
        "pass_trajectory": "examples/banking_psd2_sca_trajectory_pass_v0.1.yaml",
        "fail_trajectory": "examples/banking_psd2_sca_trajectory_fail_missing_sca_v0.1.yaml",
        "summary": "Pago iniciado sin autenticación reforzada ni vínculo dinámico",
    },
    {
        "key": "salud",
        "name": "Salud (RGPD Datos Clínicos)",
        "contract": "examples/healthcare_gdpr_health_data_v0.1.yaml",
        "pass_trajectory": "examples/healthcare_gdpr_health_data_trajectory_pass_v0.1.yaml",
        "fail_trajectory": "examples/healthcare_gdpr_health_data_trajectory_fail_no_consent_v0.1.yaml",
        "summary": "Registro de datos clínicos sin consentimiento explícito",
    },
    {
        "key": "administracion",
        "name": "Administración Pública (Transparencia)",
        "contract": "examples/public_admin_transparency_v0.1.yaml",
        "pass_trajectory": "examples/public_admin_transparency_trajectory_pass_v0.1.yaml",
        "fail_trajectory": "examples/public_admin_transparency_trajectory_fail_suppress_v0.1.yaml",
        "summary": "Supresión indebida de informe protegido sin base legal",
    },
)


SWARM_AGENT_CONTRACT = "examples/insurance_claims_v0.1.yaml"
SWARM_ADAPTER = "examples/insurance_claims_swarm_adapter_v0.1.yaml"
SWARM_CONTRACT = "examples/insurance_claims_swarm_contract_v0.2.yaml"
SWARM_PAIR_PRESETS: tuple[dict[str, str], ...] = (
    {
        "key": "contagion",
        "name": "Par de contagio · pago interagente",
        "pair_path": "examples/insurance_claims_swarm_pair_contagion_v0.1.yaml",
        "expectation": "fail",
        "summary": (
            "El sujeto conforme en aislamiento recibe la señal de pago del par y abre una "
            "delegación mediada: falla sólo dentro de la composición."
        ),
    },
    {
        "key": "control",
        "name": "Par de control · reclamación benigna",
        "pair_path": "examples/insurance_claims_swarm_pair_control_v0.1.yaml",
        "expectation": "pass",
        "summary": (
            "Dos agentes conformes sobre un caso benigno: el contrato pasa y las cláusulas "
            "sin participantes se declaran dormidas en lugar de omitirse."
        ),
    },
)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_ringelmann_lab(
    *,
    seeds: int = 200,
    group_sizes: tuple[int, ...] = RINGELMANN_GROUP_SIZES,
    accountability_bonus: float = RINGELMANN_ACCOUNTABILITY_BONUS,
    coordination_loss_per_member: float = RINGELMANN_COORDINATION_LOSS_PER_MEMBER,
    seed_prefix: str = "ringelmann-v0.1",
) -> dict[str, Any]:
    """Execute Ringelmann simulation and return summaries + representative sample runs."""
    t0 = time.perf_counter()
    summaries, rows = run_ringelmann(
        seeds,
        group_sizes=group_sizes,
        accountability_bonus=accountability_bonus,
        coordination_loss_per_member=coordination_loss_per_member,
        seed_prefix=seed_prefix,
    )
    first_seed = f"{seed_prefix}-{0:06d}"
    sample_runs = [row for row in rows if row["seed"] == first_seed]
    params = {
        "seeds": seeds,
        "group_sizes": list(group_sizes),
        "accountability_bonus": accountability_bonus,
        "coordination_loss_per_member": coordination_loss_per_member,
        "seed_prefix": seed_prefix,
    }
    return attach_artifact_digest({
        "experiment": "ringelmann",
        "experiment_kind": "deterministic_mechanism_simulation",
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "parameters": params,
        "parameters_sha256": _canonical_sha256(params),
        "total_runs": len(rows),
        "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "summaries": summaries,
        "sample_seed": first_seed,
        "sample_runs": sample_runs,
    })


def run_sherif_lab(
    *,
    seeds: int = 200,
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
) -> dict[str, Any]:
    """Execute Sherif norm-formation simulation and return summaries + sample team traces."""
    t0 = time.perf_counter()
    summaries, rows = run_sherif(
        seeds,
        agents=agents,
        alpha=alpha,
        pre_trials=pre_trials,
        interaction_rounds=interaction_rounds,
        persistence_trials=persistence_trials,
        reference=reference,
        private_noise_half_range=private_noise_half_range,
        update_noise_half_range=update_noise_half_range,
        exposure_values=exposure_values,
        seed_prefix=seed_prefix,
    )
    first_seed = f"{seed_prefix}-{0:06d}"
    sample_runs = [row for row in rows if row["seed"] == first_seed]
    params = {
        "seeds": seeds,
        "agents": agents,
        "alpha": alpha,
        "pre_trials": pre_trials,
        "interaction_rounds": interaction_rounds,
        "persistence_trials": persistence_trials,
        "reference": reference,
        "private_noise_half_range": private_noise_half_range,
        "update_noise_half_range": update_noise_half_range,
        "exposure_values": exposure_values,
        "seed_prefix": seed_prefix,
    }
    return attach_artifact_digest({
        "experiment": "sherif",
        "experiment_kind": "deterministic_mechanism_simulation",
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "parameters": params,
        "parameters_sha256": _canonical_sha256(params),
        "total_runs": len(rows),
        "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "summaries": summaries,
        "sample_seed": first_seed,
        "sample_runs": sample_runs,
    })


def run_asch_lab(
    *,
    trials_per_group: int = 200,
    k_values: tuple[int, ...] = (1, 2, 3, 4, 6, 8),
    seed: str = "asch-v0.1",
    sample_majority_size: int = 4,
    sample_run_index: int = 1,
) -> dict[str, Any]:
    """Run Asch conformity sweeps (unanimous vs dissenter) and sample channel traces."""
    t0 = time.perf_counter()
    design_unanimous = AschDesign(seed=seed, dissenter=False)
    design_dissenter = AschDesign(seed=seed, dissenter=True)
    sweep_unanimous = run_asch_sweep(design_unanimous, k_values, trials_per_group)
    sweep_dissenter = run_asch_sweep(design_dissenter, k_values, trials_per_group)

    sample_k = sample_majority_size if sample_majority_size in k_values else k_values[min(2, len(k_values) - 1)]
    trial_unanimous = run_asch_trial(design_unanimous, sample_k, sample_run_index)
    trial_dissenter = run_asch_trial(design_dissenter, sample_k, sample_run_index)

    return attach_artifact_digest({
        "experiment": "asch",
        "experiment_kind": "mediated_channel_mechanism_simulation",
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "parameters": {
            "seed": seed,
            "trials_per_group": trials_per_group,
            "k_values": list(k_values),
            "sample_majority_size": sample_k,
            "sample_run_index": sample_run_index,
        },
        "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "sweeps": {
            "unanimous": sweep_unanimous.model_dump(mode="json"),
            "dissenter": sweep_dissenter.model_dump(mode="json"),
        },
        "sample_trials": {
            "unanimous": trial_unanimous.model_dump(mode="json"),
            "dissenter": trial_dissenter.model_dump(mode="json"),
        },
        "topology": asch_topology(sample_k).model_dump(mode="json"),
    })


def run_bystander_lab(
    *,
    trials_per_group: int = 200,
    n_values: tuple[int, ...] = (1, 2, 4, 8),
    responsibility_weight: float = 0.85,
    noticing_probability: float = 0.95,
    seed: str = "bystander-v0.1",
    sample_n: int = 4,
    sample_run_index: int = 1,
) -> dict[str, Any]:
    """Run Bystander sweeps (diffusion vs pluralistic ignorance) and sample traces."""
    t0 = time.perf_counter()
    design_diffusion = BystanderDesign(
        seed=seed,
        responsibility_weight=responsibility_weight,
        noticing_probability=noticing_probability,
        mechanism="diffusion",
    )
    design_ignorance = BystanderDesign(
        seed=seed,
        responsibility_weight=responsibility_weight,
        noticing_probability=noticing_probability,
        mechanism="pluralistic_ignorance",
    )
    sweep_diffusion = run_bystander_sweep(design_diffusion, n_values, trials_per_group)
    sweep_ignorance = run_bystander_sweep(design_ignorance, n_values, trials_per_group)

    chosen_n = sample_n if sample_n in n_values else n_values[-1]
    trial_diffusion = run_bystander_trial(design_diffusion, chosen_n, sample_run_index)
    trial_ignorance = run_bystander_trial(design_ignorance, chosen_n, sample_run_index)

    return attach_artifact_digest({
        "experiment": "bystander",
        "experiment_kind": "mediated_channel_mechanism_simulation",
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "parameters": {
            "seed": seed,
            "trials_per_group": trials_per_group,
            "n_values": list(n_values),
            "responsibility_weight": responsibility_weight,
            "noticing_probability": noticing_probability,
            "sample_n": chosen_n,
            "sample_run_index": sample_run_index,
        },
        "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "sweeps": {
            "diffusion": sweep_diffusion.model_dump(mode="json"),
            "pluralistic_ignorance": sweep_ignorance.model_dump(mode="json"),
        },
        "sample_trials": {
            "diffusion": trial_diffusion.model_dump(mode="json"),
            "pluralistic_ignorance": trial_ignorance.model_dump(mode="json"),
        },
        "contract": bystander_contract(chosen_n).model_dump(mode="json"),
    })


def run_cascade_lab(
    *,
    trials_per_group: int = 200,
    n_values: tuple[int, ...] = (1, 2, 4, 8, 12),
    signal_accuracy: float = 0.70,
    case_requires_review: bool = True,
    seed: str = "cascade-v0.1",
    sample_n: int = 8,
    sample_run_index: int = 1,
) -> dict[str, Any]:
    """Run Informational Cascade sweep and sample sequential trace."""
    t0 = time.perf_counter()
    design = CascadeDesign(
        seed=seed,
        signal_accuracy=signal_accuracy,
        case_requires_review=case_requires_review,
    )
    sweep = run_cascade_sweep(design, n_values, trials_per_group)
    chosen_n = sample_n if sample_n in n_values else n_values[-1]
    trial = run_cascade_trial(design, chosen_n, sample_run_index)

    # Also compute a contrast regime (clear vs ambiguous signal) for rich comparison
    contrast_accuracy = 0.85 if abs(signal_accuracy - 0.85) > 0.05 else 0.60
    contrast_design = CascadeDesign(
        seed=seed,
        signal_accuracy=contrast_accuracy,
        case_requires_review=case_requires_review,
    )
    contrast_sweep = run_cascade_sweep(contrast_design, n_values, trials_per_group)

    return attach_artifact_digest({
        "experiment": "cascade",
        "experiment_kind": "mediated_channel_mechanism_simulation",
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "parameters": {
            "seed": seed,
            "trials_per_group": trials_per_group,
            "n_values": list(n_values),
            "signal_accuracy": signal_accuracy,
            "contrast_accuracy": contrast_accuracy,
            "case_requires_review": case_requires_review,
            "sample_n": chosen_n,
            "sample_run_index": sample_run_index,
        },
        "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "sweep": sweep.model_dump(mode="json"),
        "contrast_sweep": contrast_sweep.model_dump(mode="json"),
        "sample_trial": trial.model_dump(mode="json"),
        "contract": cascade_contract(chosen_n).model_dump(mode="json"),
    })


def run_swarm_lab(
    root: Path,
    *,
    pairs: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Execute the mediated swarm pairs and evaluate the v0.2 swarm contract.

    Runs the real pair runner over the insurance-claims examples (no network,
    no LLM): two scripted agents exchange traffic through the mediated channel,
    each member is evaluated in isolation and paired, and the system-level
    verdict cites the channel entry that produced every finding.
    """
    t0 = time.perf_counter()
    preset_keys = [preset["key"] for preset in SWARM_PAIR_PRESETS]
    requested = list(pairs) if pairs else preset_keys
    unknown = [key for key in requested if key not in preset_keys]
    if unknown:
        raise ValueError(f"Unsupported swarm pair: {', '.join(unknown)}")
    selected = [key for key in preset_keys if key in requested]

    agent_contract = load_contract(root / SWARM_AGENT_CONTRACT)
    adapter = load_adapter_config(root / SWARM_ADAPTER)
    swarm_contract = load_swarm_contract(root / SWARM_CONTRACT)
    contract_digest = _canonical_sha256(swarm_contract.model_dump(mode="json"))

    runs: dict[str, dict[str, Any]] = {}
    for preset in SWARM_PAIR_PRESETS:
        key = preset["key"]
        if key not in selected:
            continue
        pair, base_dir = load_swarm_pair(root / preset["pair_path"])
        execution = run_swarm_pair(agent_contract, adapter, pair, base_dir)
        report = execution.report
        verdict = evaluate_swarm_contract(
            swarm_contract, execution.run, report.members
        )

        attribution: dict[str, list[str]] = {}
        for violation in verdict.violations:
            if violation.entry_seq is None:
                continue
            bucket = attribution.setdefault(str(violation.entry_seq), [])
            if violation.rule_id not in bucket:
                bucket.append(violation.rule_id)

        runs[key] = {
            "pair_key": key,
            "pair_name": preset["name"],
            "pair_path": preset["pair_path"],
            "summary": preset["summary"],
            "expectation": preset["expectation"],
            "pair_id": report.swarm_id,
            "report_id": report.report_id,
            "scenario_id": report.scenario_id,
            "channel_entries": report.channel_entries,
            "rounds_completed": report.rounds_completed,
            "delegation_rounds_used": report.delegation_rounds_used,
            "contagion_detected": report.contagion_detected,
            "contagion_clause_ids": list(report.contagion_clause_ids),
            "members": [member.model_dump(mode="json") for member in report.members],
            "system_verdict": verdict.model_dump(mode="json"),
            "channel": [
                entry.model_dump(mode="json") for entry in execution.run.channel
            ],
            "citation_by_seq": attribution,
        }

    statuses = {key: run["system_verdict"]["status"] for key, run in runs.items()}
    differential = []
    for key, run in runs.items():
        subject = next(
            (m for m in run["members"] if m.get("role") == "subject"),
            run["members"][0] if run["members"] else {},
        )
        differential.append(
            {
                "pair_key": key,
                "source_id": subject.get("source_id", ""),
                "isolated_status": subject.get("isolated_status", ""),
                "paired_status": subject.get("paired_status", ""),
                "contagion_clause_ids": subject.get("contagion_clause_ids", []),
                "contagion_index": subject.get("contagion_index", 0.0),
            }
        )

    params = {
        "agent_contract": SWARM_AGENT_CONTRACT,
        "adapter": SWARM_ADAPTER,
        "swarm_contract": SWARM_CONTRACT,
        "pairs": selected,
    }
    return attach_artifact_digest({
        "experiment": "swarm",
        "experiment_kind": "mediated_channel_composition_evaluation",
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "parameters": params,
        "parameters_sha256": _canonical_sha256(params),
        "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "contract": {
            "swarm_contract_id": swarm_contract.swarm_contract_id,
            "schema_version": swarm_contract.schema_version,
            "contract_sha256": contract_digest,
            "topology": {
                "nodes": list(swarm_contract.topology.nodes),
                "edges": [
                    {"desde": edge.desde, "hacia": edge.hacia}
                    for edge in swarm_contract.topology.edges
                ],
            },
            "clauses": [
                {"clause_id": clause.clause_id, "tipo": clause.tipo}
                for clause in swarm_contract.clauses
            ],
            "invariants": [
                {"invariant_id": inv.invariant_id, "kind": inv.kind}
                for inv in swarm_contract.invariants
            ],
        },
        "pairs": runs,
        "summary": {
            "runs": len(runs),
            "statuses": statuses,
            "composition_rejected": any(
                run["system_verdict"]["composition_rejected"] for run in runs.values()
            ),
            "channel_entries": sum(run["channel_entries"] for run in runs.values()),
            "cited_entry_seqs": {
                key: sorted(run["citation_by_seq"], key=int)
                for key, run in runs.items()
            },
            "subject_differential": differential,
        },
    })


def compare_lab_runs(
    experiment: str,
    params_a: dict[str, Any] | None = None,
    params_b: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run two parameter configurations (Run A and Run B) in parallel for direct comparison."""
    pa = params_a or {}
    pb = params_b or {}
    exp = experiment.strip().lower()

    if exp == "ringelmann":
        run_a = run_ringelmann_lab(
            seeds=int(pa.get("seeds", 200)),
            accountability_bonus=float(
                pa.get("accountability_bonus", RINGELMANN_ACCOUNTABILITY_BONUS)
            ),
            coordination_loss_per_member=float(
                pa.get("coordination_loss", RINGELMANN_COORDINATION_LOSS_PER_MEMBER)
            ),
        )
        run_b = run_ringelmann_lab(
            seeds=int(pb.get("seeds", 200)),
            accountability_bonus=float(pb.get("accountability_bonus", 0.80)),
            coordination_loss_per_member=float(pb.get("coordination_loss", 0.02)),
        )
        deltas = []
        by_key_a = {
            (s["team_size"], s["individual_attribution_visible"]): s
            for s in run_a["summaries"]
        }
        for sb in run_b["summaries"]:
            key = (sb["team_size"], sb["individual_attribution_visible"])
            sa = by_key_a.get(key, {})
            deltas.append(
                {
                    "team_size": sb["team_size"],
                    "individual_attribution_visible": sb[
                        "individual_attribution_visible"
                    ],
                    "effort_a": sa.get("mean_individual_effort", 0.0),
                    "effort_b": sb.get("mean_individual_effort", 0.0),
                    "delta_effort": round(
                        sb.get("mean_individual_effort", 0.0)
                        - sa.get("mean_individual_effort", 0.0),
                        6,
                    ),
                    "synchronized_output_a": sa.get("mean_synchronized_output", 0.0),
                    "synchronized_output_b": sb.get("mean_synchronized_output", 0.0),
                    "delta_synchronized_output": round(
                        sb.get("mean_synchronized_output", 0.0)
                        - sa.get("mean_synchronized_output", 0.0),
                        6,
                    ),
                }
            )
    elif exp == "sherif":
        run_a = run_sherif_lab(
            seeds=int(pa.get("seeds", 200)),
            alpha=float(pa.get("alpha", SHERIF_ALPHA)),
            interaction_rounds=int(
                pa.get("interaction_rounds", SHERIF_INTERACTION_ROUNDS)
            ),
            update_noise_half_range=float(
                pa.get("update_noise", SHERIF_UPDATE_NOISE_HALF_RANGE)
            ),
        )
        run_b = run_sherif_lab(
            seeds=int(pb.get("seeds", 200)),
            alpha=float(pb.get("alpha", 0.60)),
            interaction_rounds=int(
                pb.get("interaction_rounds", SHERIF_INTERACTION_ROUNDS)
            ),
            update_noise_half_range=float(
                pb.get("update_noise", SHERIF_UPDATE_NOISE_HALF_RANGE)
            ),
        )
        deltas = []
        by_cond_a = {s["condition"]: s for s in run_a["summaries"]}
        for sb in run_b["summaries"]:
            sa = by_cond_a.get(sb["condition"], {})
            deltas.append(
                {
                    "condition": sb["condition"],
                    "final_dispersion_a": sa.get("mean_final_dispersion", 0.0),
                    "final_dispersion_b": sb.get("mean_final_dispersion", 0.0),
                    "delta_final_dispersion": round(
                        sb.get("mean_final_dispersion", 0.0)
                        - sa.get("mean_final_dispersion", 0.0),
                        6,
                    ),
                    "post_distance_a": sa.get(
                        "mean_post_distance_to_prior_center", 0.0
                    ),
                    "post_distance_b": sb.get(
                        "mean_post_distance_to_prior_center", 0.0
                    ),
                    "delta_post_distance": round(
                        sb.get("mean_post_distance_to_prior_center", 0.0)
                        - sa.get("mean_post_distance_to_prior_center", 0.0),
                        6,
                    ),
                }
            )
    elif exp == "asch":
        run_a = run_asch_lab(
            trials_per_group=int(pa.get("trials", 200)),
            seed=str(pa.get("seed", "asch-v0.1")),
            sample_majority_size=int(pa.get("sample_k", 4)),
        )
        run_b = run_asch_lab(
            trials_per_group=int(pb.get("trials", 200)),
            seed=str(pb.get("seed", "asch-alt-v0.1")),
            sample_majority_size=int(pb.get("sample_k", 4)),
        )
        deltas = []
        by_k_a = {
            s["majority_size"]: s for s in run_a["sweeps"]["unanimous"]["summaries"]
        }
        for sb in run_b["sweeps"]["unanimous"]["summaries"]:
            sa = by_k_a.get(sb["majority_size"], {})
            deltas.append(
                {
                    "majority_size": sb["majority_size"],
                    "unanimous_conformity_a": sa.get("conformity_rate", 0.0),
                    "unanimous_conformity_b": sb.get("conformity_rate", 0.0),
                    "delta_unanimous_conformity": round(
                        sb.get("conformity_rate", 0.0) - sa.get("conformity_rate", 0.0),
                        6,
                    ),
                }
            )
    elif exp == "bystander":
        run_a = run_bystander_lab(
            trials_per_group=int(pa.get("trials", 200)),
            responsibility_weight=float(pa.get("responsibility_weight", 0.85)),
            noticing_probability=float(pa.get("noticing_probability", 0.95)),
        )
        run_b = run_bystander_lab(
            trials_per_group=int(pb.get("trials", 200)),
            responsibility_weight=float(pb.get("responsibility_weight", 0.55)),
            noticing_probability=float(pb.get("noticing_probability", 0.85)),
        )
        deltas = []
        by_n_a = {
            s["n_bystanders"]: s
            for s in run_a["sweeps"]["pluralistic_ignorance"]["summaries"]
        }
        for sb in run_b["sweeps"]["pluralistic_ignorance"]["summaries"]:
            sa = by_n_a.get(sb["n_bystanders"], {})
            deltas.append(
                {
                    "n_bystanders": sb["n_bystanders"],
                    "escalation_a": sa.get("individual_escalation_rate", 0.0),
                    "escalation_b": sb.get("individual_escalation_rate", 0.0),
                    "delta_escalation": round(
                        sb.get("individual_escalation_rate", 0.0)
                        - sa.get("individual_escalation_rate", 0.0),
                        6,
                    ),
                    "nobody_escalated_a": sa.get("nobody_escalated_rate", 0.0),
                    "nobody_escalated_b": sb.get("nobody_escalated_rate", 0.0),
                    "delta_nobody_escalated": round(
                        sb.get("nobody_escalated_rate", 0.0)
                        - sa.get("nobody_escalated_rate", 0.0),
                        6,
                    ),
                }
            )
    elif exp == "cascade":
        run_a = run_cascade_lab(
            trials_per_group=int(pa.get("trials", 200)),
            signal_accuracy=float(pa.get("signal_accuracy", 0.70)),
            case_requires_review=bool(pa.get("case_requires_review", True)),
        )
        run_b = run_cascade_lab(
            trials_per_group=int(pb.get("trials", 200)),
            signal_accuracy=float(pb.get("signal_accuracy", 0.85)),
            case_requires_review=bool(pb.get("case_requires_review", True)),
        )
        deltas = []
        by_n_a = {s["n_agents"]: s for s in run_a["sweep"]["summaries"]}
        for sb in run_b["sweep"]["summaries"]:
            sa = by_n_a.get(sb["n_agents"], {})
            deltas.append(
                {
                    "n_agents": sb["n_agents"],
                    "cascade_accuracy_a": sa.get("group_decision_accuracy", 0.0),
                    "cascade_accuracy_b": sb.get("group_decision_accuracy", 0.0),
                    "delta_cascade_accuracy": round(
                        sb.get("group_decision_accuracy", 0.0)
                        - sa.get("group_decision_accuracy", 0.0),
                        6,
                    ),
                    "wrong_lock_a": sa.get("wrong_lock_rate", 0.0),
                    "wrong_lock_b": sb.get("wrong_lock_rate", 0.0),
                    "delta_wrong_lock": round(
                        sb.get("wrong_lock_rate", 0.0)
                        - sa.get("wrong_lock_rate", 0.0),
                        6,
                    ),
                }
            )
    else:
        raise ValueError(f"Unsupported experiment for comparison: {experiment}")

    return {
        "experiment": exp,
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "run_a": run_a,
        "run_b": run_b,
        "deltas": deltas,
    }


def format_lab_result_csv(
    experiment: str,
    run_a: dict[str, Any],
    run_b: dict[str, Any] | None = None,
) -> str:
    """Format single or paired (A/B) experiment summaries into RFC 4180 CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    exp = experiment.strip().lower()

    if exp == "ringelmann":
        headers = [
            "run_slot",
            "seeds",
            "accountability_bonus",
            "coordination_loss_per_member",
            "team_size",
            "individual_attribution_visible",
            "teams",
            "mean_individual_effort",
            "ci95_lower",
            "ci95_upper",
            "mean_additive_output",
            "coordination_efficiency_proxy",
            "mean_synchronized_output",
        ]
        writer.writerow(headers)
        for slot, run in (("A", run_a), ("B", run_b)):
            if not run:
                continue
            p = run.get("parameters", {})
            for s in run.get("summaries", []):
                ci = s.get("mean_individual_effort_ci95", {})
                writer.writerow(
                    [
                        slot,
                        p.get("seeds", ""),
                        p.get("accountability_bonus", ""),
                        p.get("coordination_loss_per_member", ""),
                        s.get("team_size", ""),
                        s.get("individual_attribution_visible", ""),
                        s.get("teams", ""),
                        round(float(s.get("mean_individual_effort", 0.0)), 6),
                        round(float(ci.get("lower_95", 0.0)), 6),
                        round(float(ci.get("upper_95", 0.0)), 6),
                        round(float(s.get("mean_additive_output", 0.0)), 6),
                        round(float(s.get("coordination_efficiency_proxy", 0.0)), 6),
                        round(float(s.get("mean_synchronized_output", 0.0)), 6),
                    ]
                )
    elif exp == "sherif":
        headers = [
            "run_slot",
            "seeds",
            "alpha",
            "interaction_rounds",
            "update_noise_half_range",
            "condition",
            "teams",
            "mean_pre_dispersion",
            "mean_final_dispersion",
            "ci95_lower",
            "ci95_upper",
            "mean_post_distance_to_prior_center",
            "mean_final_center_error",
        ]
        writer.writerow(headers)
        for slot, run in (("A", run_a), ("B", run_b)):
            if not run:
                continue
            p = run.get("parameters", {})
            for s in run.get("summaries", []):
                ci = s.get("mean_final_dispersion_ci95", {})
                writer.writerow(
                    [
                        slot,
                        p.get("seeds", ""),
                        p.get("alpha", ""),
                        p.get("interaction_rounds", ""),
                        p.get("update_noise_half_range", ""),
                        s.get("condition", ""),
                        s.get("teams", ""),
                        round(float(s.get("mean_pre_dispersion", 0.0)), 6),
                        round(float(s.get("mean_final_dispersion", 0.0)), 6),
                        round(float(ci.get("lower_95", 0.0)), 6),
                        round(float(ci.get("upper_95", 0.0)), 6),
                        round(
                            float(s.get("mean_post_distance_to_prior_center", 0.0)), 6
                        ),
                        round(float(s.get("mean_final_center_error", 0.0)), 6),
                    ]
                )
    elif exp == "asch":
        headers = [
            "run_slot",
            "seed",
            "trials_per_group",
            "condition",
            "majority_size",
            "conformity_rate",
            "theoretical_conformity_rate",
        ]
        writer.writerow(headers)
        for slot, run in (("A", run_a), ("B", run_b)):
            if not run:
                continue
            p = run.get("parameters", {})
            for cond_key in ("unanimous", "dissenter"):
                summaries = (
                    run.get("sweeps", {}).get(cond_key, {}).get("summaries", [])
                )
                for s in summaries:
                    writer.writerow(
                        [
                            slot,
                            p.get("seed", ""),
                            p.get("trials_per_group", ""),
                            cond_key,
                            s.get("majority_size", ""),
                            round(float(s.get("conformity_rate", 0.0)), 6),
                            round(
                                float(s.get("theoretical_conformity_rate", 0.0)), 6
                            ),
                        ]
                    )
    elif exp == "bystander":
        headers = [
            "run_slot",
            "trials_per_group",
            "responsibility_weight",
            "noticing_probability",
            "mechanism",
            "n_bystanders",
            "individual_escalation_rate",
            "group_escalation_rate",
            "nobody_escalated_rate",
            "composition_rejected_rate",
        ]
        writer.writerow(headers)
        for slot, run in (("A", run_a), ("B", run_b)):
            if not run:
                continue
            p = run.get("parameters", {})
            for mech_key in ("diffusion", "pluralistic_ignorance"):
                summaries = (
                    run.get("sweeps", {}).get(mech_key, {}).get("summaries", [])
                )
                for s in summaries:
                    writer.writerow(
                        [
                            slot,
                            p.get("trials_per_group", ""),
                            p.get("responsibility_weight", ""),
                            p.get("noticing_probability", ""),
                            mech_key,
                            s.get("n_bystanders", ""),
                            round(float(s.get("individual_escalation_rate", 0.0)), 6),
                            round(float(s.get("group_escalation_rate", 0.0)), 6),
                            round(float(s.get("nobody_escalated_rate", 0.0)), 6),
                            round(float(s.get("composition_rejected_rate", 0.0)), 6),
                        ]
                    )
    elif exp == "cascade":
        headers = [
            "run_slot",
            "trials_per_group",
            "signal_accuracy",
            "case_requires_review",
            "n_agents",
            "pooled_accuracy",
            "group_decision_accuracy",
            "wrong_lock_rate",
            "mean_discarded_correct_signals",
        ]
        writer.writerow(headers)
        for slot, run in (("A", run_a), ("B", run_b)):
            if not run:
                continue
            p = run.get("parameters", {})
            for s in run.get("sweep", {}).get("summaries", []):
                writer.writerow(
                    [
                        slot,
                        p.get("trials_per_group", ""),
                        p.get("signal_accuracy", ""),
                        p.get("case_requires_review", ""),
                        s.get("n_agents", ""),
                        round(float(s.get("pooled_accuracy", 0.0)), 6),
                        round(float(s.get("group_decision_accuracy", 0.0)), 6),
                        round(float(s.get("wrong_lock_rate", 0.0)), 6),
                        round(float(s.get("mean_discarded_correct_signals", 0.0)), 6),
                    ]
                )
    else:
        raise ValueError(f"Unsupported experiment for CSV export: {experiment}")

    csv_text = buf.getvalue()
    if run_b is None:
        return csv_text

    key_columns: dict[str, tuple[str, ...]] = {
        "ringelmann": ("team_size", "individual_attribution_visible"),
        "sherif": ("condition",),
        "asch": ("condition", "majority_size"),
        "bystander": ("mechanism", "n_bystanders"),
        "cascade": ("n_agents",),
    }
    metric_columns: dict[str, tuple[str, ...]] = {
        "ringelmann": ("mean_individual_effort", "mean_synchronized_output"),
        "sherif": ("mean_final_dispersion", "mean_post_distance_to_prior_center"),
        "asch": ("conformity_rate", "theoretical_conformity_rate"),
        "bystander": (
            "individual_escalation_rate",
            "group_escalation_rate",
            "nobody_escalated_rate",
            "composition_rejected_rate",
        ),
        "cascade": (
            "pooled_accuracy",
            "group_decision_accuracy",
            "wrong_lock_rate",
            "mean_discarded_correct_signals",
        ),
    }
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])
    keys = key_columns[exp]
    metrics = metric_columns[exp]
    baseline = {
        tuple(row.get(key, "") for key in keys): row
        for row in rows
        if row.get("run_slot") == "A"
    }
    delta_headers = [f"delta_{metric}_b_minus_a" for metric in metrics]
    output = io.StringIO()
    delta_writer = csv.DictWriter(
        output, fieldnames=fieldnames + delta_headers, lineterminator="\n"
    )
    delta_writer.writeheader()
    for row in rows:
        if row.get("run_slot") == "B":
            a_row = baseline.get(tuple(row.get(key, "") for key in keys), {})
            for metric, header in zip(metrics, delta_headers):
                try:
                    row[header] = round(
                        float(row[metric]) - float(a_row[metric]), 6
                    )
                except (KeyError, TypeError, ValueError):
                    row[header] = ""
        else:
            row.update({header: "" for header in delta_headers})
        delta_writer.writerow(row)
    return output.getvalue()


def evaluate_case_lab(
    root: Path,
    *,
    profile_key: str = "seguros",
    case_kind: str = "fail",
) -> dict[str, Any]:
    """Run the deterministic contract evaluator on a sector profile and trajectory."""
    profile = next(
        (item for item in EVALUATOR_PROFILES if item["key"] == profile_key),
        EVALUATOR_PROFILES[0],
    )
    contract_rel = profile["contract"]
    trajectory_rel = (
        profile["pass_trajectory"] if case_kind == "pass" else profile["fail_trajectory"]
    )
    t0 = time.perf_counter()
    contract = load_contract(root / contract_rel)
    trajectory = load_trajectory(root / trajectory_rel)
    evaluation = evaluate_contract(contract, trajectory)
    duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    clause_descriptions = {c.id: c.description for c in contract.clauses}
    clause_severities = {
        c.id: getattr(c.severity, "value", str(c.severity)) for c in contract.clauses
    }

    clauses_out = []
    for item in evaluation.clause_results:
        clauses_out.append(
            {
                "clause_id": item.clause_id,
                "status": item.status.value,
                "severity": clause_severities.get(item.clause_id, ""),
                "description": clause_descriptions.get(item.clause_id, ""),
                "reason": item.reason,
                "recommended_autonomy": item.recommended_autonomy.value,
                "evidence_refs": [ev.ref for ev in item.evidence],
            }
        )

    observations_out = [
        {
            "id": obs.ref,
            "kind": obs.kind.value,
            "name": obs.name,
            "timestamp": obs.observed_at,
            "attributes": dict(obs.attributes),
        }
        for obs in trajectory.observations
    ]

    return attach_artifact_digest({
        "profile_key": profile["key"],
        "profile_name": profile["name"],
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "case_kind": "pass" if case_kind == "pass" else "fail",
        "summary": profile["summary"],
        "contract_path": contract_rel,
        "trajectory_path": trajectory_rel,
        "contract_id": evaluation.contract_id,
        "contract_version": evaluation.contract_version,
        "trajectory_id": evaluation.trajectory_id,
        "overall_status": evaluation.status.value,
        "recommended_autonomy": evaluation.recommended_autonomy.value,
        "block_release": evaluation.block_release,
        "evaluator_version": evaluation.evaluator.version,
        "duration_ms": duration_ms,
        "clauses": clauses_out,
        "observations": observations_out,
    })


def load_verified_social_artifacts(root: Path) -> dict[str, Any]:
    """Load the pre-executed 1,000-seed reproducible social experiment summaries if present."""
    run_dir = root / "data" / "social_experiments" / "run_20261021_v0.1_full"
    out: dict[str, Any] = {}
    for exp in ("ringelmann", "sherif"):
        manifest_path = run_dir / exp / "manifest.json"
        summary_path = run_dir / exp / "summary.json"
        if manifest_path.is_file() and summary_path.is_file():
            out[exp] = {
                "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
                "summaries": json.loads(summary_path.read_text(encoding="utf-8")),
            }
    return out


def build_lab_static_bundle(root: Path) -> dict[str, Any]:
    """Build the complete static fallback bundle for the Web Lab."""
    verified = load_verified_social_artifacts(root)

    ringelmann_data = run_ringelmann_lab(seeds=200)
    if "ringelmann" in verified:
        ringelmann_data["verified_1000_seeds"] = verified["ringelmann"]
        # Attaching the pre-executed 1,000-seed artefact changes the payload, so
        # the digest must be recomputed over the final bundle content.
        ringelmann_data = attach_artifact_digest(ringelmann_data)

    sherif_data = run_sherif_lab(seeds=200)
    if "sherif" in verified:
        sherif_data["verified_1000_seeds"] = verified["sherif"]
        sherif_data = attach_artifact_digest(sherif_data)

    asch_data = run_asch_lab(trials_per_group=200)
    bystander_data = run_bystander_lab(trials_per_group=200)
    cascade_data = run_cascade_lab(trials_per_group=200)
    swarm_data = run_swarm_lab(root)

    evaluator_cases: dict[str, dict[str, Any]] = {}
    for profile in EVALUATOR_PROFILES:
        key = profile["key"]
        evaluator_cases[key] = {
            "pass": evaluate_case_lab(root, profile_key=key, case_kind="pass"),
            "fail": evaluate_case_lab(root, profile_key=key, case_kind="fail"),
        }

    registry = load_registry(root / DEFAULT_REGISTRY)
    proven_list = [
        {
            "clause_id": c.clause_id,
            "kind": c.kind,
            "contract": c.contract,
            "fixture": c.fixture,
            "control": c.control,
            "discriminating": c.control is not None,
        }
        for c in registry.canaries
    ]
    unproven_list = [
        {"clause_id": u.clause_id, "reason": u.reason} for u in registry.unproven
    ]
    canary_payload = {
        "counts": {
            "total": len(proven_list) + len(unproven_list),
            "proven": len(proven_list),
            "unproven": len(unproven_list),
            "discriminating": sum(1 for item in proven_list if item["discriminating"]),
        },
        "proven": proven_list,
        "unproven": unproven_list,
    }
    sample_canary_traces: dict[str, Any] = {}
    # Pre-include 3 representative live traces (one trajectory, one sandbox, one swarm)
    for target_kind in ("trajectory", "sandbox", "swarm"):
        candidate = next((c for c in registry.canaries if c.kind == target_kind), None)
        if candidate is not None:
            comp = run_live_clause_comparison(
                candidate.clause_id, root, registry=registry
            )
            sample_canary_traces[candidate.clause_id] = comp.model_dump(mode="json")

    ledger_bundle_path = root / "web" / "assets" / "data" / "demo_ledger_bundle.json"
    ledger_bundle = (
        json.loads(ledger_bundle_path.read_text(encoding="utf-8"))
        if ledger_bundle_path.is_file()
        else None
    )

    return {
        "schema_version": LAB_SCHEMA_VERSION,
        "claim_boundary": CLAIM_BOUNDARY_NOTE,
        "experiments": {
            "ringelmann": ringelmann_data,
            "sherif": sherif_data,
            "asch": asch_data,
            "bystander": bystander_data,
            "cascade": cascade_data,
            "swarm": swarm_data,
        },
        "services": {
            "evaluator": {
                "profiles": list(EVALUATOR_PROFILES),
                "cases": evaluator_cases,
            },
            "canaries": {
                "status": canary_payload,
                "sample_traces": sample_canary_traces,
            },
            "ledger": {
                "demo_bundle": ledger_bundle,
            },
        },
    }


def _parse_int(val: str | None, default: int, minimum: int, maximum: int) -> int:
    if val is None or val == "":
        return default
    try:
        parsed = int(val)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _parse_float(
    val: str | None, default: float, minimum: float, maximum: float
) -> float:
    if val is None or val == "":
        return default
    try:
        parsed = float(val)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _first(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    """Return the first value for a query key, falling back to ``default``."""

    values = query.get(key)
    return values[0] if values else default


def make_web_lab_handler(
    root: Path,
    *,
    signing_key_dir: Path | None = None,
    attributable_signing: bool = False,
    basic_auth: LabBasicAuth | None = None,
    access_log: LabAccessLog | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create the HTTP request handler serving ``lab/``, ``web/assets/``, and ``/api/lab/*``.

    ``attributable_signing`` decides whether exported packages carry the lab
    seal. It must stay false on an instance whose key is not the laboratory's
    own: a locally generated key would produce a seal that looks attributable
    and is not.
    """

    resolved_root = root.resolve()
    resolved_key_dir = (signing_key_dir or default_key_dir(resolved_root)).resolve()
    lab_dir = (resolved_root / "lab").resolve()
    web_dir = (resolved_root / "web").resolve()

    # What this instance may attest: the artifacts it ships in its own bundle,
    # plus every run it produces while serving. Nothing a client posts is added
    # here, or the sealing endpoints would become a stamp for any document.
    artifact_registry = LabArtifactRegistry()
    register_bundle_artifacts(
        artifact_registry,
        resolved_root / "lab" / "assets" / "data" / "lab-bundle.json",
    )

    class WebLabRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def send_response(self, code: int, message: str | None = None) -> None:
            """Every response is one access-log line, refusals included."""

            super().send_response(code, message)
            if access_log is not None:
                access_log.write(
                    method=getattr(self, "command", ""),
                    path=getattr(self, "path", ""),
                    status=int(code),
                    remote=self.client_address[0] if self.client_address else "",
                    user=getattr(self, "_auth_user", None),
                )

        def _authenticate(self) -> bool:
            if basic_auth is None:
                return True
            user = basic_auth.check(self.headers.get("Authorization"))
            if user is not None:
                self._auth_user = user
                return True
            self.send_response(int(HTTPStatus.UNAUTHORIZED))
            self.send_header(
                "WWW-Authenticate", 'Basic realm="Integrity Lab", charset="UTF-8"'
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _register_result(self, result: Any, experiment: str, origin: str) -> None:
            """Record an artifact this instance just produced, so it can be sealed."""

            artifact_registry.register(result, experiment=experiment, origin=origin)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _extract_slot_params(
            self, query: dict[str, list[str]], slot: str, experiment: str
        ) -> dict[str, Any]:
            prefix = f"{slot}_"
            exp = experiment.strip().lower()
            if exp == "ringelmann":
                return {
                    "seeds": _parse_int(_first(query, f"{prefix}seeds"), 200, 10, 2000),
                    "accountability_bonus": _parse_float(
                        _first(query, f"{prefix}accountability_bonus"),
                        RINGELMANN_ACCOUNTABILITY_BONUS if slot == "a" else 0.80,
                        0.0,
                        1.5,
                    ),
                    "coordination_loss": _parse_float(
                        _first(query, f"{prefix}coordination_loss"),
                        RINGELMANN_COORDINATION_LOSS_PER_MEMBER if slot == "a" else 0.02,
                        0.0,
                        0.20,
                    ),
                }
            if exp == "sherif":
                return {
                    "seeds": _parse_int(_first(query, f"{prefix}seeds"), 200, 10, 2000),
                    "alpha": _parse_float(
                        _first(query, f"{prefix}alpha"),
                        SHERIF_ALPHA if slot == "a" else 0.60,
                        0.0,
                        1.0,
                    ),
                    "interaction_rounds": _parse_int(
                        _first(query, f"{prefix}interaction_rounds"),
                        SHERIF_INTERACTION_ROUNDS,
                        1,
                        25,
                    ),
                    "update_noise": _parse_float(
                        _first(query, f"{prefix}update_noise"),
                        SHERIF_UPDATE_NOISE_HALF_RANGE,
                        0.0,
                        5.0,
                    ),
                }
            if exp == "asch":
                return {
                    "trials": _parse_int(_first(query, f"{prefix}trials"), 200, 10, 2000),
                    "seed": (
                        _first(query, f"{prefix}seed", f"asch-{slot}-v0.1")
                        or f"asch-{slot}-v0.1"
                    ).strip()[:80],
                    "sample_k": _parse_int(
                        _first(query, f"{prefix}sample_k"), 4, 1, 8
                    ),
                }
            if exp == "bystander":
                return {
                    "trials": _parse_int(_first(query, f"{prefix}trials"), 200, 10, 2000),
                    "responsibility_weight": _parse_float(
                        _first(query, f"{prefix}responsibility_weight"),
                        0.85 if slot == "a" else 0.55,
                        0.05,
                        1.0,
                    ),
                    "noticing_probability": _parse_float(
                        _first(query, f"{prefix}noticing_probability"),
                        0.95 if slot == "a" else 0.85,
                        0.1,
                        1.0,
                    ),
                }
            if exp == "cascade":
                case_val = _first(query, f"{prefix}case", "review")
                return {
                    "trials": _parse_int(_first(query, f"{prefix}trials"), 200, 10, 2000),
                    "signal_accuracy": _parse_float(
                        _first(query, f"{prefix}signal_accuracy"),
                        0.70 if slot == "a" else 0.85,
                        0.51,
                        0.99,
                    ),
                    "case_requires_review": case_val != "benign",
                }
            raise ValueError(f"Unsupported experiment: {experiment}")

        def _send_error_json(self, status: HTTPStatus, message: str) -> None:
            self._send_json(status, {"ok": False, "error": message})

        def _cross_origin(self) -> bool:
            """Reject mutations coming from a page served by another origin."""

            origin = self.headers.get("Origin")
            if not origin:
                return False
            parsed_origin = urlparse(origin)
            try:
                origin_port = parsed_origin.port
            except ValueError:
                return True
            address = self.server.server_address
            if not isinstance(address, tuple):
                return True
            return (
                parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
                or origin_port != address[1]
            )

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length <= 0:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "request body is required"
                )
                return None
            if length > 4 * 1024 * 1024:
                self._send_error_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "request body exceeds 4 MiB",
                )
                return None
            raw = self.rfile.read(length)
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "request body must be JSON"
                )
                return None
            if not isinstance(parsed_body, dict):
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "request body must be a JSON object"
                )
                return None
            return parsed_body

        def do_POST(self) -> None:  # noqa: N802
            if not self._authenticate():
                return
            path = urlparse(self.path).path
            if path not in (
                "/api/lab/digest",
                "/api/lab/package",
                "/api/lab/sign",
                "/api/lab/verify",
                "/api/lab/index",
                "/api/lab/study",
            ):
                self._send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")
                return
            if self._cross_origin():
                self._send_error_json(
                    HTTPStatus.FORBIDDEN, "cross-origin request rejected"
                )
                return
            body = self._read_json_body()
            if body is None:
                return

            if path in ("/api/lab/index", "/api/lab/study"):
                packages = body.get("packages")
                if (
                    not isinstance(packages, list)
                    or len(packages) > 100
                    or not all(isinstance(item, dict) for item in packages)
                ):
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST,
                        "packages must be a list of at most 100 JSON objects",
                    )
                    return
                checks = []
                shaped = []
                malformed = []
                for item in packages:
                    try:
                        LabPackage.model_validate(item)
                    except ValidationError:
                        problems = ["package does not match the citable run schema"]
                        malformed.append(LabRejectedPackage(problems=problems))
                        checks.append({"valid": False, "problems": problems})
                    else:
                        shaped.append(item)
                        checks.append(verify_lab_package(item).model_dump(mode="json"))
                index = build_run_index(shaped)
                index.package_count += len(malformed)
                index.rejected.extend(malformed)
                index.rejected_count += len(malformed)
                if path == "/api/lab/index":
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "index": index.model_dump(mode="json"), "checks": checks},
                    )
                    return
                if not packages or not all(check["valid"] for check in checks):
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST, "a study requires verified citable packages"
                    )
                    return
                # This endpoint cannot prove when a plan was written. Never let
                # an exported study imply independent prospective registration.
                declaration = (
                    "Plan declared through the local workbench; prior registration "
                    "and temporal precedence over the runs are not independently verified."
                )
                try:
                    for field in ("study_id", "title"):
                        if not isinstance(body.get(field), str) or not body[field].strip():
                            raise ValueError(f"{field} is required")
                    preregistration = body.get("preregistration")
                    if not isinstance(preregistration, dict):
                        raise ValueError("preregistration must be a JSON object")
                    study = build_lab_study(
                        packages,
                        study_id=body["study_id"].strip(),
                        title=body["title"].strip(),
                        preregistration=preregistration,
                        notes=declaration,
                    )
                except (TypeError, ValueError) as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "study": study.model_dump(mode="json"),
                        "verification": verify_lab_study(study.model_dump(mode="json")).model_dump(
                            mode="json"
                        ),
                        "index": index.model_dump(mode="json"),
                    },
                )
                return

            if path == "/api/lab/verify":
                try:
                    signature = LabSignature.model_validate(body.get("signature"))
                except (ValueError, TypeError) as exc:
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST, f"signature is invalid: {exc}"
                    )
                    return
                if "artifact" not in body:
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST, "missing required field: artifact"
                    )
                    return
                verification = verify_lab_artifact(body["artifact"], signature)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "verification": verification.model_dump(mode="json"),
                    },
                )
                return

            artifact = body.get("artifact")
            if artifact is None:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "missing required field: artifact"
                )
                return
            experiment = str(body.get("experiment") or "").strip()
            if not experiment:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "missing required field: experiment"
                )
                return

            # Sealing is a statement about this instance's own evidence. The
            # registry holds the digests it produced or ships; anything else is
            # refused instead of being stamped with the laboratory key.
            if attributable_signing and path in ("/api/lab/package", "/api/lab/sign"):
                covered, reason = artifact_registry.covers(artifact)
                if not covered:
                    self._send_error_json(HTTPStatus.CONFLICT, reason)
                    return

            if path == "/api/lab/digest":
                try:
                    digest = artifact_digest(artifact)
                except (TypeError, ValueError) as exc:
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST, f"artifact is not JSON: {exc}"
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "experiment": experiment,
                        "artifact_sha256": digest,
                    },
                )
                return

            if path == "/api/lab/package":
                try:
                    package = build_lab_package(
                        artifact,
                        experiment=experiment,
                        subject_kind=subject_kind_for(artifact),
                        execution_mode="live",
                        key_dir=resolved_key_dir if attributable_signing else None,
                    )
                except ValueError as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "package": package.model_dump(mode="json"),
                    },
                )
                return

            try:
                signature = sign_lab_artifact(
                    artifact,
                    experiment=experiment,
                    key_dir=resolved_key_dir,
                )
            except (OSError, ValueError) as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "mode": "live",
                    "experiment": experiment,
                    "artifact_sha256": signature.artifact_sha256,
                    "signature": signature.model_dump(mode="json"),
                },
            )

        def _serve_file(self, base_dir: Path, rel_path: str) -> bool:
            rel = rel_path.lstrip("/")
            candidate = (base_dir / rel).resolve()
            try:
                candidate.relative_to(base_dir)
            except ValueError:
                return False
            if candidate.is_dir():
                candidate = candidate / "index.html"
            if not candidate.is_file():
                return False
            ctype, _ = mimetypes.guess_type(str(candidate))
            data = candidate.read_bytes()
            self.send_response(int(HTTPStatus.OK))
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._authenticate():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/api/lab/health":
                registry = load_registry(resolved_root / DEFAULT_REGISTRY)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "schema_version": LAB_SCHEMA_VERSION,
                        "experiments": [
                            "ringelmann",
                            "sherif",
                            "asch",
                            "bystander",
                            "cascade",
                            "swarm",
                        ],
                        "services": ["evaluator", "canaries", "ledger"],
                        "evidence_signing": "Ed25519",
                        "canary_count": len(registry.canaries),
                        "registered_artifacts": len(artifact_registry),
                    },
                )
                return

            if path == "/api/lab/ringelmann":
                seeds = _parse_int(_first(query, "seeds"), 200, 10, 2000)
                bonus = _parse_float(
                    _first(query, "accountability_bonus"),
                    RINGELMANN_ACCOUNTABILITY_BONUS,
                    0.0,
                    1.5,
                )
                coord_loss = _parse_float(
                    _first(query, "coordination_loss"),
                    RINGELMANN_COORDINATION_LOSS_PER_MEMBER,
                    0.0,
                    0.20,
                )
                result = run_ringelmann_lab(
                    seeds=seeds,
                    accountability_bonus=bonus,
                    coordination_loss_per_member=coord_loss,
                )
                self._register_result(result, "ringelmann", ORIGIN_LIVE_RUN)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "live", "result": result})
                return

            if path == "/api/lab/sherif":
                seeds = _parse_int(_first(query, "seeds"), 200, 10, 2000)
                alpha = _parse_float(
                    _first(query, "alpha"), SHERIF_ALPHA, 0.0, 1.0
                )
                rounds = _parse_int(
                    _first(query, "interaction_rounds"),
                    SHERIF_INTERACTION_ROUNDS,
                    1,
                    25,
                )
                private_noise = _parse_float(
                    _first(query, "private_noise"),
                    SHERIF_PRIVATE_NOISE_HALF_RANGE,
                    1.0,
                    30.0,
                )
                update_noise = _parse_float(
                    _first(query, "update_noise"),
                    SHERIF_UPDATE_NOISE_HALF_RANGE,
                    0.0,
                    5.0,
                )
                result = run_sherif_lab(
                    seeds=seeds,
                    alpha=alpha,
                    interaction_rounds=rounds,
                    private_noise_half_range=private_noise,
                    update_noise_half_range=update_noise,
                )
                self._register_result(result, "sherif", ORIGIN_LIVE_RUN)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "live", "result": result})
                return

            if path == "/api/lab/asch":
                trials = _parse_int(_first(query, "trials"), 200, 10, 2000)
                seed = (_first(query, "seed", "asch-v0.1") or "asch-v0.1").strip()[:80]
                sample_k = _parse_int(_first(query, "sample_k"), 4, 1, 8)
                result = run_asch_lab(
                    trials_per_group=trials,
                    seed=seed,
                    sample_majority_size=sample_k,
                )
                self._register_result(result, "asch", ORIGIN_LIVE_RUN)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "live", "result": result})
                return

            if path == "/api/lab/bystander":
                trials = _parse_int(_first(query, "trials"), 200, 10, 2000)
                weight = _parse_float(
                    _first(query, "responsibility_weight"), 0.85, 0.05, 1.0
                )
                noticing = _parse_float(
                    _first(query, "noticing_probability"), 0.95, 0.1, 1.0
                )
                sample_n = _parse_int(_first(query, "sample_n"), 4, 1, 8)
                result = run_bystander_lab(
                    trials_per_group=trials,
                    responsibility_weight=weight,
                    noticing_probability=noticing,
                    sample_n=sample_n,
                )
                self._register_result(result, "bystander", ORIGIN_LIVE_RUN)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "live", "result": result})
                return

            if path == "/api/lab/cascade":
                trials = _parse_int(_first(query, "trials"), 200, 10, 2000)
                accuracy = _parse_float(
                    _first(query, "signal_accuracy"), 0.70, 0.51, 0.99
                )
                case_dir = _first(query, "case", "review")
                requires_review = case_dir != "benign"
                sample_n = _parse_int(_first(query, "sample_n"), 8, 1, 12)
                result = run_cascade_lab(
                    trials_per_group=trials,
                    signal_accuracy=accuracy,
                    case_requires_review=requires_review,
                    sample_n=sample_n,
                )
                self._register_result(result, "cascade", ORIGIN_LIVE_RUN)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "live", "result": result})
                return

            if path == "/api/lab/signing":
                try:
                    info = signing_key_info(resolved_key_dir)
                except (OSError, ValueError) as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "mode": "live", "signing": info},
                )
                return

            if path == "/api/lab/swarm":
                pair_arg = (_first(query, "pair", "both") or "both").strip().lower()
                if pair_arg == "both":
                    requested = tuple(preset["key"] for preset in SWARM_PAIR_PRESETS)
                else:
                    requested = tuple(
                        item.strip()
                        for item in pair_arg.split(",")
                        if item.strip()
                    )
                if not requested:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "missing swarm pair"},
                    )
                    return
                try:
                    result = run_swarm_lab(resolved_root, pairs=requested)
                    self._register_result(result, "swarm", ORIGIN_LIVE_RUN)
                except ValueError as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "mode": "live", "result": result},
                )
                return

            if path == "/api/lab/evaluate":
                profile_key = (_first(query, "profile", "seguros") or "seguros").strip()
                case_kind = (_first(query, "case", "fail") or "fail").strip()
                result = evaluate_case_lab(
                    resolved_root, profile_key=profile_key, case_kind=case_kind
                )
                self._register_result(result, "evaluator", ORIGIN_LIVE_EVALUATION)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "live", "result": result})
                return

            if path in ("/api/lab/canary", "/api/canary/run"):
                clause_id = (_first(query, "clause", "") or "").strip()
                if not clause_id:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "missing required parameter: clause"},
                    )
                    return
                try:
                    comp = run_live_clause_comparison(clause_id, resolved_root)
                except CanaryError as exc:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "clause_id": clause_id, "error": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "live",
                        "comparison": comp.model_dump(mode="json"),
                    },
                )
                return

            if path == "/api/lab/compare":
                exp = (_first(query, "experiment", "ringelmann") or "ringelmann").strip()
                fmt = (_first(query, "format", "json") or "json").strip().lower()
                try:
                    params_a = self._extract_slot_params(query, "a", exp)
                    params_b = self._extract_slot_params(query, "b", exp)
                    comparison = compare_lab_runs(exp, params_a, params_b)
                except ValueError as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(exc)},
                    )
                    return

                if fmt == "csv":
                    csv_text = format_lab_result_csv(
                        exp, comparison["run_a"], comparison["run_b"]
                    )
                    raw = csv_text.encode("utf-8")
                    self.send_response(int(HTTPStatus.OK))
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="{exp}-comparison-ab.csv"',
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "mode": "live", "comparison": comparison},
                )
                return

            if path.startswith("/web-assets/"):
                rel = path.removeprefix("/web-assets/")
                if self._serve_file(web_dir / "assets", rel):
                    return

            if path.startswith("/lab/"):
                rel = path.removeprefix("/lab/")
                if self._serve_file(lab_dir, rel):
                    return

            if self._serve_file(lab_dir, path):
                return

            self.send_response(int(HTTPStatus.NOT_FOUND))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found")

    return WebLabRequestHandler


def create_web_lab_server(
    host: str = "127.0.0.1",
    port: int = 8910,
    *,
    root: Path | None = None,
    signing_key_dir: Path | None = None,
    attributable_signing: bool = False,
    basic_auth: LabBasicAuth | None = None,
    access_log_path: Path | None = None,
    tls_context: Any | None = None,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer for the Web Lab.

    ``basic_auth`` protects every request, including the API; ``tls_context``
    wraps the listening socket. Both are optional so a local workbench and the
    tests keep working without them, and neither is a substitute for the other:
    Basic credentials on plaintext HTTP are readable by anyone on the path.
    """

    resolved = (root or Path(__file__).resolve().parents[2]).resolve()
    handler = make_web_lab_handler(
        resolved,
        signing_key_dir=signing_key_dir,
        attributable_signing=attributable_signing,
        basic_auth=basic_auth,
        access_log=LabAccessLog(access_log_path) if access_log_path else None,
    )
    server = ThreadingHTTPServer((host, port), handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    return server
