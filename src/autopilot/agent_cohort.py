"""Cross-product runner for multiple executable agents and scenarios."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from autopilot.adapter import AdapterConfig
from autopilot.agent_sandbox import (
    DeclarativeAgentSandboxConfig,
    run_declarative_sandbox_evaluation,
)
from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import ContractDocument, ExtensibleModel, SnakeName
from autopilot.evaluator import ClauseStatus
from autopilot.insurance_agent import (
    AgentEvaluationBundle,
    AgentExecutionError,
    InsuranceAgentConfig,
    load_agent_scenario,
    run_agent_evaluation,
)
from autopilot.telemetry_interchange import AgentTelemetryExport

COHORT_RUNNER_VERSION = "0.2.1"

#: One retry per episode, as the pressure pilot's operational rules declare: a
#: malformed tool call or a provider hiccup is a technical failure of that
#: episode, not a reason to discard an entire lot. An episode that fails twice is
#: listed instead of silently dropped, because an exclusion nobody can see is
#: indistinguishable from a run that never happened.
MAX_EPISODE_ATTEMPTS = 2


class CohortAgentSource(ExtensibleModel):
    source_id: SnakeName
    config_path: Annotated[str, Field(min_length=1, max_length=500)]


class CohortScenarioSource(ExtensibleModel):
    source_id: SnakeName
    scenario_path: Annotated[str, Field(min_length=1, max_length=500)]


class AgentCohortConfig(ExtensibleModel):
    cohort_version: Literal["0.1"]
    cohort_id: Annotated[str, Field(min_length=1, max_length=255)]
    agents: list[CohortAgentSource] = Field(min_length=2, max_length=100)
    scenarios: list[CohortScenarioSource] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def validate_unique_sources(self) -> AgentCohortConfig:
        for label, values in (
            ("agent", [item.source_id for item in self.agents]),
            ("scenario", [item.source_id for item in self.scenarios]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} source ids must be unique")
        return self


class CohortRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_source_id: str
    scenario_source_id: str
    agent_id: str
    candidate_version: str
    candidate_digest: str
    scenario_digest: str
    status: ClauseStatus
    behavioral_status: str
    operational_status: str
    recommended_autonomy: str
    block_release: bool
    failed_clause_ids: list[str]
    failed_integrity_check_ids: list[str]
    relative_output_dir: str


class CohortRunArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: CohortRunSummary
    bundle: AgentEvaluationBundle
    telemetry_interchange: AgentTelemetryExport | None = None


class CohortTechnicalFailure(BaseModel):
    """An episode that never produced a bundle, so it is excluded, not scored."""

    model_config = ConfigDict(extra="forbid")

    agent_source_id: str
    scenario_source_id: str
    attempts: int = Field(ge=1)
    error: str


class AgentCohortReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    cohort_id: str
    dataset_id: str
    agent_source_count: int = Field(ge=2)
    scenario_source_count: int = Field(ge=2)
    run_count: int = Field(ge=4)
    pass_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    indeterminate_count: int = Field(ge=0)
    candidate_digests: list[str]
    scenario_digests: list[str]
    runs: list[CohortRunSummary]
    technical_failures: list[CohortTechnicalFailure] = Field(default_factory=list)
    runner_version: str = COHORT_RUNNER_VERSION


class AgentCohortExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: AgentCohortReport
    artifacts: list[CohortRunArtifact]


def _canonical_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def load_agent_cohort(path: str | Path) -> tuple[AgentCohortConfig, Path]:
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc
    try:
        return AgentCohortConfig.model_validate(load_yaml_mapping(text)), resolved.parent
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def _load_cohort_agent_source(
    path: str | Path,
) -> InsuranceAgentConfig | DeclarativeAgentSandboxConfig:
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError([ValidationIssue("file", f"not found: {resolved}")]) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc
    raw = load_yaml_mapping(text)
    try:
        if "sandbox_config_version" in raw:
            return DeclarativeAgentSandboxConfig.model_validate(raw)
        return InsuranceAgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def _evaluate_once(
    contract: ContractDocument,
    adapter: AdapterConfig,
    config: InsuranceAgentConfig | DeclarativeAgentSandboxConfig,
    scenario,
) -> tuple[AgentEvaluationBundle, AgentTelemetryExport | None]:
    """Evaluate one episode, whatever kind of agent produced it."""

    if isinstance(config, DeclarativeAgentSandboxConfig):
        sandbox_eval = run_declarative_sandbox_evaluation(contract, adapter, config, scenario)
        return sandbox_eval.agent_evaluation, sandbox_eval.telemetry_interchange
    return run_agent_evaluation(contract, adapter, config, scenario), None


def run_agent_cohort(
    contract: ContractDocument,
    adapter: AdapterConfig,
    cohort: AgentCohortConfig,
    base_dir: Path,
) -> AgentCohortExecution:
    """Execute every configured agent against every configured scenario."""

    artifacts: list[CohortRunArtifact] = []
    failures: list[CohortTechnicalFailure] = []
    for agent_source in cohort.agents:
        config = _load_cohort_agent_source(base_dir / agent_source.config_path)
        for scenario_source in cohort.scenarios:
            scenario = load_agent_scenario(base_dir / scenario_source.scenario_path)
            bundle: AgentEvaluationBundle | None = None
            interchange: AgentTelemetryExport | None = None
            attempts = 0
            error = ""
            while attempts < MAX_EPISODE_ATTEMPTS:
                attempts += 1
                try:
                    bundle, interchange = _evaluate_once(contract, adapter, config, scenario)
                    break
                except AgentExecutionError as exc:
                    error = str(exc)
            if bundle is None:
                failures.append(
                    CohortTechnicalFailure(
                        agent_source_id=agent_source.source_id,
                        scenario_source_id=scenario_source.source_id,
                        attempts=attempts,
                        error=error,
                    )
                )
                continue
            failed = sorted(
                result.clause_id
                for result in bundle.evaluation.clause_results
                if result.status is ClauseStatus.failed
            )
            failed_integrity_checks = sorted(
                result.check_id
                for result in bundle.operational_evaluation.check_results
                if result.status.value == "fail"
            )
            behavioral_status = bundle.evaluation.status
            operational_status = bundle.operational_evaluation.status.value
            if operational_status == "fail":
                overall_status = ClauseStatus.failed
            elif operational_status == "indeterminate":
                overall_status = (
                    behavioral_status
                    if behavioral_status is ClauseStatus.failed
                    else ClauseStatus.indeterminate
                )
            else:
                overall_status = behavioral_status
            if operational_status in {"fail", "indeterminate"}:
                recommended_autonomy = (
                    bundle.operational_evaluation.recommended_autonomy.value
                )
            else:
                recommended_autonomy = bundle.evaluation.recommended_autonomy.value
            run_seed = {
                "agent_source_id": agent_source.source_id,
                "scenario_source_id": scenario_source.source_id,
                "candidate_digest": bundle.agent_run.manifest.candidate_digest,
                "scenario_digest": bundle.agent_run.scenario_sha256,
                "adapter_digest": bundle.adapter_sha256,
            }
            run_digest = _canonical_hash(run_seed)
            relative_dir = f"runs/{agent_source.source_id}/{scenario_source.source_id}"
            summary = CohortRunSummary(
                run_id=f"COHORT-RUN-{run_digest[:24].upper()}",
                agent_source_id=agent_source.source_id,
                scenario_source_id=scenario_source.source_id,
                agent_id=config.agent_id,
                candidate_version=config.candidate_version,
                candidate_digest=bundle.agent_run.manifest.candidate_digest,
                scenario_digest=bundle.agent_run.scenario_sha256,
                status=overall_status,
                behavioral_status=behavioral_status.value,
                operational_status=operational_status,
                recommended_autonomy=recommended_autonomy,
                block_release=(
                    bundle.evaluation.block_release
                    or bundle.operational_evaluation.block_release
                ),
                failed_clause_ids=failed,
                failed_integrity_check_ids=failed_integrity_checks,
                relative_output_dir=relative_dir,
            )
            artifacts.append(
                CohortRunArtifact(
                    summary=summary,
                    bundle=bundle,
                    telemetry_interchange=interchange,
                )
            )

    summaries = [item.summary for item in artifacts]
    dataset_digest = _canonical_hash(
        {
            "cohort_id": cohort.cohort_id,
            "runs": [item.model_dump(mode="json") for item in summaries],
            "runner_version": COHORT_RUNNER_VERSION,
        }
    )
    statuses = [item.status for item in summaries]
    report = AgentCohortReport(
        cohort_id=cohort.cohort_id,
        dataset_id=f"DATASET-{dataset_digest[:24].upper()}",
        agent_source_count=len(cohort.agents),
        scenario_source_count=len(cohort.scenarios),
        run_count=len(summaries),
        pass_count=statuses.count(ClauseStatus.passed),
        fail_count=statuses.count(ClauseStatus.failed),
        indeterminate_count=statuses.count(ClauseStatus.indeterminate),
        candidate_digests=sorted({item.candidate_digest for item in summaries}),
        scenario_digests=sorted({item.scenario_digest for item in summaries}),
        runs=summaries,
        technical_failures=failures,
    )
    return AgentCohortExecution(report=report, artifacts=artifacts)
