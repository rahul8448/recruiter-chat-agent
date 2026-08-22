"""Logging setup for the app's entry points.

One place to configure logging, called once by each entry point (CLI, web
UI, inquiries viewer). Library modules never configure logging themselves —
they just do `logger = logging.getLogger(__name__)` and log; this decides
where that output goes and at what level.

**A note on PII:** this app handles other people's contact details. Log
lines here deliberately record *what happened* (a contact was saved, how
many records are on file, which session) and never the recruiter's name,
email, phone, or message text. Logs routinely end up somewhere less
protected than the encrypted, permission-restricted store those details
live in — so keep them out of log lines when adding new ones.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

# Third-party loggers too chatty to be useful here. "httpx2" is not a typo:
# the Anthropic SDK's vendored HTTP client logs an INFO line per API call
# under that name, which would otherwise drown out our own output.
_NOISY_LOGGERS = (
    "httpx",
    "httpx2",
    "httpcore",
    "urllib3",
    "anthropic",
    "gradio",
    "asyncio",
)


def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    """Configure root logging for an entry point.

    Idempotent — calling it twice is a no-op unless `force=True`, so an
    entry point that imports another entry point's module can't end up
    with duplicated handlers (and duplicated log lines).
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        # Don't fail startup over a typo'd LOG_LEVEL — fall back to INFO
        # and say so, which is far more useful than a crash.
        resolved = logging.INFO
        invalid_level = level
    else:
        invalid_level = None

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    # Replace any pre-existing handlers so `force=True` genuinely
    # reconfigures rather than stacking another handler on top.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(resolved, logging.WARNING))

    _CONFIGURED = True

    if invalid_level is not None:
        logging.getLogger(__name__).warning(
            "Unknown LOG_LEVEL %r — falling back to INFO. Valid values: "
            "DEBUG, INFO, WARNING, ERROR, CRITICAL.",
            invalid_level,
        )
