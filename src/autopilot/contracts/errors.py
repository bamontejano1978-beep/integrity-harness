"""Validation errors exposed by the contracts DSL parser."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One actionable validation issue with a document path."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class DSLValidationError(ValueError):
    """Raised when a DSL document cannot be parsed or validated."""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))
