"""Behavioural Autopilot foundation package.

Public API is preserved via PEP 562 lazy ``__getattr__`` so that importing a
specific submodule (e.g. ``autopilot.evaluator``) does not eagerly load
unrelated modules (e.g. ``autopilot.adapter``). This closes B0 capability C1
acceptance criterion: ``evaluator.py`` is importable and re-entrant without
``adapter.py`` in ``sys.modules``.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.0.1"

# Map every public name to the submodule that exports it. This is the single
# source of truth for the lazy API surface; ``__all__`` mirrors its keys.
_LAZY_IMPORTS: dict[str, str] = {
    # autopilot.adapter
    "AdapterConfig": "autopilot.adapter",
    "RawSession": "autopilot.adapter",
    "adapt_session": "autopilot.adapter",
    "load_adapter_config": "autopilot.adapter",
    "load_raw_session": "autopilot.adapter",
    # autopilot.agent_cohort
    "AgentCohortConfig": "autopilot.agent_cohort",
    "AgentCohortExecution": "autopilot.agent_cohort",
    "AgentCohortReport": "autopilot.agent_cohort",
    "load_agent_cohort": "autopilot.agent_cohort",
    "run_agent_cohort": "autopilot.agent_cohort",
    # autopilot.agent_sandbox
    "DeclarativeAgentSandbox": "autopilot.agent_sandbox",
    "DeclarativeAgentSandboxConfig": "autopilot.agent_sandbox",
    "SandboxEvaluationBundle": "autopilot.agent_sandbox",
    "export_run_to_telemetry_interchange": "autopilot.agent_sandbox",
    "load_declarative_sandbox_config": "autopilot.agent_sandbox",
    "run_declarative_sandbox_evaluation": "autopilot.agent_sandbox",
    # autopilot.audit
    "AuditLedger": "autopilot.audit",
    "AuditVerification": "autopilot.audit",
    "KeyRevocation": "autopilot.audit",
    "ReviewRevocation": "autopilot.audit",
    "record_change_card": "autopilot.audit",
    "record_release_review": "autopilot.audit",
    "record_sandbox_report": "autopilot.audit",
    "revoke_release_review": "autopilot.audit",
    "revoke_signing_key": "autopilot.audit",
    "verify_audit_ledger": "autopilot.audit",
    # autopilot.contracts
    "ContractDocument": "autopilot.contracts",
    "load_contract": "autopilot.contracts",
    "parse_contract": "autopilot.contracts",
    # autopilot.diagnostics
    "ChangeCard": "autopilot.diagnostics",
    "ChangeCardBatch": "autopilot.diagnostics",
    "generate_change_cards": "autopilot.diagnostics",
    "render_change_card_markdown": "autopilot.diagnostics",
    # autopilot.evaluator
    "ContractEvaluation": "autopilot.evaluator",
    "evaluate_contract": "autopilot.evaluator",
    # autopilot.execution_boundary
    "BoundaryExecutionPlan": "autopilot.execution_boundary",
    "BoundaryBackend": "autopilot.execution_boundary",
    "BoundaryReport": "autopilot.execution_boundary",
    "BoundaryVerification": "autopilot.execution_boundary",
    "DockerBoundaryConfig": "autopilot.execution_boundary",
    "build_docker_command": "autopilot.execution_boundary",
    "load_boundary_plan": "autopilot.execution_boundary",
    "load_boundary_report": "autopilot.execution_boundary",
    "run_execution_boundary": "autopilot.execution_boundary",
    "verify_boundary_report": "autopilot.execution_boundary",
    # autopilot.incidents
    "Incident": "autopilot.incidents",
    "IncidentBatch": "autopilot.incidents",
    "IncidentRegistry": "autopilot.incidents",
    "detect_incidents": "autopilot.incidents",
    # autopilot.operational_integrity
    "IntegrityCheckResult": "autopilot.operational_integrity",
    "OperationalIntegrityEvaluation": "autopilot.operational_integrity",
    "OperationalObservation": "autopilot.operational_integrity",
    "OperationalTrace": "autopilot.operational_integrity",
    "evaluate_operational_integrity": "autopilot.operational_integrity",
    # autopilot.insurance_agent
    "AgentEvaluationBundle": "autopilot.insurance_agent",
    "AgentRunBundle": "autopilot.insurance_agent",
    "AgentScenario": "autopilot.insurance_agent",
    "CandidateManifest": "autopilot.insurance_agent",
    "InsuranceAgentConfig": "autopilot.insurance_agent",
    "InsuranceClaimsAgent": "autopilot.insurance_agent",
    "load_agent_config": "autopilot.insurance_agent",
    "load_agent_scenario": "autopilot.insurance_agent",
    "run_agent_evaluation": "autopilot.insurance_agent",
    # autopilot.lead_discovery
    "DiscoveryReport": "autopilot.lead_discovery",
    "LeadScore": "autopilot.lead_discovery",
    "QualifiedLead": "autopilot.lead_discovery",
    "SearchResult": "autopilot.lead_discovery",
    "discover_leads": "autopilot.lead_discovery",
    "load_search_results": "autopilot.lead_discovery",
    "write_crm_csv": "autopilot.lead_discovery",
    # autopilot.lead_radar
    "LeadRadarConfig": "autopilot.lead_radar",
    "LeadRadarReport": "autopilot.lead_radar",
    "LeadRadarSource": "autopilot.lead_radar",
    "load_radar_config": "autopilot.lead_radar",
    "run_lead_radar": "autopilot.lead_radar",
    "write_raw_results_csv": "autopilot.lead_radar",
    # autopilot.sandbox
    "SandboxApproval": "autopilot.sandbox",
    "SandboxReport": "autopilot.sandbox",
    "load_change_card": "autopilot.sandbox",
    "run_candidate_sandbox": "autopilot.sandbox",
    # autopilot.release_review
    "ReleaseReview": "autopilot.release_review",
    "ReviewDecision": "autopilot.release_review",
    "ReviewerIdentity": "autopilot.release_review",
    "ReviewVerification": "autopilot.release_review",
    "create_release_review": "autopilot.release_review",
    "generate_review_keypair": "autopilot.release_review",
    "load_release_review": "autopilot.release_review",
    "verify_release_review": "autopilot.release_review",
    # autopilot.situational_assurance
    "analyze_cohort": "autopilot.situational_assurance",
    # autopilot.statistics
    "CohortFPRReport": "autopilot.statistics",
    "WilsonInterval": "autopilot.statistics",
    "false_positive_rate_report": "autopilot.statistics",
    "wilson_score_interval": "autopilot.statistics",
    "z_value_for_confidence": "autopilot.statistics",
    # autopilot.trajectory
    "Trajectory": "autopilot.trajectory",
    "load_trajectory": "autopilot.trajectory",
    "parse_trajectory": "autopilot.trajectory",
}

__all__ = sorted(_LAZY_IMPORTS.keys())


def __getattr__(name: str) -> Any:
    """Lazily import a public attribute from its owning submodule.

    Once accessed, the value is cached in ``globals()`` so subsequent lookups
    skip the ``importlib`` call. Names not in ``_LAZY_IMPORTS`` raise
    ``AttributeError`` with a helpful message.
    """

    module_path = _LAZY_IMPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Support ``dir(autopilot)`` and tab-completion for lazy exports."""

    return sorted(set(globals().keys()) | set(_LAZY_IMPORTS.keys()))
