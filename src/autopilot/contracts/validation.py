"""Cross-field semantic validation for Behavioural Contracts DSL v0.1."""

from __future__ import annotations

from typing import Any, Iterable

from autopilot.contracts.errors import ValidationIssue
from autopilot.contracts.models import (
    AttributeType,
    AutonomyLevel,
    ConditionalClause,
    ContractDocument,
    DebeClause,
    NoDebeClause,
    RecoveryClause,
    TemporalClause,
    value_matches_attribute_type,
)

_AUTONOMY_ORDER = {
    AutonomyLevel.COACH: 0,
    AutonomyLevel.SUPERVISE: 1,
    AutonomyLevel.RESTRICT: 2,
    AutonomyLevel.QUARANTINE: 3,
}


def _duplicate_values(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _validate_mapping(
    mapping: dict[str, Any],
    *,
    path: str,
    attributes: dict[str, AttributeType],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for key, value in mapping.items():
        key_path = f"{path}.{key}"
        if key not in attributes:
            issues.append(ValidationIssue(key_path, "attribute is not declared in vocabulary"))
        elif not value_matches_attribute_type(value, attributes[key]):
            issues.append(
                ValidationIssue(
                    key_path,
                    f"expected {attributes[key].value}, got {type(value).__name__}",
                )
            )
    return issues


def validate_semantics(document: ContractDocument) -> list[ValidationIssue]:
    """Return all independently detectable cross-field validation issues."""

    issues: list[ValidationIssue] = []
    vocabulary = document.vocabulary
    event_names = set(vocabulary.events)
    action_names = set(vocabulary.actions)
    attributes = vocabulary.attributes

    if document.contract.status.value == "active" and document.contract.effective_from is None:
        issues.append(
            ValidationIssue(
                "contract.effective_from",
                "is required when contract.status is active",
            )
        )

    for field_name, values in (
        ("vocabulary.events", vocabulary.events),
        ("vocabulary.actions", vocabulary.actions),
        ("contract.tags", document.contract.tags),
    ):
        for duplicate in _duplicate_values(values):
            issues.append(ValidationIssue(field_name, f"duplicate value: {duplicate}"))

    clause_ids = [clause.id for clause in document.clauses]
    for duplicate in _duplicate_values(clause_ids):
        issues.append(ValidationIssue("clauses", f"duplicate clause id: {duplicate}"))

    clause_by_id = {clause.id: clause for clause in document.clauses}

    for index, clause in enumerate(document.clauses):
        base = f"clauses[{index}]"

        for duplicate in _duplicate_values(clause.tags):
            issues.append(ValidationIssue(f"{base}.tags", f"duplicate value: {duplicate}"))

        if clause.on_failure is not None:
            for duplicate in _duplicate_values(clause.on_failure.notify):
                issues.append(
                    ValidationIssue(f"{base}.on_failure.notify", f"duplicate value: {duplicate}")
                )

        if isinstance(clause, DebeClause):
            if clause.trigger.event not in event_names:
                issues.append(
                    ValidationIssue(f"{base}.trigger.event", "event is not declared in vocabulary")
                )
            if clause.requirement.action not in action_names:
                issues.append(
                    ValidationIssue(
                        f"{base}.requirement.action",
                        "action is not declared in vocabulary",
                    )
                )
            issues.extend(
                _validate_mapping(
                    clause.trigger.conditions,
                    path=f"{base}.trigger.conditions",
                    attributes=attributes,
                )
            )
            issues.extend(
                _validate_mapping(
                    clause.requirement.parameters,
                    path=f"{base}.requirement.parameters",
                    attributes=attributes,
                )
            )

        elif isinstance(clause, NoDebeClause):
            if clause.prohibition.action not in action_names:
                issues.append(
                    ValidationIssue(
                        f"{base}.prohibition.action",
                        "action is not declared in vocabulary",
                    )
                )
            issues.extend(
                _validate_mapping(
                    clause.prohibition.conditions,
                    path=f"{base}.prohibition.conditions",
                    attributes=attributes,
                )
            )

        elif isinstance(clause, ConditionalClause):
            if clause.if_.event not in event_names:
                issues.append(
                    ValidationIssue(f"{base}.if.event", "event is not declared in vocabulary")
                )
            if clause.then.action not in action_names:
                issues.append(
                    ValidationIssue(f"{base}.then.action", "action is not declared in vocabulary")
                )
            issues.extend(
                _validate_mapping(
                    clause.if_.conditions,
                    path=f"{base}.if.conditions",
                    attributes=attributes,
                )
            )
            issues.extend(
                _validate_mapping(
                    clause.then.parameters,
                    path=f"{base}.then.parameters",
                    attributes=attributes,
                )
            )

        elif isinstance(clause, TemporalClause):
            if clause.trigger.event not in event_names:
                issues.append(
                    ValidationIssue(f"{base}.trigger.event", "event is not declared in vocabulary")
                )
            if clause.requirement.action not in action_names:
                issues.append(
                    ValidationIssue(
                        f"{base}.requirement.action",
                        "action is not declared in vocabulary",
                    )
                )
            issues.extend(
                _validate_mapping(
                    clause.trigger.conditions,
                    path=f"{base}.trigger.conditions",
                    attributes=attributes,
                )
            )
            issues.extend(
                _validate_mapping(
                    clause.requirement.parameters,
                    path=f"{base}.requirement.parameters",
                    attributes=attributes,
                )
            )

        elif isinstance(clause, RecoveryClause):
            for reference_index, reference in enumerate(
                clause.activation.clause_failure_any_of
            ):
                ref_path = f"{base}.activation.clause_failure_any_of[{reference_index}]"
                target = clause_by_id.get(reference)
                if target is None:
                    issues.append(ValidationIssue(ref_path, "referenced clause does not exist"))
                elif reference == clause.id:
                    issues.append(ValidationIssue(ref_path, "recovery clause cannot reference itself"))
                elif isinstance(target, RecoveryClause):
                    issues.append(
                        ValidationIssue(ref_path, "recovery clause cannot reference another recovery clause")
                    )

            if (
                _AUTONOMY_ORDER[clause.autonomy.restore_to]
                >= _AUTONOMY_ORDER[clause.autonomy.on_activation]
            ):
                issues.append(
                    ValidationIssue(
                        f"{base}.autonomy.restore_to",
                        "must be more permissive than autonomy.on_activation",
                    )
                )

    return issues
