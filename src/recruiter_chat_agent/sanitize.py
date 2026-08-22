"""Shared sanitization for text that ultimately traces back to a chat
visitor — recruiter name/email/phone/message. Used both where it's stored
(tools.leave_contact_request) and everywhere it's later displayed
(inquiries.py's terminal viewer, notify.py's push notifications), so the
same untrusted-text handling isn't duplicated — and potentially
inconsistent — in three different places.
"""

from __future__ import annotations

import re

# Strip ASCII C0 (0x00-0x1F, 0x7F) and C1 (0x80-0x9F) control characters —
# keeps plain newlines/tabs, which every consumer here handles fine. Closes
# off terminal escape-sequence injection (inquiries.py's plain print()) and
# the same class of control-character trick in any other renderer (a push
# notification client, a future web view, etc.).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_control_chars(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


def clamp(text: str, max_length: int) -> str:
    """Truncate to at most `max_length` characters, marking that it happened."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def sanitize(text: str, max_length: int | None = None) -> str:
    """Strip control characters and (optionally) clamp length. The one
    function most callers want.
    """
    text = strip_control_chars(text).strip()
    if max_length is not None:
        text = clamp(text, max_length)
    return text
