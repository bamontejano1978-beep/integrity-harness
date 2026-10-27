"""Parser for consumer WhatsApp chat exports (the evidence format of the battery).

The battery runs on consumer WhatsApp: the operator exports each session with
``⋮ → Más → Exportar chat`` and the harness ingests the ``.txt``. Export lines
look like::

    25/9/26, 22:16 - <speaker>: <message text>
    <continuation lines of a multi-line message>

The speaker of Lora's side is a long business signature; the red team side is a
short alias. The rule ``"Lora" in speaker`` discriminates the sides and is
frozen here so every consumer of the ledger shares it. Timestamps are naive
local datetimes — exports carry no zone info, and the battery never compares
across zones.

Session alignment: an export always contains the whole chat, so a battery
session is *cut* out of it. ``trim_after_last_message_at`` keeps every message
up to and including the anchor timestamp and silently drops anything after it
(the exports re-list older traffic verbatim — any divergence there would be
corruption to investigate, not merge).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_MESSAGE_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4}), (\d{1,2}):(\d{2}) - ([^:]+): (.*)$"
)


@dataclass(frozen=True)
class ChatMessage:
    """One message of an export, sides already normalised."""

    timestamp: datetime
    speaker: str
    text: str
    side: str  # "lora" | "operator"
    is_continuation: bool = False


def _side_of(speaker: str) -> str:
    return "lora" if "Lora" in speaker else "operator"


def parse_export(text: str) -> list[ChatMessage]:
    """Parse the full export into messages, in file order."""
    messages: list[ChatMessage] = []
    current: ChatMessage | None = None
    pending: list[str] = []

    def flush_pending() -> None:
        nonlocal current
        if current is not None and pending:
            current = ChatMessage(
                timestamp=current.timestamp,
                speaker=current.speaker,
                text=current.text + "\n" + "\n".join(pending).strip(),
                side=current.side,
            )
            pending.clear()
        if current is not None:
            messages.append(current)
            current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        match = _MESSAGE_RE.match(line)
        if match:
            flush_pending()
            day, month, year, hour, minute = (int(part) for part in match.groups()[:5])
            speaker, body = match.group(6), match.group(7)
            year = year if year >= 100 else 2000 + year
            current = ChatMessage(
                timestamp=datetime(year, month, day, hour, minute),
                speaker=speaker,
                text=body,
                side=_side_of(speaker),
            )
        elif current is not None:
            pending.append(line)
        # Lines before the first header (export notices, media placeholders
        # without a speaker) are not messages and are dropped.
    flush_pending()
    return messages


def parse_export_file(path: Path | str) -> list[ChatMessage]:
    return parse_export(Path(path).read_text(encoding="utf-8"))


def trim_after_last_message_at(
    messages: list[ChatMessage], anchor: datetime
) -> list[ChatMessage]:
    """Keep every message up to and including ``anchor``; drop what follows.

    Cutting point of a battery session: the frozen session deck's last message
    is the anchor, and anything the export shows beyond it belongs to a later
    session.
    """
    kept: list[ChatMessage] = []
    for message in messages:
        if message.timestamp <= anchor:
            kept.append(message)
    return kept


def pick_session_part(
    export_text: str, anchor: datetime
) -> list[ChatMessage]:
    """Parse an export and cut the part of the session that ends at ``anchor``."""
    return trim_after_last_message_at(parse_export(export_text), anchor)
