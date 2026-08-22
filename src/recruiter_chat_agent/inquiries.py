"""View recruiter contact requests recorded by the agent.

Run with `uv run recruiter-chat-inquiries`.
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from .config import Settings
from .errors import RecruiterChatError
from .logging_setup import configure_logging
from .sanitize import strip_control_chars
from .storage import read_records

logger = logging.getLogger(__name__)


def _clean(text: str) -> str:
    """Strip control characters before printing.

    Storage-time sanitization already does this, but a terminal is the
    actual point of exposure — a value carrying ANSI escape sequences would
    execute in *this* terminal when printed. Cleaning again here is cheap
    defense in depth, and covers any record written before that
    storage-time sanitization existed.
    """
    return strip_control_chars(str(text))


def format_record(index: int, record: dict) -> str:
    """Render one contact request as display lines."""
    lines = [f"[{index}] first contacted {record.get('first_contacted', 'unknown')}"]
    if record.get("name"):
        lines.append(f"    Name:  {_clean(record['name'])}")
    lines.append(f"    Email: {_clean(', '.join(record.get('emails', [])))}")
    if record.get("phones"):
        lines.append(f"    Phone: {_clean(', '.join(record['phones']))}")
    for message in record.get("messages", []):
        timestamp = message.get("timestamp", "")
        lines.append(f"    [{timestamp}] {_clean(message.get('text', ''))}")
    return "\n".join(lines)


def main() -> int:
    """Entry point for `recruiter-chat-inquiries`. Returns an exit code."""
    load_dotenv()
    # Warnings and errors only by default here — this is a report, and
    # routine INFO chatter would clutter it. LOG_LEVEL still overrides.
    raw_level = os.environ.get("LOG_LEVEL", "").strip().upper()
    configure_logging(raw_level or "WARNING")

    try:
        settings = Settings.from_env()
        records = read_records(settings)
    except RecruiterChatError as exc:
        # Never let an unreadable store look like an empty one — that would
        # imply no recruiter has been in touch, which may be badly wrong.
        logger.error("%s", exc)
        return 1

    if not records:
        print("No recruiter contact requests recorded yet.")
        return 0

    print(f"{len(records)} recruiter contact(s):\n")
    for index, record in enumerate(records, start=1):
        print(format_record(index, record))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
