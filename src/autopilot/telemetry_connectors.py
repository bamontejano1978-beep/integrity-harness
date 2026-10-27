"""Thin offline platform connectors translating native exports to the v0.1 profile.

Each connector is a pure local translation: platform-native dict in,
``agent-telemetry-interchange-0.1`` dict out. No network I/O, no credentials,
no interception. Provenance stamped by a connector is a descriptive claim, not
authentication of the emitter.

Supports both hardcoded ``ConnectorSpec`` instances and declarative YAML
connector templates (``DeclarativeConnectorTemplate``) covering the three pilot
visibility tiers: ``conversation_only``, ``conversation_and_tools``, and
``control_events``.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from autopilot.adapter import AdapterConfig, RawSession, adapt_session, load_adapter_config
from autopilot.contracts import load_contract
from autopilot.contracts.errors import DSLValidationError
from autopilot.contracts.loader import load_yaml_mapping
from autopilot.contracts.models import (
    ConditionalClause,
    ContractDocument,
    DebeClause,
    NoDebeClause,
    RecoveryClause,
    TemporalClause,
)
from autopilot.evaluator import evaluate_contract
from autopilot.strategyai_telemetry import (
    MAX_INPUT_BYTES,
    SCHEMA_VERSION as STRATEGYAI_SCHEMA_VERSION,
    TelemetryImportError,
    _json_object_no_duplicates,
    _reject_json_constant,
)
from autopilot.telemetry_interchange import PROFILE_VERSION, parse_interchange
from autopilot.trajectory import ObservationKind

ObservationMethod = Literal["agent_assertion", "runtime_observer", "enforcement_boundary"]
VisibilityTier = Literal["conversation_only", "conversation_and_tools", "control_events"]
CanonicalKind = Literal["message", "tool_call", "tool_result", "control_event"]

_CONNECTOR_ID_PATTERN = "strategyai-reference-v0.1"
_ALIAS_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"
_KEY_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"

_TIER_ALLOWED_KINDS: dict[VisibilityTier, frozenset[CanonicalKind]] = {
    "conversation_only": frozenset({"message"}),
    "conversation_and_tools": frozenset({"message", "tool_call", "tool_result"}),
    "control_events": frozenset({"message", "tool_call", "tool_result", "control_event"}),
}

_TIER_ALLOWED_EVIDENCE: dict[VisibilityTier, frozenset[ObservationMethod]] = {
    "conversation_only": frozenset({"agent_assertion"}),
    "conversation_and_tools": frozenset({"agent_assertion", "runtime_observer"}),
    "control_events": frozenset(
        {"agent_assertion", "runtime_observer", "enforcement_boundary"}
    ),
}

_LEGACY_TOP_KEYS = {"schema_version", "classification", "redaction_reviewed", "run", "events"}
_LEGACY_RUN_KEYS = {
    "run_id",
    "scenario_id",
    "agent_alias",
    "agent_version",
    "brain_config_alias",
    "started_at",
    "ended_at",
    "complete",
}
# Legacy StrategyAI events carry no evidence or trace correlation; the reference
# connector must refuse payloads that already carry those fields so it never
# silently overrides a platform claim.
_RESERVED_EVENT_FIELDS = {"evidence", "observed_at", "trace_id", "span_id"}


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    connector_id: str
    platform_schema: str
    producer_alias: str
    exporter_name: str
    exporter_version: str
    observer_alias: str
    observation_method: ObservationMethod
    description: str


STRATEGYAI_REFERENCE = ConnectorSpec(
    connector_id=_CONNECTOR_ID_PATTERN,
    platform_schema=STRATEGYAI_SCHEMA_VERSION,
    producer_alias="strategyai-sandbox",
    exporter_name="strategyai_reference_connector",
    exporter_version="0.1.0",
    observer_alias="strategyai-export-tap",
    # Legacy export carries no observer information, so the only honest default
    # is the weakest claim. A platform-specific observer may supersede this by
    # registering its own connector with documented coverage.
    observation_method="agent_assertion",
    description=(
        "Reference translation of strategyai-sandbox-export-0.1 synthetic "
        "exports to the interchange profile. Stamps uniform agent_assertion "
        "evidence because the legacy format declares no observer."
    ),
)


class _StrictTemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConnectorTemplateMetadata(_StrictTemplateModel):
    id: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    version: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    visibility_tier: VisibilityTier
    platform_schema: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    producer_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    exporter_name: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    exporter_version: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    description: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]


class TopLevelFieldMap(_StrictTemplateModel):
    schema_version: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "schema_version"
    classification: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "classification"
    redaction_reviewed: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "redaction_reviewed"
    run: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "run"
    events: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "events"


class RunFieldMap(_StrictTemplateModel):
    run_id: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "run_id"
    scenario_id: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "scenario_id"
    agent_alias: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "agent_alias"
    agent_version: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "agent_version"
    brain_config_alias: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "brain_config_alias"
    started_at: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "started_at"
    ended_at: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "ended_at"
    completeness_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "complete"
    completeness_mode: Literal["boolean", "status_enum"] = "boolean"


class CommonEventFieldMap(_StrictTemplateModel):
    event_id: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "event_id"
    sequence: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "sequence"
    occurred_at: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "occurred_at"
    kind: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "kind"
    observed_at: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] | None = None
    trace_id: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] | None = None
    span_id: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] | None = None


class EvidenceStampSpec(_StrictTemplateModel):
    observer_alias: Annotated[StrictStr, Field(pattern=_ALIAS_PATTERN)]
    observation_method: ObservationMethod


class MessageKindMap(_StrictTemplateModel):
    source_kind: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "message"
    role_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "role"
    content_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "content"
    evidence: EvidenceStampSpec


class ToolCallKindMap(_StrictTemplateModel):
    source_kind: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "tool_call"
    call_id_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "call_id"
    tool_name_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "tool_name"
    arguments_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "arguments"
    attribute_aliases: dict[
        Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)],
        Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)],
    ] = Field(default_factory=dict)
    evidence: EvidenceStampSpec


class ToolResultKindMap(_StrictTemplateModel):
    source_kind: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "tool_result"
    call_id_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "call_id"
    outcome_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "outcome"
    result_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "result"
    attribute_aliases: dict[
        Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)],
        Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)],
    ] = Field(default_factory=dict)
    evidence: EvidenceStampSpec


class ControlEventKindMap(_StrictTemplateModel):
    source_kind: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "control_event"
    name_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "name"
    attributes_field: Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)] = "attributes"
    attribute_aliases: dict[
        Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)],
        Annotated[StrictStr, Field(pattern=_KEY_NAME_PATTERN)],
    ] = Field(default_factory=dict)
    evidence: EvidenceStampSpec


class EventMappingSpec(_StrictTemplateModel):
    common: CommonEventFieldMap = Field(default_factory=CommonEventFieldMap)
    message: MessageKindMap
    tool_call: ToolCallKindMap | None = None
    tool_result: ToolResultKindMap | None = None
    control_event: ControlEventKindMap | None = None


class DeclarativeConnectorTemplate(_StrictTemplateModel):
    connector_template_version: Literal["0.1"]
    connector: ConnectorTemplateMetadata
    top_level: TopLevelFieldMap = Field(default_factory=TopLevelFieldMap)
    run: RunFieldMap = Field(default_factory=RunFieldMap)
    events: EventMappingSpec
    unevaluated_properties: Annotated[
        list[Annotated[StrictStr, Field(min_length=1, max_length=200)]],
        Field(min_length=1),
    ]

    def model_post_init(self, __context: Any) -> None:
        tier = self.connector.visibility_tier
        allowed_evidence = _TIER_ALLOWED_EVIDENCE[tier]

        if tier == "conversation_only":
            if (
                self.events.tool_call is not None
                or self.events.tool_result is not None
                or self.events.control_event is not None
            ):
                raise ValueError(
                    "conversation_only template cannot declare tool_call, tool_result, or control_event mappings"
                )
        elif tier == "conversation_and_tools":
            if self.events.tool_call is None or self.events.tool_result is None:
                raise ValueError(
                    "conversation_and_tools template requires both tool_call and tool_result mappings"
                )
            if self.events.control_event is not None:
                raise ValueError(
                    "conversation_and_tools template cannot declare control_event mapping"
                )
        elif tier == "control_events":
            if (
                self.events.tool_call is None
                or self.events.tool_result is None
                or self.events.control_event is None
            ):
                raise ValueError(
                    "control_events template requires message, tool_call, tool_result, and control_event mappings"
                )

        kind_specs = [
            spec
            for spec in (
                self.events.message,
                self.events.tool_call,
                self.events.tool_result,
                self.events.control_event,
            )
            if spec is not None
        ]
        source_kinds = [spec.source_kind for spec in kind_specs]
        if len(source_kinds) != len(set(source_kinds)):
            raise ValueError("source_kind values across event mappings must be unique")

        for spec in kind_specs:
            if spec.evidence.observation_method not in allowed_evidence:
                raise ValueError(
                    f"visibility_tier '{tier}' does not permit observation_method '{spec.evidence.observation_method}'"
                )


_CONNECTORS: dict[str, ConnectorSpec] = {STRATEGYAI_REFERENCE.connector_id: STRATEGYAI_REFERENCE}


def list_connectors() -> list[str]:
    return sorted(_CONNECTORS)


def get_connector(connector_id: str) -> ConnectorSpec:
    try:
        return _CONNECTORS[connector_id]
    except KeyError:
        raise TelemetryImportError(f"connector: unknown connector '{connector_id}'") from None


def parse_connector_template(text: str, *, source: str = "<memory>") -> DeclarativeConnectorTemplate:
    """Parse and validate a declarative connector template from YAML text."""
    if not text.strip():
        raise TelemetryImportError(f"template: {source} is empty")
    try:
        raw = load_yaml_mapping(text)
    except DSLValidationError as exc:
        raise TelemetryImportError(f"template: invalid YAML mapping in {source}: {exc}") from None
    try:
        return DeclarativeConnectorTemplate.model_validate(raw)
    except ValidationError:
        raise TelemetryImportError(
            "template: declarative connector schema or tier validation failed"
        ) from None


def load_connector_template(path: str | Path) -> DeclarativeConnectorTemplate:
    """Load and validate a UTF-8 YAML declarative connector template from disk."""
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        raise TelemetryImportError(f"template: cannot read file {resolved}") from None
    return parse_connector_template(text, source=str(resolved))


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TelemetryImportError(f"{path}: must be an object")
    return value


def _translate_attributes(
    raw_attrs: Any,
    aliases: dict[str, str],
    path: str,
) -> dict[str, Any]:
    mapping = _require_dict(raw_attrs, path)
    if not aliases:
        return copy.deepcopy(mapping)
    translated: dict[str, Any] = {}
    for key, val in mapping.items():
        target = aliases.get(key, key)
        if target in translated:
            raise TelemetryImportError(f"{path}: attribute alias collision on '{target}'")
        translated[target] = copy.deepcopy(val)
    return translated


def translate_via_template(
    platform_payload: Any,
    template: DeclarativeConnectorTemplate,
) -> dict[str, Any]:
    """Translate a platform export dict to a profile v0.1 document using a declarative template."""
    root = _require_dict(platform_payload, "platform_export")
    tl = template.top_level
    expected_top = {
        tl.schema_version,
        tl.classification,
        tl.redaction_reviewed,
        tl.run,
        tl.events,
    }
    extra_top = set(root) - expected_top
    if extra_top:
        raise TelemetryImportError(
            "connector: unsupported platform field(s): " + ", ".join(sorted(extra_top))
        )
    missing_top = expected_top - set(root)
    if missing_top:
        raise TelemetryImportError(
            "connector: missing platform field(s): " + ", ".join(sorted(missing_top))
        )
    if root[tl.schema_version] != template.connector.platform_schema:
        raise TelemetryImportError("connector: unsupported platform schema version")
    if root[tl.classification] != "synthetic":
        raise TelemetryImportError("connector: only synthetic platform exports are accepted")
    if root[tl.redaction_reviewed] is not True:
        raise TelemetryImportError("connector: redaction_reviewed must be explicitly true")

    rm = template.run
    run = _require_dict(root[tl.run], "platform_export.run")
    expected_run = {
        rm.run_id,
        rm.scenario_id,
        rm.agent_alias,
        rm.agent_version,
        rm.brain_config_alias,
        rm.started_at,
        rm.ended_at,
        rm.completeness_field,
    }
    missing_run = expected_run - set(run)
    if missing_run:
        raise TelemetryImportError(
            "connector: missing run field(s): " + ", ".join(sorted(missing_run))
        )
    extra_run = set(run) - expected_run
    if extra_run:
        raise TelemetryImportError(
            "connector: unsupported run field(s): " + ", ".join(sorted(extra_run))
        )

    raw_completeness = run[rm.completeness_field]
    if rm.completeness_mode == "boolean":
        if type(raw_completeness) is not bool:
            raise TelemetryImportError(f"connector: run.{rm.completeness_field} must be a boolean")
        run_status = "complete" if raw_completeness is True else "incomplete"
    else:
        if raw_completeness not in {"complete", "incomplete", "unknown"}:
            raise TelemetryImportError(
                f"connector: run.{rm.completeness_field} must be complete, incomplete, or unknown"
            )
        run_status = raw_completeness

    events = root[tl.events]
    if not isinstance(events, list) or not events:
        raise TelemetryImportError("connector: events must be a non-empty array")

    cm = template.events.common
    kind_dispatch: dict[str, tuple[CanonicalKind, Any]] = {
        template.events.message.source_kind: ("message", template.events.message),
    }
    if template.events.tool_call is not None:
        kind_dispatch[template.events.tool_call.source_kind] = (
            "tool_call",
            template.events.tool_call,
        )
    if template.events.tool_result is not None:
        kind_dispatch[template.events.tool_result.source_kind] = (
            "tool_result",
            template.events.tool_result,
        )
    if template.events.control_event is not None:
        kind_dispatch[template.events.control_event.source_kind] = (
            "control_event",
            template.events.control_event,
        )

    optional_common_sources = {
        src: target
        for target, src in (
            ("observed_at", cm.observed_at),
            ("trace_id", cm.trace_id),
            ("span_id", cm.span_id),
        )
        if src is not None
    }

    translated_events: list[dict[str, Any]] = []
    for index, raw_event in enumerate(events):
        path = f"platform_export.events[{index}]"
        event = _require_dict(raw_event, path)
        if "evidence" in event:
            raise TelemetryImportError(
                "connector: platform event already carries profile field(s): evidence"
            )
        unmapped_reserved = (_RESERVED_EVENT_FIELDS - set(optional_common_sources)) & set(event)
        if unmapped_reserved:
            raise TelemetryImportError(
                "connector: platform event already carries profile field(s): "
                + ", ".join(sorted(unmapped_reserved))
            )

        raw_kind = event.get(cm.kind)
        if not isinstance(raw_kind, str) or raw_kind not in kind_dispatch:
            raise TelemetryImportError(
                f"connector: event kind not permitted by visibility tier '{template.connector.visibility_tier}'"
            )
        canonical_kind, kind_spec = kind_dispatch[raw_kind]

        required_common = {cm.event_id, cm.sequence, cm.occurred_at, cm.kind}
        if canonical_kind == "message":
            required_specific = {kind_spec.role_field, kind_spec.content_field}
        elif canonical_kind == "tool_call":
            required_specific = {
                kind_spec.call_id_field,
                kind_spec.tool_name_field,
                kind_spec.arguments_field,
            }
        elif canonical_kind == "tool_result":
            required_specific = {
                kind_spec.call_id_field,
                kind_spec.outcome_field,
                kind_spec.result_field,
            }
        else:
            required_specific = {kind_spec.name_field, kind_spec.attributes_field}

        required_keys = required_common | required_specific
        allowed_keys = required_keys | set(optional_common_sources)
        missing_ev = required_keys - set(event)
        if missing_ev:
            raise TelemetryImportError(
                f"{path}: missing required field(s): " + ", ".join(sorted(missing_ev))
            )
        extra_ev = set(event) - allowed_keys
        if extra_ev:
            raise TelemetryImportError(
                f"{path}: unsupported field(s): " + ", ".join(sorted(extra_ev))
            )

        out_event: dict[str, Any] = {
            "event_id": event[cm.event_id],
            "sequence": event[cm.sequence],
            "occurred_at": event[cm.occurred_at],
            "kind": canonical_kind,
            "evidence": {
                "observer_alias": kind_spec.evidence.observer_alias,
                "observation_method": kind_spec.evidence.observation_method,
            },
        }
        for src_key, target_key in optional_common_sources.items():
            if src_key in event:
                out_event[target_key] = event[src_key]

        if canonical_kind == "message":
            out_event["role"] = event[kind_spec.role_field]
            out_event["content"] = event[kind_spec.content_field]
        elif canonical_kind == "tool_call":
            out_event["call_id"] = event[kind_spec.call_id_field]
            out_event["tool_name"] = event[kind_spec.tool_name_field]
            out_event["arguments"] = _translate_attributes(
                event[kind_spec.arguments_field],
                kind_spec.attribute_aliases,
                f"{path}.{kind_spec.arguments_field}",
            )
        elif canonical_kind == "tool_result":
            out_event["call_id"] = event[kind_spec.call_id_field]
            out_event["outcome"] = event[kind_spec.outcome_field]
            out_event["result"] = _translate_attributes(
                event[kind_spec.result_field],
                kind_spec.attribute_aliases,
                f"{path}.{kind_spec.result_field}",
            )
        else:
            out_event["name"] = event[kind_spec.name_field]
            out_event["attributes"] = _translate_attributes(
                event[kind_spec.attributes_field],
                kind_spec.attribute_aliases,
                f"{path}.{kind_spec.attributes_field}",
            )

        translated_events.append(out_event)

    translated_run = {
        "agent_alias": run[rm.agent_alias],
        "agent_version": run[rm.agent_version],
        "brain_config_alias": run[rm.brain_config_alias],
        "ended_at": run[rm.ended_at],
        "run_id": run[rm.run_id],
        "scenario_id": run[rm.scenario_id],
        "started_at": run[rm.started_at],
        "status": run_status,
    }

    return {
        "schema_version": PROFILE_VERSION,
        "classification": "synthetic",
        "redaction_reviewed": True,
        "source": {
            "producer_alias": template.connector.producer_alias,
            "exporter_name": template.connector.exporter_name,
            "exporter_version": template.connector.exporter_version,
        },
        "run": translated_run,
        "events": translated_events,
    }


def import_via_template(
    template: DeclarativeConnectorTemplate | str | Path,
    platform_payload: Any,
    *,
    contract_id: str,
    contract_version: str,
    candidate_version: str,
) -> tuple[RawSession, dict[str, Any]]:
    """Translate a platform export using a declarative template and validate via the v0.1 profile."""
    resolved_template = (
        template
        if isinstance(template, DeclarativeConnectorTemplate)
        else load_connector_template(template)
    )
    document = translate_via_template(platform_payload, resolved_template)
    session, report = parse_interchange(
        document,
        contract_id=contract_id,
        contract_version=contract_version,
        candidate_version=candidate_version,
    )
    report["connector_id"] = resolved_template.connector.id
    report["connector_version"] = resolved_template.connector.version
    report["visibility_tier"] = resolved_template.connector.visibility_tier
    report["unevaluated_properties"] = list(resolved_template.unevaluated_properties)
    return session, report


def translate_to_interchange(platform_payload: Any, spec: ConnectorSpec) -> dict[str, Any]:
    """Translate one platform-native export dict to a profile v0.1 document."""
    root = _require_dict(platform_payload, "platform_export")
    extra_top = set(root) - _LEGACY_TOP_KEYS
    if extra_top:
        raise TelemetryImportError(
            "connector: unsupported platform field(s): " + ", ".join(sorted(extra_top))
        )
    missing_top = _LEGACY_TOP_KEYS - set(root)
    if missing_top:
        raise TelemetryImportError(
            "connector: missing platform field(s): " + ", ".join(sorted(missing_top))
        )
    if root["schema_version"] != spec.platform_schema:
        raise TelemetryImportError("connector: unsupported platform schema version")
    if root["classification"] != "synthetic":
        raise TelemetryImportError("connector: only synthetic platform exports are accepted")
    if root["redaction_reviewed"] is not True:
        raise TelemetryImportError("connector: redaction_reviewed must be explicitly true")

    run = _require_dict(root["run"], "platform_export.run")
    missing_run = _LEGACY_RUN_KEYS - set(run)
    if missing_run:
        raise TelemetryImportError(
            "connector: missing run field(s): " + ", ".join(sorted(missing_run))
        )
    extra_run = set(run) - _LEGACY_RUN_KEYS
    if extra_run:
        raise TelemetryImportError(
            "connector: unsupported run field(s): " + ", ".join(sorted(extra_run))
        )
    if type(run["complete"]) is not bool:
        raise TelemetryImportError("connector: run.complete must be a boolean")

    events = root["events"]
    if not isinstance(events, list) or not events:
        raise TelemetryImportError("connector: events must be a non-empty array")

    translated_events: list[dict[str, Any]] = []
    for index, raw_event in enumerate(events):
        event = _require_dict(raw_event, f"platform_export.events[{index}]")
        reserved = _RESERVED_EVENT_FIELDS & set(event)
        if reserved:
            raise TelemetryImportError(
                "connector: platform event already carries profile field(s): "
                + ", ".join(sorted(reserved))
            )
        copied = copy.deepcopy(event)
        copied["evidence"] = {
            "observer_alias": spec.observer_alias,
            "observation_method": spec.observation_method,
        }
        translated_events.append(copied)

    translated_run = {key: run[key] for key in sorted(_LEGACY_RUN_KEYS)}
    translated_run["status"] = "complete" if run["complete"] is True else "incomplete"
    del translated_run["complete"]

    return {
        "schema_version": PROFILE_VERSION,
        "classification": "synthetic",
        "redaction_reviewed": True,
        "source": {
            "producer_alias": spec.producer_alias,
            "exporter_name": spec.exporter_name,
            "exporter_version": spec.exporter_version,
        },
        "run": translated_run,
        "events": translated_events,
    }


def import_via_connector(
    connector_id: str,
    platform_payload: Any,
    *,
    contract_id: str,
    contract_version: str,
    candidate_version: str,
) -> tuple[RawSession, dict[str, Any]]:
    """Translate a platform export and validate it as a profile document."""
    spec = get_connector(connector_id)
    document = translate_to_interchange(platform_payload, spec)
    session, report = parse_interchange(
        document,
        contract_id=contract_id,
        contract_version=contract_version,
        candidate_version=candidate_version,
    )
    report["connector_id"] = spec.connector_id
    return session, report


def _load_local_json_export(input_path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = input_path.read_bytes()
    except OSError:
        raise TelemetryImportError("input: cannot read local file") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise TelemetryImportError(f"input: exceeds the {MAX_INPUT_BYTES}-byte limit")
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_json_object_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except TelemetryImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise TelemetryImportError(
            "input: invalid UTF-8 JSON or excessively nested structure"
        ) from None
    return _require_dict(payload, "input"), hashlib.sha256(raw).hexdigest()


def _tier_allows_adapter_source(tier: VisibilityTier, source: str) -> bool:
    if source == "message":
        return True
    if source == "tool_call":
        return tier in {"conversation_and_tools", "control_events"}
    if source == "event":
        return tier == "control_events"
    return False


def _analyze_contract_signals(
    contract: ContractDocument,
    config: AdapterConfig,
    session: RawSession,
    tier: VisibilityTier,
) -> dict[str, Any]:
    trajectory = adapt_session(contract, config, session)
    evaluation = evaluate_contract(contract, trajectory)

    rules_by_output: dict[tuple[str, str], list[Any]] = {}
    for rule in config.rules:
        key = (rule.output_kind.value, rule.output_name)
        rules_by_output.setdefault(key, []).append(rule)

    obs_by_kind_name: dict[tuple[str, str], int] = {}
    for obs in trajectory.observations:
        key = (obs.kind.value, obs.name)
        obs_by_kind_name[key] = obs_by_kind_name.get(key, 0) + 1

    vocabulary_signals: list[dict[str, Any]] = []
    for kind_label, names in (
        (ObservationKind.event.value, contract.vocabulary.events),
        (ObservationKind.action.value, contract.vocabulary.actions),
    ):
        for name in names:
            matching_rules = rules_by_output.get((kind_label, name), [])
            reachable_rules = [
                r.id
                for r in matching_rules
                if _tier_allows_adapter_source(tier, r.source.value)
            ]
            blocked_rules = [
                r.id
                for r in matching_rules
                if not _tier_allows_adapter_source(tier, r.source.value)
            ]
            observed_count = obs_by_kind_name.get((kind_label, name), 0)
            if not matching_rules:
                reachability = "unmapped_in_adapter"
            elif reachable_rules:
                reachability = "observable_in_tier"
            else:
                reachability = "blocked_by_visibility_tier"
            vocabulary_signals.append(
                {
                    "kind": kind_label,
                    "name": name,
                    "reachability": reachability,
                    "observed_count": observed_count,
                    "reachable_rule_ids": reachable_rules,
                    "tier_blocked_rule_ids": blocked_rules,
                }
            )

    signal_lookup = {
        (item["kind"], item["name"]): item for item in vocabulary_signals
    }
    eval_by_clause = {res.clause_id: res for res in evaluation.clause_results}

    clause_reports: list[dict[str, Any]] = []
    evaluable_clause_ids: list[str] = []
    indeterminate_clause_ids: list[str] = []
    tier_blind_clause_ids: list[str] = []

    for clause in contract.clauses:
        res = eval_by_clause[clause.id]
        status_val = res.status.value
        required_signals: list[tuple[str, str]] = []
        if isinstance(clause, DebeClause):
            required_signals = [
                ("event", clause.trigger.event),
                ("action", clause.requirement.action),
            ]
        elif isinstance(clause, ConditionalClause):
            required_signals = [
                ("event", clause.if_.event),
                ("action", clause.then.action),
            ]
        elif isinstance(clause, TemporalClause):
            required_signals = [
                ("event", clause.trigger.event),
                ("action", clause.requirement.action),
            ]
        elif isinstance(clause, NoDebeClause):
            required_signals = [("action", clause.prohibition.action)]
        elif isinstance(clause, RecoveryClause):
            required_signals = []

        blocked_signals = [
            f"{k}:{n}"
            for k, n in required_signals
            if signal_lookup.get((k, n), {}).get("reachability") != "observable_in_tier"
        ]

        if status_val == "indeterminate":
            evaluability = "indeterminate"
            indeterminate_clause_ids.append(clause.id)
        elif blocked_signals:
            evaluability = "unevaluable_in_tier"
            tier_blind_clause_ids.append(clause.id)
        elif status_val in {"pass", "fail"}:
            evaluability = "evaluable"
            evaluable_clause_ids.append(clause.id)
        else:
            evaluability = "not_triggered_in_sample"

        clause_reports.append(
            {
                "clause_id": clause.id,
                "clause_type": clause.type,
                "sample_status": status_val,
                "evaluability": evaluability,
                "blocked_signals": blocked_signals,
                "reason": res.reason,
            }
        )

    return {
        "contract_id": contract.contract.id,
        "contract_version": contract.contract.version,
        "adapter_id": config.adapter.id,
        "adapter_version": config.adapter.version,
        "trajectory_complete": trajectory.complete,
        "overall_evaluation_status": evaluation.status.value,
        "recommended_autonomy": evaluation.recommended_autonomy.value,
        "block_release": evaluation.block_release,
        "observations_emitted": [obs.name for obs in trajectory.observations],
        "vocabulary_signals": vocabulary_signals,
        "clauses": clause_reports,
        "summary": {
            "evaluable_clauses": evaluable_clause_ids,
            "indeterminate_clauses": indeterminate_clause_ids,
            "unevaluable_in_tier_clauses": tier_blind_clause_ids,
        },
    }


def dry_run_telemetry_sample(
    input_path: Path,
    *,
    template_path: Path | None = None,
    connector_id: str | None = None,
    contract_path: Path | None = None,
    adapter_path: Path | None = None,
    candidate_version: str = "0.1.0",
) -> dict[str, Any]:
    """Validate a local synthetic export in memory and report signal/clause evaluability."""
    if (template_path is None) == (connector_id is None):
        raise TelemetryImportError(
            "dry_run: specify exactly one of --template or --connector"
        )
    if (contract_path is None) != (adapter_path is None):
        raise TelemetryImportError(
            "dry_run: --contract and --adapter must be provided together"
        )

    payload, input_sha256 = _load_local_json_export(input_path)

    contract: ContractDocument | None = None
    adapter_cfg: AdapterConfig | None = None
    if contract_path is not None and adapter_path is not None:
        try:
            contract = load_contract(contract_path)
            adapter_cfg = load_adapter_config(adapter_path)
        except DSLValidationError as exc:
            raise TelemetryImportError(f"dry_run: invalid contract or adapter: {exc}") from None
        contract_id = contract.contract.id
        contract_version = contract.contract.version
    else:
        contract_id = "DRY-RUN-CONTRACT"
        contract_version = "0.1.0"

    if template_path is not None:
        template = load_connector_template(template_path)
        session, import_report = import_via_template(
            template,
            payload,
            contract_id=contract_id,
            contract_version=contract_version,
            candidate_version=candidate_version,
        )
        tier: VisibilityTier = template.connector.visibility_tier
        unevaluated_props = list(template.unevaluated_properties)
    else:
        assert connector_id is not None
        session, import_report = import_via_connector(
            connector_id,
            payload,
            contract_id=contract_id,
            contract_version=contract_version,
            candidate_version=candidate_version,
        )
        tier = "control_events"
        unevaluated_props = [
            "cryptographic_emitter_authenticity",
            "unobserved_out_of_band_side_effects",
        ]

    result: dict[str, Any] = {
        "valid": True,
        "dry_run": True,
        "input_path": str(input_path),
        "input_sha256": input_sha256,
        "connector_id": import_report["connector_id"],
        "visibility_tier": tier,
        "run_id": import_report["run_id"],
        "scenario_id": import_report["scenario_id"],
        "run_status_claimed": import_report["run_status_claimed"],
        "run_complete_effective": session.complete,
        "event_count": import_report["event_count"],
        "events_by_kind": import_report["events_by_kind"],
        "observation_methods": import_report["observation_methods"],
        "authenticity": import_report["authenticity"],
        "unevaluated_properties": unevaluated_props,
    }

    if contract is not None and adapter_cfg is not None:
        try:
            result["contract_analysis"] = _analyze_contract_signals(
                contract, adapter_cfg, session, tier
            )
        except DSLValidationError as exc:
            raise TelemetryImportError(
                f"dry_run: contract/adapter mismatch during adaptation: {exc}"
            ) from None

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run validate a synthetic StrategyAI telemetry sample and report signal evaluability."
    )
    parser.add_argument("input", type=Path, help="local synthetic export JSON")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--template", type=Path, help="declarative connector template YAML")
    group.add_argument("--connector", help="registered connector ID (e.g. strategyai-reference-v0.1)")
    parser.add_argument("--contract", type=Path, help="optional contract YAML to check clause evaluability")
    parser.add_argument("--adapter", type=Path, help="optional adapter YAML paired with --contract")
    parser.add_argument("--candidate-version", default="0.1.0", help="candidate SemVer alias (default: 0.1.0)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        report = dry_run_telemetry_sample(
            args.input,
            template_path=args.template,
            connector_id=args.connector,
            contract_path=args.contract,
            adapter_path=args.adapter,
            candidate_version=args.candidate_version,
        )
    except TelemetryImportError as exc:
        if args.as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "dry_run": True,
                        "input_path": str(args.input),
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"TELEMETRY DRY-RUN REJECTED: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(
        f"TELEMETRY DRY-RUN VALID: {report['run_id']} "
        f"(connector={report['connector_id']}, tier={report['visibility_tier']}, "
        f"complete={report['run_complete_effective']})"
    )
    print(f"  Events: {report['event_count']} {report['events_by_kind']}")
    print(f"  Observation methods: {report['observation_methods']}")
    print(f"  Unevaluated properties: {', '.join(report['unevaluated_properties'])}")
    if "contract_analysis" in report:
        ca = report["contract_analysis"]
        print(
            f"  Contract {ca['contract_id']} v{ca['contract_version']}: "
            f"status={ca['overall_evaluation_status']}, "
            f"autonomy={ca['recommended_autonomy']}"
        )
        print(f"    Evaluable clauses: {ca['summary']['evaluable_clauses']}")
        print(f"    Indeterminate clauses: {ca['summary']['indeterminate_clauses']}")
        print(
            f"    Unevaluable in tier: {ca['summary']['unevaluable_in_tier_clauses']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
