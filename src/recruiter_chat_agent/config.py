"""Centralized, validated configuration.

Every environment variable this app reads is declared here, in one place,
with its default and its validation — rather than scattered `os.environ.get`
calls across modules where a typo'd name or a bad value fails silently (or
much later, at the worst possible moment).

Settings are read fresh from the environment rather than cached at import
time. That's deliberate: entry points call `load_dotenv()` *after* imports
happen, so anything captured at import time would miss a locally-set `.env`
value — a bug this project has actually hit before. Reading a handful of
env vars per call is far too cheap to be worth the caching risk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigurationError

_DATA_DIR = Path(__file__).parent / "data"

DEFAULT_PROFILE_PATH = _DATA_DIR / "profile.yaml"
DEFAULT_INQUIRIES_PATH = _DATA_DIR / "inquiries.json"

# Chat model + token ceiling for one reply. Kept here so both front ends and
# any future caller agree on them.
MODEL = "claude-opus-5"
MAX_TOKENS = 4096


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read an int env var, falling back to `default` with a clear error if
    it's set to something unparseable — rather than crashing with a bare
    ValueError deep inside a request handler.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be a whole number, got {raw!r}."
        ) from exc
    if value < minimum:
        raise ConfigurationError(
            f"{name} must be at least {minimum}, got {value}."
        )
    return value


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_path(name: str, default: Path) -> Path:
    raw = _env_str(name)
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class RateLimitSettings:
    """Two tiers: a per-conversation cap plus a broader per-IP backstop
    (a browser reload gets a fresh session, so a session-only limit is
    trivially bypassable on its own).
    """

    session_max_messages: int = 20
    session_window_seconds: int = 600
    ip_max_messages: int = 60
    ip_window_seconds: int = 600

    @classmethod
    def from_env(cls) -> RateLimitSettings:
        return cls(
            session_max_messages=_env_int("SESSION_RATE_LIMIT_MAX_MESSAGES", 20),
            session_window_seconds=_env_int("SESSION_RATE_LIMIT_WINDOW_SECONDS", 600),
            ip_max_messages=_env_int("IP_RATE_LIMIT_MAX_MESSAGES", 60),
            ip_window_seconds=_env_int("IP_RATE_LIMIT_WINDOW_SECONDS", 600),
        )


@dataclass(frozen=True)
class Settings:
    """Everything this app reads from the environment."""

    profile_path: Path = DEFAULT_PROFILE_PATH
    inquiries_path: Path = DEFAULT_INQUIRIES_PATH

    log_level: str = "INFO"

    # Optional at-rest encryption for the inquiries store.
    inquiries_encryption_key: str = ""

    # Optional ntfy.sh push notifications.
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"

    # Optional Gradio login wall. Empty (the default) means no login —
    # recruiters can open the link and chat, with rate limiting as the
    # actual abuse defense.
    auth_raw: str = ""

    rate_limits: RateLimitSettings = field(default_factory=RateLimitSettings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            # Overridable so a container can point these at a mounted
            # volume — the packaged default lives inside site-packages,
            # which is typically read-only and always ephemeral.
            profile_path=_env_path("PROFILE_PATH", DEFAULT_PROFILE_PATH),
            inquiries_path=_env_path("INQUIRIES_PATH", DEFAULT_INQUIRIES_PATH),
            log_level=_env_str("LOG_LEVEL", "INFO").upper() or "INFO",
            inquiries_encryption_key=_env_str("INQUIRIES_ENCRYPTION_KEY"),
            ntfy_topic=_env_str("NTFY_TOPIC"),
            ntfy_server=_env_str("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
            or "https://ntfy.sh",
            auth_raw=_env_str("RECRUITER_CHAT_AUTH"),
            rate_limits=RateLimitSettings.from_env(),
        )

    @property
    def encryption_enabled(self) -> bool:
        return bool(self.inquiries_encryption_key)

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.ntfy_topic)


def has_api_credentials() -> bool:
    """Whether an Anthropic credential is reachable.

    A bare `Anthropic()` client also picks up an `ant auth login` profile,
    so an unset env var alone doesn't prove there are no credentials — this
    only reports on the env vars we can actually check.
    """
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
