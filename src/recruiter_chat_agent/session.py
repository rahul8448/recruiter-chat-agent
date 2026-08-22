"""Tracks "who is asking" across one conversation, so tools.py can scope
writes to the session that made them.

Why this exists: leave_contact_request originally matched an existing
contact record by email/phone overlap alone, with no proof the caller
actually owns that email/phone. Anyone who knew (or guessed) a real
recruiter's address could submit a call that overwrote their stored phone
number or appended a fabricated "corrected" message onto their record — a
record-tampering / impersonation vector. Scoping matches to the current
session closes that: a different visitor's session can never touch a
record created in someone else's session, even with an identical email.

Uses a contextvar rather than a global, since Gradio can serve concurrent
requests from different visitors in different threads — each gets its own
isolated value, so one visitor's session id never leaks into another's
request handling.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_UNSCOPED = "unscoped"

_current_session_id: ContextVar[str] = ContextVar(
    "current_session_id", default=_UNSCOPED
)


def get_session_id() -> str:
    """The active session id for the current call — set by the terminal
    loop or the Gradio chat handler before running a turn. Falls back to a
    fixed "unscoped" value if nothing set one (e.g. calling a tool directly
    from a script), which still behaves safely: matches will only ever be
    found against other equally-unscoped records, never against a real
    session's data.
    """
    return _current_session_id.get()


@contextmanager
def use_session(session_id: str) -> Iterator[None]:
    """Scope everything inside this `with` block to `session_id`."""
    token = _current_session_id.set(session_id or _UNSCOPED)
    try:
        yield
    finally:
        _current_session_id.reset(token)
