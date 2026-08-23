"""Tools the agent calls to answer recruiter questions and to record a
recruiter's contact details.

The read tools are deliberately thin: they pull from the profile (see
profile.py) and format it. Keeping facts behind tool calls — rather than
pasting the whole profile into the system prompt — means answers are
grounded in that file rather than in the model's memory of the
conversation.

Every tool is wrapped in `_tool_guard`, which turns an unexpected failure
into an error *string* rather than an exception. That matters: an exception
raised inside the SDK's tool runner aborts the whole turn, so the recruiter
sees a generic failure and the model gets no chance to recover. A returned
string goes back to the model as a normal tool result, which it can explain
or work around.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from anthropic import beta_tool

from .errors import RecruiterChatError
from .notify import notify_new_contact
from .profile import load_profile
from .sanitize import sanitize
from .session import get_session_id
from .storage import read_records, transaction

logger = logging.getLogger(__name__)

_GENERIC_TOOL_ERROR = (
    "That information isn't available right now due to a system error. Let the "
    "recruiter know you couldn't look it up, and offer to help with something else."
)


def _tool_guard(func: Callable[..., str]) -> Callable[..., str]:
    """Convert exceptions into a tool result the model can act on."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return func(*args, **kwargs)
        except RecruiterChatError as exc:
            # Our own errors carry operator-actionable detail — log it in
            # full, but hand the model a short, non-leaky summary.
            logger.error("Tool %s failed: %s", func.__name__, exc)
            return f"Error: {exc}"
        except Exception:
            logger.exception("Unexpected failure in tool %s", func.__name__)
            return _GENERIC_TOOL_ERROR

    return wrapper


def _format_experience(entry: dict[str, Any]) -> str:
    highlights = "\n".join(f"  - {h}" for h in entry.get("highlights", []))
    header = (
        f"{entry.get('title')} at {entry.get('company')} "
        f"({entry.get('start')} - {entry.get('end')})"
    )
    return f"{header}\n{highlights}" if highlights else header


def _format_project(entry: dict[str, Any]) -> str:
    tech = ", ".join(entry.get("tech", []))
    return (
        f"{entry.get('name')}: {entry.get('description')}\n"
        f"  Tech: {tech}\n"
        f"  Link: {entry.get('link')}"
    )


@beta_tool
@_tool_guard
def get_summary() -> str:
    """Get the candidate's headline, professional summary, location, and job
    preferences (what they're looking for, work type, availability).

    Call this first for any general "tell me about yourself" or "what are
    you looking for in your next role" style question.
    """
    profile = load_profile()
    c = profile.get("candidate", {})
    p = profile.get("preferences", {})
    return (
        f"Name: {c.get('name')}\n"
        f"Headline: {c.get('headline')}\n"
        f"Location: {c.get('location')}\n"
        f"Summary: {c.get('summary')}\n"
        f"Seeking: {p.get('seeking')}\n"
        f"Work type: {p.get('work_type')}\n"
        f"Availability: {p.get('availability')}\n"
        f"Work authorization: {p.get('work_authorization')}\n"
        f"What they're looking for in their next role: {p.get('looking_for')}"
    )


@beta_tool
@_tool_guard
def list_skills(category: str = "") -> str:
    """List the candidate's technical skills.

    Args:
        category: Optional category to filter by — one of "languages",
            "frameworks", "infrastructure", "data", "other". Leave empty to
            get every category.
    """
    skills = load_profile().get("skills", {})
    if category:
        values = skills.get(category)
        if values is None:
            return f"Unknown category '{category}'. Known categories: {', '.join(skills)}"
        return f"{category}: {', '.join(values)}"
    return "\n".join(f"{cat}: {', '.join(values)}" for cat, values in skills.items())


@beta_tool
@_tool_guard
def get_experience(company: str = "") -> str:
    """Get work experience history, including titles, dates, and highlights.

    Args:
        company: Optional company name to filter to a single role. Leave
            empty to get the full work history.
    """
    experience = load_profile().get("experience", [])
    if company:
        matches = [
            e for e in experience if company.lower() in e.get("company", "").lower()
        ]
        if not matches:
            return f"No experience entry found for '{company}'."
        return "\n\n".join(_format_experience(e) for e in matches)
    return "\n\n".join(_format_experience(e) for e in experience)


@beta_tool
@_tool_guard
def get_projects(name: str = "") -> str:
    """Get details on notable projects: description, tech stack, and link.

    Args:
        name: Optional project name to filter to a single project. Leave
            empty to get all projects.
    """
    projects = load_profile().get("projects", [])
    if name:
        matches = [p for p in projects if name.lower() in p.get("name", "").lower()]
        if not matches:
            return f"No project found matching '{name}'."
        return "\n\n".join(_format_project(p) for p in matches)
    return "\n\n".join(_format_project(p) for p in projects)


@beta_tool
@_tool_guard
def get_education() -> str:
    """Get the candidate's education history (institution, degree, year)."""
    education = load_profile().get("education", [])
    return "\n".join(
        f"{e.get('degree')}, {e.get('institution')} ({e.get('year')})"
        for e in education
    )


@beta_tool
@_tool_guard
def get_certifications() -> str:
    """Get the candidate's professional certifications."""
    certifications = load_profile().get("certifications", [])
    if not certifications:
        return "No certifications listed."
    return "\n".join(f"- {c}" for c in certifications)


@beta_tool
@_tool_guard
def get_work_authorization() -> str:
    """Get the candidate's work authorization / visa / immigration status.

    Call this whenever the recruiter asks about work authorization, visa
    status, or sponsorship needs — a very common early screening question,
    worth answering precisely and directly rather than deflecting.
    """
    status = load_profile().get("preferences", {}).get("work_authorization")
    return status or "Not specified — ask the candidate directly."


@beta_tool
@_tool_guard
def get_contact_info() -> str:
    """Get the candidate's contact details — whichever of email, phone,
    LinkedIn, and GitHub they've listed — for scheduling a call or
    following up.
    """
    contact = load_profile().get("candidate", {}).get("contact", {})
    return "\n".join(f"{k}: {v}" for k, v in contact.items())


# --- Recording a recruiter's contact details --------------------------------

MAX_NAME_LENGTH = 200
MAX_MESSAGE_LENGTH = 2000
MAX_EMAIL_LENGTH = 254  # RFC 5321's practical limit
MAX_PHONE_LENGTH = 32
MAX_VALUES_PER_FIELD = 5  # cap on comma-separated emails/phones per call

# Deliberately permissive — full RFC 5322 validation rejects plenty of real
# addresses and is famously not worth chasing. This just catches obvious
# garbage (missing @, no domain, embedded whitespace).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _split_values(raw: str) -> list[str]:
    return [v.strip() for v in raw.split(",") if v.strip()]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for v in values:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            result.append(v)
    return result


def _merge_values(existing: list[str], new: list[str], replace: bool) -> list[str]:
    return _dedupe(new) if replace else _dedupe(existing + new)


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def _looks_like_phone(value: str) -> bool:
    # Permissive about formatting (spaces, dashes, parens, a leading +) —
    # phone formats vary too widely worldwide to validate strictly.
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15


def _find_matching_record(
    records: list[dict[str, Any]], emails: list[str], phones: list[str], session_id: str
) -> dict[str, Any] | None:
    """Find a recruiter's existing entry by overlap on email OR phone, so an
    update or an added alternate contact method within the SAME conversation
    resolves to the same record instead of spawning a duplicate.

    Scoped to `session_id` on purpose: without this, anyone who merely knew
    (or guessed) a real recruiter's email could submit a call from an
    unrelated conversation that silently overwrote that recruiter's stored
    phone number or appended a fabricated "correction" onto their record —
    record tampering with no proof of ownership. Scoping matches to the
    session that created the record means a different visitor's session can
    never touch it, even with an identical email/phone. The tradeoff: the
    same real recruiter returning in a brand-new session (a different day,
    a fresh tab) creates a new entry rather than merging into their old one
    — an acceptable cost for closing a real tampering vector; the candidate
    reviewing the store can still recognize repeat entries by email.
    """
    email_set = {e.lower() for e in emails}
    phone_set = {p.lower() for p in phones}
    for r in records:
        if r.get("session_id") != session_id:
            continue
        stored_emails = {e.lower() for e in r.get("emails", [])}
        stored_phones = {p.lower() for p in r.get("phones", [])}
        if email_set & stored_emails or (phone_set and phone_set & stored_phones):
            return r
    return None


def _validate_contact_fields(
    name: str, emails: list[str], phones: list[str], message: str
) -> str | None:
    """Return an error message for the model, or None if everything is OK."""
    missing = [
        field
        for field, value in (
            ("name", name),
            ("email", emails),
            ("phone", phones),
            ("message", message),
        )
        if not value
    ]
    if missing:
        return (
            f"Error: missing required field(s): {', '.join(missing)}. Name, email, and "
            "phone must come from the recruiter — ask for whichever they haven't given. "
            "If message is missing, don't ask — write a short note yourself summarizing "
            "why they're reaching out, based on the conversation so far."
        )

    if len(emails) > MAX_VALUES_PER_FIELD:
        return (
            f"Error: that's {len(emails)} email addresses in one call — max "
            f"{MAX_VALUES_PER_FIELD}. Send the most relevant ones."
        )
    if len(phones) > MAX_VALUES_PER_FIELD:
        return (
            f"Error: that's {len(phones)} phone numbers in one call — max "
            f"{MAX_VALUES_PER_FIELD}. Send the most relevant ones."
        )

    bad_emails = [e for e in emails if not _looks_like_email(e)]
    if bad_emails:
        return (
            f"Error: this doesn't look like a valid email address: {', '.join(bad_emails)}. "
            "Confirm the correct address with the recruiter and try again."
        )
    bad_phones = [p for p in phones if not _looks_like_phone(p)]
    if bad_phones:
        return (
            f"Error: this doesn't look like a valid phone number: {', '.join(bad_phones)}. "
            "Confirm the correct number with the recruiter and try again."
        )

    return None


@beta_tool
@_tool_guard
def leave_contact_request(
    name: str,
    email: str,
    phone: str,
    message: str,
    replace_email: bool = False,
    replace_phone: bool = False,
    replace_message: bool = False,
) -> str:
    """Record or update a recruiter's contact details so the candidate can
    reach back out.

    Name, email, and phone are all REQUIRED — ask the recruiter for whichever
    of those they haven't given before calling this. `message` is required
    too, but doesn't have to come from the recruiter verbatim: if they didn't
    state a reason for reaching out, write a short 1-2 sentence note yourself
    summarizing it from the conversation so far (e.g. the role/company they
    mentioned, or why they're interested) — never leave it blank or generic.

    Matches the recruiter by overlap on email OR phone against what's
    already on file, so it's safe — and correct — to call this more than
    once for the same recruiter as they give you more info over the
    conversation: it updates their one entry instead of creating a
    duplicate. Always reuse whatever email/phone you already have for this
    recruiter in later calls; that's what lets a new call find their
    existing entry.

    - A recruiter can have more than one email or phone on file. Pass
      comma-separated values (e.g. phone="555-1111, 555-2222") if they give
      you several at once. By default, new values are ADDED to what's
      already stored (deduplicated) — so "here's an alternative email" or
      "also reach me at ..." just works without you needing to repeat every
      value given earlier.
    - If the recruiter is CORRECTING a value rather than adding one (e.g.
      "actually my number is now X", "use this email instead"), set
      replace_email / replace_phone (whichever applies) to True so the old
      value is dropped instead of kept alongside the new one.
    - `message` is appended as a new note by default. If the recruiter asks
      you to fix or update what you already recorded, set
      replace_message=True to overwrite the most recent note instead of
      adding another one.
    - Each email/phone is checked for a plausible format (not exhaustively —
      just enough to catch obvious garbage) and there's a cap of 5 values
      per field per call. A rejection returns an Error: message explaining
      why — read it, fix the value with the recruiter, and retry rather
      than giving up.

    Args:
        name: Recruiter's name. Required.
        email: Recruiter's email address, or comma-separated addresses.
            Required — used (with phone) to find an existing entry for this
            recruiter, so reuse the same value(s) across calls.
        phone: Recruiter's phone number(s), comma-separated if more than
            one. Required.
        message: A short note for the candidate — e.g. the role, company, or
            why they're reaching out. Required; write one yourself from
            context if the recruiter didn't give one.
        replace_email: If True, replace the stored email(s) with `email`
            instead of adding to them. Default False.
        replace_phone: If True, replace the stored phone number(s) with
            `phone` instead of adding to them. Default False.
        replace_message: If True, replace the most recent stored note with
            `message` instead of appending a new one. Default False.
    """
    name = sanitize(name, MAX_NAME_LENGTH)
    emails = [sanitize(e, MAX_EMAIL_LENGTH) for e in _split_values(email)]
    phones = [sanitize(p, MAX_PHONE_LENGTH) for p in _split_values(phone)]
    message = sanitize(message, MAX_MESSAGE_LENGTH)

    validation_error = _validate_contact_fields(name, emails, phones, message)
    if validation_error is not None:
        logger.info("Rejected a contact request: failed validation.")
        return validation_error

    now = datetime.now(timezone.utc).isoformat()
    session_id = get_session_id()

    # Everything inside the transaction is serialized against concurrent
    # callers and written atomically. The notification is deliberately sent
    # *after* it commits: it makes a network call with a multi-second
    # timeout, and holding the store lock across that would serialize every
    # other recruiter's submission behind it.
    with transaction() as records:
        existing = _find_matching_record(records, emails, phones, session_id)

        if existing is None:
            existing = {
                "session_id": session_id,
                "emails": _dedupe(emails),
                "phones": _dedupe(phones),
                "name": name,
                "first_contacted": now,
                "last_updated": now,
                "messages": [{"timestamp": now, "text": message}],
            }
            records.append(existing)
            verb = "saved"
        else:
            existing["emails"] = _merge_values(
                existing.get("emails", []), emails, replace_email
            )
            existing["phones"] = _merge_values(
                existing.get("phones", []), phones, replace_phone
            )
            existing["name"] = name
            existing["last_updated"] = now
            if replace_message and existing["messages"]:
                existing["messages"][-1] = {"timestamp": now, "text": message}
            else:
                existing["messages"].append({"timestamp": now, "text": message})
            verb = "updated"

        # Snapshot for the notification, so we're not reading a structure
        # another thread may be mutating once we're outside the lock.
        snapshot = dict(existing)
        total_records = len(records)

    # No PII in the log line — see logging_setup.py.
    logger.info(
        "Contact request %s (session=%s, %d record(s) on file).",
        verb,
        session_id,
        total_records,
    )
    notify_new_contact(snapshot, is_update=(verb == "updated"))
    return f"Thanks — I've {verb} your details. They'll be in touch."


def load_inquiries() -> list[dict[str, Any]]:
    """Load every recorded recruiter contact request, oldest-added first.

    Raises StorageError if the store exists but can't be read — the
    inquiries viewer surfaces that rather than showing a misleading
    "no contacts yet".
    """
    return read_records()


ALL_TOOLS = [
    get_summary,
    list_skills,
    get_experience,
    get_projects,
    get_education,
    get_certifications,
    get_work_authorization,
    get_contact_info,
    leave_contact_request,
]
