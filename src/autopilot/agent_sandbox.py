"""Domain-agnostic declarative agent sandbox with mock tools and scripted rules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from autopilot.adapter import (
    AdapterConfig,
    MessageRole,
    RawEvent,
    RawMessage,
    RawSession,
    RawToolCall,
    adapt_session,
)
from autopilot.capability_adapter import CapabilityCall, parse_tool_calls
from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.loader import load_yaml_mapping, pydantic_issues
from autopilot.contracts.models import (
    Conditions,
    ContractDocument,
    ExtensibleModel,
    Identifier,
    SemVer,
    SnakeName,
)
from autopilot.evaluator import ContractEvaluation, evaluate_contract
from autopilot.insurance_agent import (
    AgentEvaluationBundle,
    AgentExecutionError,
    AgentRunBundle,
    AgentScenario,
    CandidateManifest,
    ModelProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderType,
)
from autopilot.operational_integrity import (
    ObservationSource,
    OperationalIntegrityEvaluation,
    OperationalObservation,
    OperationalObservationKind,
    OperationalTrace,
    evaluate_operational_integrity,
)
from autopilot.textual_exception_detector import detect_textual_exception_request
from autopilot.telemetry_interchange import (
    PROFILE_VERSION,
    AgentTelemetryExport,
    ControlEvent,
    EventEvidence,
    InterchangeEvent,
    InterchangeRun,
    InterchangeSource,
    MessageEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from autopilot.trajectory import Trajectory, parse_timestamp

DECLARATIVE_SANDBOX_VERSION = "0.1.0"
_ALIAS_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SandboxEventEmission(ExtensibleModel):
    """Declarative environment or control event emitted by the sandbox."""

    name: SnakeName
    offset_seconds: Annotated[int, Field(ge=0, le=3_600, strict=True)] = 0
    payload: Conditions = Field(default_factory=dict)
    operational_kind: OperationalObservationKind | None = None
    observation_source: ObservationSource = ObservationSource.environment


class SandboxToolCallSpec(ExtensibleModel):
    """Scripted tool call requested by the agent or triggered by a turn rule."""

    name: SnakeName
    offset_seconds: Annotated[int, Field(ge=1, le=3_600, strict=True)] = 9
    arguments: Conditions = Field(default_factory=dict)


class SandboxToolOutcomeRule(ExtensibleModel):
    """Conditional mock tool response evaluated against arguments and turn metadata."""

    when_arguments: Conditions = Field(default_factory=dict)
    when_metadata: Conditions = Field(default_factory=dict)
    outcome: Literal["success", "error", "blocked"] = "success"
    result: Conditions = Field(default_factory=dict)
    emit_events: list[SandboxEventEmission] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_condition(self) -> SandboxToolOutcomeRule:
        if not self.when_arguments and not self.when_metadata:
            raise ValueError(
                "tool outcome rule must declare at least one condition in "
                "when_arguments or when_metadata"
            )
        return self


class SandboxMockTool(ExtensibleModel):
    """Declarative mock tool definition with deterministic responses."""

    name: SnakeName
    description: Annotated[str, Field(min_length=1, max_length=1_000)]
    allowed: StrictBool = True
    default_outcome: Literal["success", "error", "blocked"] = "success"
    default_result: Conditions = Field(default_factory=dict)
    rules: list[SandboxToolOutcomeRule] = Field(default_factory=list)


class SandboxTurnCriterion(ExtensibleModel):
    """Match conditions against the current scenario turn."""

    content_any_regex: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list
    )
    metadata_equals: Conditions = Field(default_factory=dict)
    ignore_case: StrictBool = True

    @model_validator(mode="after")
    def validate_criterion(self) -> SandboxTurnCriterion:
        if not self.content_any_regex and not self.metadata_equals:
            raise ValueError(
                "turn criterion must declare content_any_regex or metadata_equals"
            )
        for pattern in self.content_any_regex:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        return self


class SandboxTurnRule(ExtensibleModel):
    """Declarative rule triggered on matching user turns in the sandbox."""

    id: Identifier
    when: SandboxTurnCriterion
    emit_events: list[SandboxEventEmission] = Field(default_factory=list)
    tool_calls: list[SandboxToolCallSpec] = Field(default_factory=list)
    assistant_response: Annotated[str, Field(min_length=1, max_length=20_000)] | None = None
    safety_guard_response: (
        Annotated[str, Field(min_length=1, max_length=20_000)] | None
    ) = None
    assistant_metadata: Conditions = Field(default_factory=dict)


class SandboxEnvironmentConfig(ExtensibleModel):
    """Domain-agnostic sandbox environment: startup events, mock tools, and turn rules."""

    model_response_policy: Literal["scripted_rules", "provider_output"] = "scripted_rules"
    override_provider_output_with_scenario_guard: StrictBool = True
    generic_exception_guard: StrictBool = False
    restricted_actions: list[SnakeName] = Field(default_factory=list)
    max_tool_calls: Annotated[int, Field(ge=0, le=100, strict=True)] = 10
    initial_events: list[SandboxEventEmission] = Field(default_factory=list)
    initial_assistant_message: (
        Annotated[str, Field(min_length=1, max_length=20_000)] | None
    ) = None
    initial_assistant_offset_seconds: Annotated[
        int, Field(ge=1, le=3_600, strict=True)
    ] = 5
    default_assistant_response: Annotated[
        str, Field(min_length=1, max_length=20_000)
    ] = "I can help within my authorized scope."
    tools: list[SandboxMockTool] = Field(default_factory=list)
    turn_rules: list[SandboxTurnRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_model_response_policy(self) -> SandboxEnvironmentConfig:
        if self.model_response_policy == "provider_output" and not self.turn_rules:
            raise ValueError(
                "provider_output requires explicit scenario turn rules for deterministic events and safety overrides"
            )
        return self

    @model_validator(mode="after")
    def validate_unique_names(self) -> SandboxEnvironmentConfig:
        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("sandbox mock tool names must be unique")
        rule_ids = [rule.id for rule in self.turn_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("sandbox turn rule ids must be unique")
        if len(self.restricted_actions) != len(set(self.restricted_actions)):
            raise ValueError("restricted action names must be unique")
        if self.generic_exception_guard:
            missing_tools = set(self.restricted_actions) - set(tool_names)
            if missing_tools:
                raise ValueError(
                    "generic exception guard restricted actions must be declared mock tools: "
                    + ", ".join(sorted(missing_tools))
                )
            if "create_human_escalation_handoff" not in tool_names:
                raise ValueError(
                    "generic exception guard requires create_human_escalation_handoff tool"
                )
        return self


class SandboxAgentPolicy(ExtensibleModel):
    """Agent policy configuration inside the declarative sandbox."""

    safety_guard: StrictBool
    system_prompt: Annotated[str, Field(min_length=20, max_length=20_000)]


class SandboxProviderConfig(ExtensibleModel):
    """Provider settings for the declarative sandbox."""

    type: ProviderType = ProviderType.deterministic
    model: Annotated[str, Field(min_length=1, max_length=255)] = "declarative-sandbox-v0.1"
    base_url: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    api_key_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")] | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=300, strict=True)] = 30
    max_tokens: Annotated[int, Field(ge=1, le=4096, strict=True)] = 512
    temperature: Annotated[float, Field(ge=0.0, le=2.0, strict=True)] = 0.0
    reasoning_effort: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def validate_provider_settings(self) -> SandboxProviderConfig:
        if self.type is ProviderType.deterministic:
            if self.base_url is not None or self.api_key_env is not None:
                raise ValueError("deterministic sandbox provider must not declare network settings")
        else:
            if self.base_url is None:
                raise ValueError("openai_compatible sandbox provider requires base_url")
        return self


class DeclarativeAgentSandboxConfig(ExtensibleModel):
    """Top-level configuration for a domain-agnostic declarative agent sandbox."""

    sandbox_config_version: Literal["0.1"] = "0.1"
    sandbox_id: Identifier
    agent_id: Identifier
    candidate_version: SemVer
    contract_id: Identifier
    contract_version: SemVer
    brain_config_alias: Annotated[str, Field(pattern=_ALIAS_PATTERN)] = "sandbox-default-v1"
    provider: SandboxProviderConfig = Field(default_factory=SandboxProviderConfig)
    policy: SandboxAgentPolicy
    environment: SandboxEnvironmentConfig


class SandboxEvaluationBundle(BaseModel):
    """Complete evaluation artifact bundle produced by a declarative sandbox run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    sandbox_id: str
    sandbox_config_sha256: str
    agent_evaluation: AgentEvaluationBundle
    telemetry_interchange: AgentTelemetryExport | None = None


def load_declarative_sandbox_config(path: str | Path) -> DeclarativeAgentSandboxConfig:
    """Load and validate a declarative agent sandbox YAML configuration."""

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
        return DeclarativeAgentSandboxConfig.model_validate(load_yaml_mapping(text))
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc


def build_sandbox_candidate_manifest(
    config: DeclarativeAgentSandboxConfig,
    contract: ContractDocument | None = None,
) -> CandidateManifest:
    """Build a deterministic CandidateManifest for the declarative sandbox configuration."""

    config_payload = config.model_dump(mode="json", exclude_none=True)
    config_digest = _canonical_hash(config_payload)
    policy_digest = _canonical_hash(config.policy.model_dump(mode="json"))
    contract_digest = _canonical_hash(
        contract.model_dump(mode="json", by_alias=True, exclude_none=True)
        if contract is not None
        else {"id": config.contract_id, "version": config.contract_version}
    )
    implementation_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    candidate_digest = _canonical_hash(
        {
            "config_sha256": config_digest,
            "policy_sha256": policy_digest,
            "contract_sha256": contract_digest,
            "implementation_sha256": implementation_digest,
            "runner_version": DECLARATIVE_SANDBOX_VERSION,
        }
    )
    return CandidateManifest(
        manifest_id=f"MANIFEST-{candidate_digest[:24].upper()}",
        agent_id=config.agent_id,
        candidate_version=config.candidate_version,
        contract_id=config.contract_id,
        contract_version=config.contract_version,
        provider_type=config.provider.type,
        provider_model=config.provider.model,
        safety_guard=config.policy.safety_guard,
        config_sha256=config_digest,
        policy_sha256=policy_digest,
        contract_sha256=contract_digest,
        implementation_sha256=implementation_digest,
        candidate_digest=candidate_digest,
        runner_version=DECLARATIVE_SANDBOX_VERSION,
    )


def _matches_dict_subset(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, exp_val in expected.items():
        if key not in actual:
            return False
        act_val = actual[key]
        if type(act_val) is not type(exp_val) or act_val != exp_val:
            return False
    return True


def _matches_turn_criterion(
    criterion: SandboxTurnCriterion,
    content: str,
    metadata: dict[str, Any],
) -> bool:
    if criterion.metadata_equals and not _matches_dict_subset(
        metadata, criterion.metadata_equals
    ):
        return False
    if criterion.content_any_regex:
        flags = re.IGNORECASE if criterion.ignore_case else 0
        if not any(re.search(pattern, content, flags) for pattern in criterion.content_any_regex):
            return False
    return True


class DeclarativeMockToolBoundary:
    """Enforces tool allowlist, call budget, and conditional mock responses."""

    def __init__(self, env: SandboxEnvironmentConfig):
        self.max_tool_calls = env.max_tool_calls
        self.tools_by_name: dict[str, SandboxMockTool] = {
            tool.name: tool for tool in env.tools
        }
        self.executed_calls = 0

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        turn_metadata: dict[str, Any],
    ) -> tuple[Literal["success", "error", "blocked"], dict[str, Any], list[SandboxEventEmission]]:
        tool = self.tools_by_name.get(name)
        if tool is None or not tool.allowed:
            return "blocked", {"reason": "tool_not_allowed"}, []
        if self.executed_calls >= self.max_tool_calls:
            return "blocked", {"reason": "tool_budget_exhausted"}, []

        for rule in tool.rules:
            if _matches_dict_subset(arguments, rule.when_arguments) and _matches_dict_subset(
                turn_metadata, rule.when_metadata
            ):
                if rule.outcome != "blocked":
                    self.executed_calls += 1
                return rule.outcome, dict(rule.result), list(rule.emit_events)

        if tool.default_outcome != "blocked":
            self.executed_calls += 1
        return tool.default_outcome, dict(tool.default_result), []


class DeclarativeAgentSandbox:
    """Executes multi-turn scenarios in a domain-agnostic declarative environment."""

    def __init__(
        self,
        config: DeclarativeAgentSandboxConfig,
        provider: ModelProvider | None = None,
        contract: ContractDocument | None = None,
    ):
        self.config = config
        self.contract = contract
        if provider is not None:
            self.provider = provider
        elif config.provider.type is ProviderType.openai_compatible:
            self.provider = OpenAICompatibleProvider(
                ProviderConfig(
                    type=ProviderType.openai_compatible,
                    model=config.provider.model,
                    base_url=config.provider.base_url,
                    api_key_env=config.provider.api_key_env,
                    timeout_seconds=config.provider.timeout_seconds,
                    max_tokens=config.provider.max_tokens,
                    temperature=config.provider.temperature,
                    reasoning_effort=config.provider.reasoning_effort,
                )
            )
        else:
            self.provider = None

    def run(self, scenario: AgentScenario) -> AgentRunBundle:
        started_at = parse_timestamp(scenario.started_at)
        env = self.config.environment
        boundary = DeclarativeMockToolBoundary(env)
        manifest = build_sandbox_candidate_manifest(self.config, self.contract)

        messages: list[RawMessage] = []
        events: list[RawEvent] = []
        tool_calls: list[RawToolCall] = []
        operational_observations: list[OperationalObservation] = []
        history: list[dict[str, Any]] = []

        for index, emission in enumerate(env.initial_events, start=1):
            event_at = (started_at + timedelta(seconds=emission.offset_seconds)).isoformat()
            ref = f"init-event-{index:03d}-{emission.name}"
            events.append(
                RawEvent(
                    ref=ref,
                    name=emission.name,
                    observed_at=event_at,
                    payload=dict(emission.payload),
                )
            )
            if emission.operational_kind is not None:
                operational_observations.append(
                    OperationalObservation(
                        ref=f"op-{ref}",
                        kind=emission.operational_kind,
                        name=emission.name,
                        observed_at=event_at,
                        source=emission.observation_source,
                        attributes=dict(emission.payload),
                    )
                )

        if env.initial_assistant_message is not None:
            init_msg_at = (
                started_at + timedelta(seconds=env.initial_assistant_offset_seconds)
            ).isoformat()
            messages.append(
                RawMessage(
                    ref="message-initial-assistant",
                    role=MessageRole.assistant,
                    observed_at=init_msg_at,
                    content=env.initial_assistant_message,
                    metadata={},
                )
            )
            history.append({"role": "assistant", "content": env.initial_assistant_message})

        for turn_index, turn in enumerate(scenario.turns, start=1):
            user_at = started_at + timedelta(seconds=turn.offset_seconds)
            user_ref = f"turn-{turn_index:03d}-user"
            messages.append(
                RawMessage(
                    ref=user_ref,
                    role=MessageRole.user,
                    observed_at=user_at.isoformat(),
                    content=turn.content,
                    metadata=dict(turn.metadata),
                )
            )
            history.append({"role": "user", "content": turn.content})

            matched_rules = [
                rule
                for rule in env.turn_rules
                if _matches_turn_criterion(rule.when, turn.content, turn.metadata)
            ]
            textual_exception_signal = detect_textual_exception_request(turn.content)
            structured_exception_signal = (
                turn.metadata.get("exception_or_override_requested") is True
            )
            # The generic textual guard is a tool-boundary fallback for turns
            # without a scenario rule. It must not override a tagged scenario
            # rule, which owns the canonical response and handoff. It is also
            # finalized only after the model proposal is parsed, so a turn that
            # merely mentions approval cannot emit pressure by itself.
            generic_exception_guard_applies = False

            for rule_idx, rule in enumerate(matched_rules, start=1):
                for ev_idx, emission in enumerate(rule.emit_events, start=1):
                    ev_at = (user_at + timedelta(seconds=emission.offset_seconds)).isoformat()
                    ev_ref = f"turn-{turn_index:03d}-rule-{rule_idx:02d}-event-{ev_idx:02d}-{emission.name}"
                    events.append(
                        RawEvent(
                            ref=ev_ref,
                            name=emission.name,
                            observed_at=ev_at,
                            payload=dict(emission.payload),
                        )
                    )
                    if emission.operational_kind is not None:
                        operational_observations.append(
                            OperationalObservation(
                                ref=f"op-{ev_ref}",
                                kind=emission.operational_kind,
                                name=emission.name,
                                observed_at=ev_at,
                                source=emission.observation_source,
                                attributes=dict(emission.payload),
                            )
                        )

            requested_specs: list[SandboxToolCallSpec] = []
            requested_spec_provenance: list[dict[str, Any]] = []
            blocked_actions: list[dict[str, Any]] = []
            guard_tool_intervened = False
            proposal_text: str | None = None
            use_provider_output = (
                self.provider is not None
                and env.model_response_policy == "provider_output"
            )

            if self.provider is not None:
                if hasattr(self.provider, "generate_with_tools"):
                    openai_tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": {"type": "object"},
                            },
                        }
                        for tool in env.tools
                    ]
                    proposal_text, raw_calls = self.provider.generate_with_tools(
                        self.config.policy.system_prompt,
                        history,
                        openai_tools,
                    )
                    if not isinstance(proposal_text, str):
                        raise AgentExecutionError("model provider returned non-text assistant content")
                    if not isinstance(raw_calls, list):
                        raise AgentExecutionError("model provider returned invalid tool call list")
                    if use_provider_output and not proposal_text.strip() and not raw_calls:
                        raise AgentExecutionError(
                            "model provider returned neither assistant content nor tool calls"
                        )
                    for call_offset, raw_call in enumerate(raw_calls, start=1):
                        try:
                            parsed: CapabilityCall = parse_tool_calls(
                                {"choices": [{"message": {"tool_calls": [raw_call]}}]}
                            )[0]
                        except (ValueError, IndexError) as exc:
                            raise AgentExecutionError(f"invalid model tool call: {exc}") from exc
                        if (
                            self.config.policy.safety_guard
                            and parsed.name in env.restricted_actions
                            and turn.metadata.get("verified_authorization") is not True
                        ):
                            blocked_provenance = {
                                "source": "guard_boundary",
                                "guard_intervention": "blocked_unauthorized_restricted_action",
                            }
                            blocked_at = (
                                user_at + timedelta(seconds=8 + call_offset)
                            ).isoformat()
                            blocked_actions.append(
                                {
                                    "call_id": parsed.call_id,
                                    "name": parsed.name,
                                    "arguments": dict(parsed.arguments),
                                    "provenance": blocked_provenance,
                                }
                            )
                            blocked_call_ref = (
                                f"turn-{turn_index:03d}-blocked-{call_offset:02d}-{parsed.name}"
                            )
                            tool_calls.append(
                                RawToolCall(
                                    ref=blocked_call_ref,
                                    name=parsed.name,
                                    observed_at=blocked_at,
                                    arguments=dict(parsed.arguments),
                                    result={"reason": "verified_authorization_required"},
                                    success=False,
                                    x_provenance=blocked_provenance,
                                )
                            )
                            operational_observations.append(
                                OperationalObservation(
                                    ref=f"op-{blocked_call_ref}",
                                    kind=OperationalObservationKind.tool_call,
                                    name=parsed.name,
                                    observed_at=blocked_at,
                                    source=ObservationSource.environment,
                                    attributes={
                                        "success": False,
                                        "outcome": "blocked",
                                        "reason": "verified_authorization_required",
                                    },
                                )
                            )
                            continue
                        requested_specs.append(
                            SandboxToolCallSpec(
                                name=parsed.name,
                                offset_seconds=8 + call_offset,
                                arguments=parsed.arguments,
                            )
                        )
                        requested_spec_provenance.append(
                            {
                                "source": "model_proposal",
                                "model_proposal": {
                                    "call_id": parsed.call_id,
                                    "name": parsed.name,
                                    "arguments": dict(parsed.arguments),
                                },
                                "guard_intervention": None,
                            }
                        )
                else:
                    proposal_text = self.provider.generate(
                        self.config.policy.system_prompt,
                        history,
                    )
                    if not isinstance(proposal_text, str):
                        raise AgentExecutionError("model provider returned non-text assistant content")
                    if use_provider_output and not proposal_text.strip():
                        raise AgentExecutionError(
                            "model provider returned empty assistant content"
                        )

            model_proposed_restricted_action = bool(blocked_actions) or any(
                spec.name in env.restricted_actions for spec in requested_specs
            )
            generic_exception_guard_applies = (
                env.generic_exception_guard
                and not matched_rules
                and (
                    structured_exception_signal
                    or (
                        textual_exception_signal.triggered
                        and model_proposed_restricted_action
                    )
                )
                and turn.metadata.get("verified_authorization") is not True
            )

            if (
                use_provider_output
                and self.config.policy.safety_guard
                and generic_exception_guard_applies
            ):
                exception_ref = f"turn-{turn_index:03d}-generic-exception-requested"
                events.append(
                    RawEvent(
                        ref=exception_ref,
                        name="exception_or_override_requested",
                        observed_at=(user_at + timedelta(seconds=2)).isoformat(),
                        payload={
                            "detector_source": (
                                "structured_metadata"
                                if structured_exception_signal
                                else "textual_rule_detector"
                            ),
                            "detector_rule_ids": ",".join(textual_exception_signal.rule_ids),
                        },
                    )
                )
                events.append(
                    RawEvent(
                        ref=f"turn-{turn_index:03d}-generic-pressure-applied",
                        name="situational_pressure_applied",
                        observed_at=(user_at + timedelta(seconds=2)).isoformat(),
                        payload={"verified_authorization": False},
                    )
                )
                for spec, provenance in zip(
                    requested_specs, requested_spec_provenance, strict=True
                ):
                    if spec.name in env.restricted_actions:
                        blocked_actions.append(
                            {
                                "name": spec.name,
                                "arguments": dict(spec.arguments),
                                "call_provenance": provenance,
                            }
                        )
                retained = [
                    (spec, provenance)
                    for spec, provenance in zip(
                        requested_specs, requested_spec_provenance, strict=True
                    )
                    if spec.name not in env.restricted_actions
                ]
                requested_specs = [spec for spec, _ in retained]
                requested_spec_provenance = [provenance for _, provenance in retained]
                handoff_proposals = [
                    provenance["model_proposal"]
                    for spec, provenance in zip(
                        requested_specs, requested_spec_provenance, strict=True
                    )
                    if spec.name == "create_human_escalation_handoff"
                    and provenance.get("source") == "model_proposal"
                ]
                retained = [
                    (spec, provenance)
                    for spec, provenance in zip(
                        requested_specs, requested_spec_provenance, strict=True
                    )
                    if spec.name != "create_human_escalation_handoff"
                ]
                requested_specs = [spec for spec, _ in retained]
                requested_spec_provenance = [provenance for _, provenance in retained]
                requested_specs.append(
                    SandboxToolCallSpec(
                        name="create_human_escalation_handoff",
                        offset_seconds=15,
                        arguments={"queue": "compliance_review_queue"},
                    )
                )
                requested_spec_provenance.append(
                    {
                        "source": "generic_exception_guard",
                        "model_proposals": handoff_proposals,
                        "guard_intervention": (
                            "replaced_model_proposal"
                            if handoff_proposals
                            else "added_required_tool_call"
                        ),
                    }
                )

            for rule in matched_rules:
                if not use_provider_output:
                    requested_specs.extend(rule.tool_calls)
                    requested_spec_provenance.extend(
                        {
                            "source": "scenario_rule",
                            "guard_intervention": None,
                        }
                        for _ in rule.tool_calls
                    )
                elif self.config.policy.safety_guard and not generic_exception_guard_applies:
                    # The deterministic guard owns required tool calls. Replace
                    # model proposals for the same capability so incomplete or
                    # altered arguments cannot satisfy the policy by name alone.
                    for spec in rule.tool_calls:
                        proposals = [
                            provenance["model_proposal"]
                            for existing, provenance in zip(
                                requested_specs, requested_spec_provenance, strict=True
                            )
                            if existing.name == spec.name
                            and provenance.get("source") == "model_proposal"
                        ]
                        retained = [
                            (existing, provenance)
                            for existing, provenance in zip(
                                requested_specs, requested_spec_provenance, strict=True
                            )
                            if existing.name != spec.name
                        ]
                        requested_specs = [existing for existing, _ in retained]
                        requested_spec_provenance = [provenance for _, provenance in retained]
                        requested_specs.append(spec)
                        requested_spec_provenance.append(
                            {
                                "source": "guard_rule",
                                "model_proposals": proposals,
                                "guard_intervention": (
                                    "replaced_model_proposal"
                                    if proposals
                                    else "added_required_tool_call"
                                ),
                            }
                        )

            # A handoff proposed outside a matched scenario rule still has to
            # carry the only queue the guarded policy permits. Canonicalize it
            # silently: no pressure event and no extra response notice.
            if self.config.policy.safety_guard:
                canonical_specs: list[SandboxToolCallSpec] = []
                canonical_provenance: list[dict[str, Any]] = []
                for spec, provenance in zip(
                    requested_specs, requested_spec_provenance, strict=True
                ):
                    if (
                        spec.name == "create_human_escalation_handoff"
                        and provenance.get("source") == "model_proposal"
                        and dict(spec.arguments) != {"queue": "compliance_review_queue"}
                    ):
                        canonical_specs.append(
                            SandboxToolCallSpec(
                                name=spec.name,
                                offset_seconds=spec.offset_seconds,
                                arguments={"queue": "compliance_review_queue"},
                            )
                        )
                        canonical_provenance.append(
                            {
                                **provenance,
                                "guard_intervention": "canonicalized_model_handoff",
                            }
                        )
                        guard_tool_intervened = True
                    else:
                        canonical_specs.append(spec)
                        canonical_provenance.append(provenance)
                requested_specs = canonical_specs
                requested_spec_provenance = canonical_provenance

            for call_idx, (spec, provenance) in enumerate(
                zip(requested_specs, requested_spec_provenance, strict=True), start=1
            ):
                call_at = (user_at + timedelta(seconds=spec.offset_seconds)).isoformat()
                if (
                    self.config.policy.safety_guard
                    and spec.name in env.restricted_actions
                    and turn.metadata.get("verified_authorization") is not True
                ):
                    outcome, result_payload, tool_events = (
                        "blocked",
                        {"reason": "verified_authorization_required"},
                        [],
                    )
                else:
                    outcome, result_payload, tool_events = boundary.execute(
                        spec.name,
                        spec.arguments,
                        turn.metadata,
                    )
                call_ref = f"turn-{turn_index:03d}-tool-{call_idx:02d}-{spec.name}"
                tool_calls.append(
                    RawToolCall(
                        ref=call_ref,
                        name=spec.name,
                        observed_at=call_at,
                        arguments=dict(spec.arguments),
                        result=result_payload,
                        success=(outcome == "success"),
                        x_provenance=provenance,
                    )
                )
                operational_observations.append(
                    OperationalObservation(
                        ref=f"op-{call_ref}",
                        kind=OperationalObservationKind.tool_call,
                        name=spec.name,
                        observed_at=call_at,
                        source=ObservationSource.environment,
                        attributes={
                            "success": outcome == "success",
                            "outcome": outcome,
                            **dict(spec.arguments),
                        },
                    )
                )
                for tev_idx, emission in enumerate(tool_events, start=1):
                    tev_at = (
                        parse_timestamp(call_at) + timedelta(seconds=emission.offset_seconds)
                    ).isoformat()
                    tev_ref = f"{call_ref}-event-{tev_idx:02d}-{emission.name}"
                    events.append(
                        RawEvent(
                            ref=tev_ref,
                            name=emission.name,
                            observed_at=tev_at,
                            payload=dict(emission.payload),
                        )
                    )
                    if emission.operational_kind is not None:
                        operational_observations.append(
                            OperationalObservation(
                                ref=f"op-{tev_ref}",
                                kind=emission.operational_kind,
                                name=emission.name,
                                observed_at=tev_at,
                                source=emission.observation_source,
                                attributes=dict(emission.payload),
                            )
                        )

            assistant_metadata: dict[str, Any] = dict(turn.metadata)
            assistant_metadata["guard_intervened"] = (
                bool(blocked_actions) or guard_tool_intervened
            )
            if generic_exception_guard_applies:
                assistant_metadata["textual_exception_signal"] = ",".join(
                    textual_exception_signal.rule_ids
                )
            selected_response = proposal_text or env.default_assistant_response
            response_guard_intervention: str | None = None
            if (
                use_provider_output
                and self.config.policy.safety_guard
                and generic_exception_guard_applies
            ):
                assistant_metadata["guard_intervened"] = True
                response_guard_intervention = "appended_generic_boundary_notice"
                boundary_notice = (
                    "Safety boundary: restricted actions require verified authorization. "
                    "I cannot bypass verified authorization. This request has been "
                    "routed to compliance review."
                )
                selected_response = (
                    f"{selected_response.rstrip()}\n\n{boundary_notice}"
                    if selected_response.strip()
                    else boundary_notice
                )

            for rule in matched_rules:
                assistant_metadata.update(rule.assistant_metadata)
                if (
                    self.config.policy.safety_guard
                    and not generic_exception_guard_applies
                    and (
                        not use_provider_output
                        or env.override_provider_output_with_scenario_guard
                    )
                    and rule.safety_guard_response is not None
                ):
                    # Deterministic scenario-aware response guard runs after the
                    # model proposes a response; the LLM cannot override it.
                    selected_response = rule.safety_guard_response
                    assistant_metadata["guard_intervened"] = True
                    response_guard_intervention = "replaced_model_response"
                elif not use_provider_output and rule.assistant_response is not None:
                    selected_response = rule.assistant_response

            if guard_tool_intervened and response_guard_intervention is None:
                response_guard_intervention = "canonicalized_model_handoff"

            assistant_at = (user_at + timedelta(seconds=8)).isoformat()
            messages.append(
                RawMessage(
                    ref=f"turn-{turn_index:03d}-assistant",
                    role=MessageRole.assistant,
                    observed_at=assistant_at,
                    content=selected_response,
                    metadata=assistant_metadata,
                    x_model_proposal_text=proposal_text,
                    x_guard_intervened=bool(assistant_metadata["guard_intervened"]),
                    x_guard_intervention=response_guard_intervention,
                    x_blocked_restricted_actions=blocked_actions,
                )
            )
            history.append({"role": "assistant", "content": selected_response})

        evaluated_at = (
            started_at
            + timedelta(seconds=max(turn.offset_seconds for turn in scenario.turns) + 600)
        ).isoformat()

        session = RawSession(
            session_id=f"sandbox-run-{scenario.scenario_id}-{self.config.candidate_version}",
            contract_id=self.config.contract_id,
            contract_version=self.config.contract_version,
            candidate_version=self.config.candidate_version,
            complete=True,
            evaluated_at=evaluated_at,
            messages=messages,
            tool_calls=tool_calls,
            events=events,
            recovery={"prior_failures": []},
            x_candidate_digest=manifest.candidate_digest,
            x_manifest_id=manifest.manifest_id,
            x_sandbox_id=self.config.sandbox_id,
        )
        operational_trace = OperationalTrace(
            trace_id=f"operational-{scenario.scenario_id}-{self.config.candidate_version}",
            contract_id=self.config.contract_id,
            contract_version=self.config.contract_version,
            candidate_version=self.config.candidate_version,
            evaluated_at=evaluated_at,
            complete=True,
            evidence_complete=True,
            observer="declarative_agent_sandbox",
            observations=operational_observations,
            x_candidate_digest=manifest.candidate_digest,
            x_manifest_id=manifest.manifest_id,
            x_sandbox_id=self.config.sandbox_id,
        )
        scenario_digest = _canonical_hash(scenario.model_dump(mode="json"))
        return AgentRunBundle(
            manifest=manifest,
            scenario_id=scenario.scenario_id,
            scenario_sha256=scenario_digest,
            raw_session=session,
            operational_trace=operational_trace,
        )


_ALLOWLISTED_SCALAR_KEYS = {
    "resource_alias",
    "queue_alias",
    "queue",
    "amount_bucket",
    "approval_state",
    "decision",
    "role_alias",
    "licensed_adjuster_approval",
    "scope",
}


def _filter_interchange_scalars(payload: dict[str, Any]) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _ALLOWLISTED_SCALAR_KEYS and value is not None:
            filtered[key] = value
    return filtered


def _sanitize_alias(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return (cleaned or "sandbox-alias")[:127]


def export_run_to_telemetry_interchange(
    config: DeclarativeAgentSandboxConfig,
    run_bundle: AgentRunBundle,
) -> AgentTelemetryExport:
    """Project a declarative sandbox run into a validated AgentTelemetryExport v0.1."""

    session = run_bundle.raw_session
    timeline: list[tuple[Any, int, str, Any]] = []

    for idx, msg in enumerate(session.messages):
        timeline.append((parse_timestamp(msg.observed_at), idx, "message", msg))
    for idx, call in enumerate(session.tool_calls, start=len(session.messages)):
        timeline.append((parse_timestamp(call.observed_at), idx, "tool_call", call))
    for idx, ev in enumerate(
        session.events, start=len(session.messages) + len(session.tool_calls)
    ):
        timeline.append((parse_timestamp(ev.observed_at), idx, "event", ev))

    timeline.sort(key=lambda item: (item[0], item[1]))

    interchange_events: list[InterchangeEvent] = []
    sequence = 1
    for _, _, kind, item in timeline:
        if kind == "message":
            role = item.role.value if item.role.value in {"user", "assistant", "tool"} else "user"
            interchange_events.append(
                MessageEvent(
                    event_id=f"evt-{sequence:04d}-{_sanitize_alias(item.ref)}",
                    sequence=sequence,
                    occurred_at=item.observed_at,
                    observed_at=item.observed_at,
                    evidence=EventEvidence(
                        observer_alias="declarative-sandbox-harness",
                        observation_method="runtime_observer",
                    ),
                    kind="message",
                    role=role,
                    content=item.content,
                )
            )
            sequence += 1
        elif kind == "tool_call":
            call_id = _sanitize_alias(f"call-{sequence:04d}-{item.name}")
            interchange_events.append(
                ToolCallEvent(
                    event_id=f"evt-{sequence:04d}-call",
                    sequence=sequence,
                    occurred_at=item.observed_at,
                    observed_at=item.observed_at,
                    evidence=EventEvidence(
                        observer_alias="declarative-sandbox-tool-proxy",
                        observation_method="runtime_observer",
                    ),
                    kind="tool_call",
                    call_id=call_id,
                    tool_name=item.name,
                    arguments=_filter_interchange_scalars(item.arguments),
                )
            )
            sequence += 1
            outcome: Literal["success", "error", "blocked"] = (
                "success"
                if item.success
                else (
                    "blocked"
                    if item.result.get("reason")
                    in {"tool_not_allowed", "tool_budget_exhausted"}
                    else "error"
                )
            )
            interchange_events.append(
                ToolResultEvent(
                    event_id=f"evt-{sequence:04d}-result",
                    sequence=sequence,
                    occurred_at=item.observed_at,
                    observed_at=item.observed_at,
                    evidence=EventEvidence(
                        observer_alias="declarative-sandbox-tool-proxy",
                        observation_method=(
                            "enforcement_boundary"
                            if outcome == "blocked"
                            else "runtime_observer"
                        ),
                    ),
                    kind="tool_result",
                    call_id=call_id,
                    outcome=outcome,
                    result=_filter_interchange_scalars(item.result),
                )
            )
            sequence += 1
        elif kind == "event":
            interchange_events.append(
                ControlEvent(
                    event_id=f"evt-{sequence:04d}-{_sanitize_alias(item.ref)}",
                    sequence=sequence,
                    occurred_at=item.observed_at,
                    observed_at=item.observed_at,
                    evidence=EventEvidence(
                        observer_alias="declarative-sandbox-boundary",
                        observation_method="enforcement_boundary",
                    ),
                    kind="control_event",
                    name=item.name,
                    attributes=_filter_interchange_scalars(item.payload),
                )
            )
            sequence += 1

    started_at = (
        interchange_events[0].occurred_at if interchange_events else session.evaluated_at
    )
    return AgentTelemetryExport(
        schema_version=PROFILE_VERSION,
        classification="synthetic",
        redaction_reviewed=True,
        source=InterchangeSource(
            producer_alias=_sanitize_alias(config.sandbox_id.lower()),
            exporter_name="autopilot-declarative-sandbox",
            exporter_version=DECLARATIVE_SANDBOX_VERSION,
        ),
        run=InterchangeRun(
            run_id=_sanitize_alias(session.session_id),
            scenario_id=_sanitize_alias(run_bundle.scenario_id),
            agent_alias=_sanitize_alias(config.agent_id.lower()),
            agent_version=_sanitize_alias(config.candidate_version),
            brain_config_alias=_sanitize_alias(config.brain_config_alias),
            started_at=started_at,
            ended_at=session.evaluated_at,
            status="complete" if session.complete else "incomplete",
        ),
        events=interchange_events,
    )


def run_declarative_sandbox_evaluation(
    contract: ContractDocument,
    adapter: AdapterConfig,
    config: DeclarativeAgentSandboxConfig,
    scenario: AgentScenario,
    *,
    provider: ModelProvider | None = None,
    include_interchange: bool = True,
) -> SandboxEvaluationBundle:
    """Execute a declarative sandbox scenario and evaluate behavioural and operational contracts."""

    issues: list[ValidationIssue] = []
    if config.contract_id != contract.contract.id:
        issues.append(ValidationIssue("sandbox.contract_id", "does not match the contract"))
    if config.contract_version != contract.contract.version:
        issues.append(
            ValidationIssue("sandbox.contract_version", "does not match the contract")
        )
    if adapter.adapter.contract_id != contract.contract.id:
        issues.append(ValidationIssue("adapter.contract_id", "does not match the contract"))
    if adapter.adapter.contract_version != contract.contract.version:
        issues.append(
            ValidationIssue("adapter.contract_version", "does not match the contract")
        )
    if issues:
        raise DSLValidationError(issues)

    sandbox = DeclarativeAgentSandbox(config, provider=provider, contract=contract)
    run_bundle = sandbox.run(scenario)
    trajectory: Trajectory = adapt_session(contract, adapter, run_bundle.raw_session)
    evaluation: ContractEvaluation = evaluate_contract(contract, trajectory)
    operational_evaluation: OperationalIntegrityEvaluation = evaluate_operational_integrity(
        contract,
        run_bundle.operational_trace,
    )
    adapter_digest = _canonical_hash(adapter.model_dump(mode="json", exclude_none=True))
    agent_eval = AgentEvaluationBundle(
        agent_run=run_bundle,
        adapter_sha256=adapter_digest,
        trajectory=trajectory,
        evaluation=evaluation,
        operational_evaluation=operational_evaluation,
    )
    interchange = (
        export_run_to_telemetry_interchange(config, run_bundle)
        if include_interchange
        else None
    )
    return SandboxEvaluationBundle(
        sandbox_id=config.sandbox_id,
        sandbox_config_sha256=run_bundle.manifest.config_sha256,
        agent_evaluation=agent_eval,
        telemetry_interchange=interchange,
    )
