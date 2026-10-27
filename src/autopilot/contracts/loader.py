"""Safe YAML loading and validation for Behavioural Contracts DSL v0.1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.tokens import AliasToken, AnchorToken, TagToken

from autopilot.contracts.errors import DSLValidationError, ValidationIssue
from autopilot.contracts.models import ContractDocument
from autopilot.contracts.validation import validate_semantics


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _location(mark: Any) -> str:
    if mark is None:
        return "document"
    return f"line {mark.line + 1}, column {mark.column + 1}"


def _format_path(location: tuple[Any, ...]) -> str:
    path = ""
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        elif path:
            path += f".{part}"
        else:
            path = str(part)
    return path or "document"


def pydantic_issues(error: ValidationError) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for detail in error.errors(include_url=False):
        message = detail["msg"]
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        issues.append(ValidationIssue(_format_path(detail["loc"]), message))
    return issues


def load_yaml_mapping(text: str) -> Any:
    try:
        for token in yaml.scan(text):
            if isinstance(token, AnchorToken):
                raise DSLValidationError(
                    [ValidationIssue(_location(token.start_mark), "YAML anchors are not allowed")]
                )
            if isinstance(token, AliasToken):
                raise DSLValidationError(
                    [ValidationIssue(_location(token.start_mark), "YAML aliases are not allowed")]
                )
            if isinstance(token, TagToken):
                raise DSLValidationError(
                    [ValidationIssue(_location(token.start_mark), "custom YAML tags are not allowed")]
                )

        documents = list(yaml.load_all(text, Loader=_UniqueKeySafeLoader))
    except DSLValidationError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        message = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise DSLValidationError([ValidationIssue(_location(mark), message)]) from exc

    if len(documents) != 1:
        raise DSLValidationError(
            [ValidationIssue("document", "expected exactly one YAML document")]
        )
    if not isinstance(documents[0], dict):
        raise DSLValidationError(
            [ValidationIssue("document", "top-level YAML value must be a mapping")]
        )
    return documents[0]


def parse_contract(text: str, *, source: str = "<memory>") -> ContractDocument:
    """Parse and validate a contract from YAML text."""

    if not text.strip():
        raise DSLValidationError([ValidationIssue("document", f"{source} is empty")])

    raw = load_yaml_mapping(text)
    try:
        document = ContractDocument.model_validate(raw)
    except ValidationError as exc:
        raise DSLValidationError(pydantic_issues(exc)) from exc

    issues = validate_semantics(document)
    if issues:
        raise DSLValidationError(issues)
    return document


def load_contract(path: str | Path) -> ContractDocument:
    """Load and validate a UTF-8 YAML contract from disk."""

    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"not found: {resolved}")]
        ) from exc
    except UnicodeDecodeError as exc:
        raise DSLValidationError(
            [ValidationIssue("file", f"is not valid UTF-8: {resolved}")]
        ) from exc
    return parse_contract(text, source=str(resolved))
