"""Conservative lexical screening for requests to bypass authorization controls.

This transparent rule is not a semantic classifier, and never establishes that
approval is absent. The authorization state must come from a trusted verifier.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_ACTION = re.compile(
    r"\b(?:release\w*|send\w*|execute\w*|submit\w*|wire\w*|pay\w*|disburse\w*|"
    r"process\w*|issue\w*|proceed\w*|run\w*|push\s+(?:it\s+)?through|move\s+funds|"
    r"transfer\s+(?:the\s+)?(?:funds|money)|carry\s+out|go\s+ahead|liber\w*|envi\w*|"
    r"ejecut\w*|transfi\w*|desembols\w*|proces\w*|paga\w*|mueve\s+(?:los\s+)?fondos)\b",
    re.IGNORECASE,
)
_CONTROL = re.compile(
    r"\b(?:approv\w*|authori[sz]\w*|verif\w*|validat\w*|sign[- ]*off|signature|"
    r"delegation|second\s+(?:approval|check|sign[- ]*off)|review|control\w*|"
    r"aprob\w*|autoriz\w*|verific\w*|validaci\w*|firma\w*|visto\s+bueno|revisi\w*)\b",
    re.IGNORECASE,
)
_BYPASS = re.compile(
    r"\b(?:skip\w*|bypass\w*|waiv\w*|ignor\w*|omit\w*|overrid\w*|circumvent\w*|"
    r"remov\w*|disabl\w*|forgo\w*|salt\w*|omitir|evit\w*|elud\w*)\b",
    re.IGNORECASE,
)
_MISSING = re.compile(
    r"\b(?:without|pending|missing|absent|unavailable|unsigned|incomplete|unapproved|"
    r"not\s+(?:yet\s+)?(?:recorded|attached|signed|approved|verified|complete(?:d)?|cleared)|"
    r"not\s+in\s+the\s+record|still\s+waiting|no\s+signed|sin|pendiente|falta\w*|"
    r"ausente|no\s+consta|no\s+esta|no\s+se\s+ha|todavia\s+no|aun\s+no)\b",
    re.IGNORECASE,
)
_SAFE_CONDITION = re.compile(
    r"\b(?:only\s+after|once|when|after|until|solo\s+cuando|una\s+vez\s+que|"
    r"hasta\s+que)\b.{0,100}\b(?:approv\w*|authori[sz]\w*|verif\w*|"
    r"sign\w*|complete\w*|aprob\w*|autoriz\w*|verific\w*|firm\w*)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:do\s+not|don't|dont|never|must\s+not|should\s+not|will\s+not|won't|wont|"
    r"cannot|can't|cant|mustn't|shouldn't|no\s+se\s+debe|no\s+puede)\s+"
    r"(?:(?:\w+|not)\s+){0,2}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextExceptionSignal:
    """Detector decision and stable rule IDs; deliberately excludes raw user text."""

    triggered: bool
    rule_ids: tuple[str, ...] = ()


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain).strip()


def _is_negated(clause: str, match: re.Match[str]) -> bool:
    prefix = clause[max(0, match.start() - 64) : match.start()]
    return _NEGATION.search(prefix) is not None


def detect_textual_exception_request(text: str) -> TextExceptionSignal:
    """Flag an explicit control-bypass request using conservative lexical rules.

    Triggers require a positive operational action plus either a bypass verb and
    a named control, or missing-control language and a named control. Clear
    instructions to wait for authorization suppress a missing-control trigger.
    This only screens text: authorization status remains a separate trusted input.
    """

    if not isinstance(text, str) or not text.strip():
        return TextExceptionSignal(False)

    clauses = [
        part.strip()
        for part in re.split(r"[.!?\n]+", _normalize(text))
        if part.strip()
    ]
    rule_ids: set[str] = set()
    for clause in clauses:
        actions = [match for match in _ACTION.finditer(clause) if not _is_negated(clause, match)]
        if not actions:
            continue
        controls = list(_CONTROL.finditer(clause))
        bypasses = [match for match in _BYPASS.finditer(clause) if not _is_negated(clause, match)]
        missing = list(_MISSING.finditer(clause))
        safe_condition = _SAFE_CONDITION.search(clause) is not None

        for action in actions:
            def near(match: re.Match[str]) -> bool:
                return abs(match.start() - action.start()) <= 120

            has_control = any(near(control) for control in controls)
            if has_control and any(near(bypass) for bypass in bypasses):
                rule_ids.add("TEXT-BYPASS-CONTROL-ACTION-001")
            if has_control and any(near(cue) for cue in missing) and not safe_condition:
                rule_ids.add("TEXT-MISSING-CONTROL-ACTION-001")

    return TextExceptionSignal(bool(rule_ids), tuple(sorted(rule_ids)))
