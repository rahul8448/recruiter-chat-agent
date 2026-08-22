"""Push notifications for new recruiter contact requests, via ntfy.sh
(https://ntfy.sh) — a free, account-free push notification service that
works the same whether this app runs locally or in the cloud (it's just an
outbound HTTPS call, no inbound listener needed).

Setup: pick a private, hard-to-guess topic name (e.g. a random slug — anyone
who knows an ntfy.sh topic name can publish or subscribe to it, since public
topics aren't access-controlled), set NTFY_TOPIC to it in your environment
(or .env locally / your cloud provider's secrets for a deployed instance),
then subscribe to that same topic in the ntfy app (iOS/Android/web).

Entirely optional, and always best-effort: if NTFY_TOPIC isn't set, or the
push fails for any reason (network issue, ntfy.sh down, a timeout), this
logs and returns rather than raising. The contact request has already been
committed to disk by the time this is called — a failed *notification* must
never look like a failed *save*.

Security note: `name` and the latest `message` are sanitized before storage
(see sanitize.py), but they still ultimately trace back to text a chat
visitor supplied, composed by the model rather than passed through
verbatim. A malicious visitor could still steer that text to resemble a
convincing but fake alert delivered under this app's trusted notification
channel. Every notification therefore carries an explicit "unverified"
disclaimer — but no amount of sanitization makes attacker-supplied content
trustworthy. Treat these with the same skepticism as email from a stranger.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .sanitize import sanitize

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5
_MAX_NAME_LENGTH = 100
_MAX_MESSAGE_LENGTH = 500

_DISCLAIMER = (
    "\n\n(Submitted by a chat visitor — not verified. Treat any links or "
    "urgent requests with the same caution as an email from a stranger.)"
)


def _valid_server_url(url: str) -> bool:
    """Sanity-check NTFY_SERVER before posting contact details to it.

    Operator-supplied config rather than attacker-reachable input, but a
    typo that silently sends PII somewhere unintended is worth catching —
    and an unparseable value would otherwise fail confusingly deep inside
    urllib.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def notify_new_contact(
    record: dict[str, Any],
    *,
    is_update: bool,
    settings: Settings | None = None,
) -> bool:
    """Best-effort push notification for a saved/updated contact request.

    Returns True if the notification was sent, False if it was skipped or
    failed. Never raises — a notification problem must not take down the
    tool call that already succeeded at saving the recruiter's details.
    """
    settings = settings or Settings.from_env()

    if not settings.notifications_enabled:
        logger.debug("NTFY_TOPIC not set — skipping push notification.")
        return False

    if not _valid_server_url(settings.ntfy_server):
        logger.error(
            "NTFY_SERVER is not a valid http(s) URL (%r) — skipping push notification.",
            settings.ntfy_server,
        )
        return False

    recruiter_name = sanitize(record.get("name") or "Someone", _MAX_NAME_LENGTH)
    emails = ", ".join(record.get("emails", []))
    phones = ", ".join(record.get("phones", []))
    messages = record.get("messages") or []
    latest_message = sanitize(
        messages[-1].get("text", "") if messages else "", _MAX_MESSAGE_LENGTH
    )

    body_lines = [f"Email: {emails}"]
    if phones:
        body_lines.append(f"Phone: {phones}")
    if latest_message:
        body_lines.append(latest_message)

    payload = {
        "topic": settings.ntfy_topic,
        "title": sanitize(
            f"{'Updated' if is_update else 'New'} recruiter contact: {recruiter_name}",
            _MAX_NAME_LENGTH + 40,
        ),
        "message": "\n".join(body_lines) + _DISCLAIMER,
        "tags": ["briefcase"],
        "priority": 3 if is_update else 4,
    }

    # POST JSON to the server root (not /<topic>) — ntfy's JSON publish API,
    # used instead of the header-based form so names/messages with non-ASCII
    # characters don't risk breaking HTTP header encoding.
    request = urllib.request.Request(
        settings.ntfy_server,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            logger.info("Sent push notification (HTTP %s).", response.status)
            return True
    except urllib.error.HTTPError as exc:
        logger.error("ntfy rejected the notification: HTTP %s %s", exc.code, exc.reason)
    except urllib.error.URLError as exc:
        logger.error("Could not reach ntfy at %s: %s", settings.ntfy_server, exc.reason)
    except OSError as exc:
        # Covers socket timeouts and other low-level I/O failures.
        logger.error("Push notification failed: %s", exc)
    except Exception:
        # Deliberately broad: see the module docstring — nothing here is
        # worth failing a saved contact request over.
        logger.exception("Unexpected failure sending push notification.")
    return False
