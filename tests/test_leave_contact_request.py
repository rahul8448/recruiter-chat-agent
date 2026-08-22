"""Tests for leave_contact_request — the tool that records a recruiter's
contact details. Every test here runs against an isolated temp file (see
conftest.py's `isolated_store`, autouse) instead of the real one.

Several of these are regression tests for specific security fixes made
earlier: session-scoped matching (prevents record tampering across
conversations), input validation (rejects malformed email/phone), and
control-character stripping (prevents terminal escape injection in the
inquiries viewer). Losing any of these silently would be a real
reintroduced vulnerability, not just a style regression — that's why
they're pinned down here instead of only having been checked by hand once.
"""

from __future__ import annotations

import json
from pathlib import Path

from recruiter_chat_agent.session import use_session
from recruiter_chat_agent.tools import leave_contact_request, load_inquiries


def test_valid_submission_creates_a_record() -> None:
    with use_session("session-1"):
        result = leave_contact_request.func(
            name="Jane Doe",
            email="jane@example.com",
            phone="555-1111",
            message="Interested in a backend role.",
        )

    assert "saved" in result.lower()
    records = load_inquiries()
    assert len(records) == 1
    assert records[0]["name"] == "Jane Doe"
    assert records[0]["emails"] == ["jane@example.com"]
    assert records[0]["phones"] == ["555-1111"]
    assert records[0]["messages"][0]["text"] == "Interested in a backend role."


def test_missing_required_fields_are_rejected() -> None:
    with use_session("session-1"):
        result = leave_contact_request.func(name="", email="", phone="", message="")

    assert result.startswith("Error:")
    assert load_inquiries() == []


def test_invalid_email_is_rejected() -> None:
    with use_session("session-1"):
        result = leave_contact_request.func(
            name="Jane", email="not-an-email", phone="555-1111", message="hi"
        )

    assert result.startswith("Error:")
    assert "email" in result.lower()
    assert load_inquiries() == []


def test_invalid_phone_is_rejected() -> None:
    with use_session("session-1"):
        result = leave_contact_request.func(
            name="Jane", email="jane@example.com", phone="abc", message="hi"
        )

    assert result.startswith("Error:")
    assert "phone" in result.lower()
    assert load_inquiries() == []


def test_too_many_emails_in_one_call_is_rejected() -> None:
    many_emails = ", ".join(f"user{i}@example.com" for i in range(8))
    with use_session("session-1"):
        result = leave_contact_request.func(
            name="Jane", email=many_emails, phone="555-1111", message="hi"
        )

    assert result.startswith("Error:")
    assert load_inquiries() == []


def test_same_session_repeat_call_merges_into_one_record() -> None:
    with use_session("session-1"):
        leave_contact_request.func(
            name="Jane Doe",
            email="jane@example.com",
            phone="555-1111",
            message="First contact.",
        )
        leave_contact_request.func(
            name="Jane Doe",
            email="jane@example.com",
            phone="555-2222",
            message="Follow-up.",
        )

    records = load_inquiries()
    assert len(records) == 1
    assert set(records[0]["phones"]) == {"555-1111", "555-2222"}
    assert [m["text"] for m in records[0]["messages"]] == [
        "First contact.",
        "Follow-up.",
    ]


def test_different_session_cannot_tamper_with_another_sessions_record() -> None:
    """Regression test for the record-tampering fix: a different
    conversation claiming the exact same email must never merge into (and
    so overwrite) another session's already-saved record.
    """
    with use_session("session-A"):
        leave_contact_request.func(
            name="Jane Doe",
            email="jane@example.com",
            phone="555-1111",
            message="Legit message.",
        )

    with use_session("session-B-attacker"):
        # Deliberately a validation-passing phone number (not "555-EVIL" or
        # similar) — the point of this test is to isolate the
        # session-scoping defense specifically. A garbage phone number
        # would get rejected by the separate input-validation fix (#6)
        # before ever reaching the session-matching logic, which would
        # make this test pass for the wrong reason.
        leave_contact_request.func(
            name="FAKE NAME",
            email="jane@example.com",
            phone="555-0000",
            message="Tampered message.",
            replace_phone=True,
            replace_message=True,
        )

    records = load_inquiries()
    assert len(records) == 2  # not merged into one

    session_a_record = next(r for r in records if r["session_id"] == "session-A")
    assert session_a_record["name"] == "Jane Doe"
    assert session_a_record["phones"] == ["555-1111"]
    assert session_a_record["messages"][-1]["text"] == "Legit message."


def test_replace_phone_overwrites_instead_of_adding() -> None:
    with use_session("session-1"):
        leave_contact_request.func(
            name="Jane", email="jane@example.com", phone="555-1111", message="a"
        )
        leave_contact_request.func(
            name="Jane",
            email="jane@example.com",
            phone="555-9999",
            message="b",
            replace_phone=True,
        )

    records = load_inquiries()
    assert records[0]["phones"] == ["555-9999"]


def test_replace_message_overwrites_last_note_instead_of_appending() -> None:
    with use_session("session-1"):
        leave_contact_request.func(
            name="Jane", email="jane@example.com", phone="555-1111", message="original"
        )
        leave_contact_request.func(
            name="Jane",
            email="jane@example.com",
            phone="555-1111",
            message="corrected",
            replace_message=True,
        )

    records = load_inquiries()
    assert [m["text"] for m in records[0]["messages"]] == ["corrected"]


def test_control_characters_are_stripped_from_stored_text() -> None:
    """Regression test for the terminal-escape-injection fix."""
    with use_session("session-1"):
        leave_contact_request.func(
            name="Jane\x1b[31mDoe\x07",
            email="jane@example.com",
            phone="555-1111",
            message="Hello\x00 world",
        )

    records = load_inquiries()
    assert "\x1b" not in records[0]["name"]
    assert "\x07" not in records[0]["name"]
    assert "\x00" not in records[0]["messages"][0]["text"]


def test_oversized_message_is_truncated_not_rejected() -> None:
    with use_session("session-1"):
        result = leave_contact_request.func(
            name="Jane", email="jane@example.com", phone="555-1111", message="A" * 3000
        )

    assert "saved" in result.lower()
    text = load_inquiries()[0]["messages"][0]["text"]
    assert len(text) == 2000
    assert text.endswith("…")


def test_store_is_always_valid_json_on_disk(isolated_store: Path) -> None:
    with use_session("session-1"):
        leave_contact_request.func(
            name="Jane", email="jane@example.com", phone="555-1111", message="hi"
        )

    data = json.loads(isolated_store.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
