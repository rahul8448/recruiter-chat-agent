"""Browser chat UI (Gradio) for recruiters — the same agent logic as
agent.py's terminal loop, behind a different front end.

Run with `uv run recruiter-chat-web`.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

import anthropic
import gradio as gr
from dotenv import load_dotenv

from .agent import build_client, run_turn
from .config import Settings
from .context import build_system_prompt
from .errors import RecruiterChatError
from .lifecycle import GracefulShutdown
from .logging_setup import configure_logging
from .profile import candidate_name, has_placeholder_values
from .ratelimit import RateLimiter
from .session import use_session

logger = logging.getLogger(__name__)

_STYLE_CSS_PATH = Path(__file__).parent / "static" / "style.css"

# Indigo/violet accent on a neutral slate base, Inter for UI text — see
# static/style.css for the header, bubble, and chip styling built on top.
_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.indigo,
    secondary_hue=gr.themes.colors.violet,
    neutral_hue=gr.themes.colors.slate,
    font=gr.themes.GoogleFont("Inter"),
)

# What a visitor sees when something breaks. Deliberately generic: raw SDK
# error text can leak internals to an anonymous visitor.
_API_ERROR_MESSAGE = (
    "⚠️ Something went wrong answering that — please try again in a moment."
)
_NETWORK_ERROR_MESSAGE = "⚠️ Network error — please try again in a moment."
_EMPTY_REPLY_MESSAGE = "⚠️ No response was generated — please try again."


def parse_auth_credentials(raw: str) -> list[tuple[str, str]] | None:
    """Parse RECRUITER_CHAT_AUTH ("user1:pass1,user2:pass2") into the
    (username, password) pairs Gradio's `auth=` expects.

    Returns None (no login wall) when unset — that's the intended default,
    so recruiters can reach the chat without a shared password; rate
    limiting is the actual defense against abuse.
    """
    if not raw.strip():
        return None

    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning(
                "Skipping a malformed RECRUITER_CHAT_AUTH entry (expected user:pass)."
            )
            continue
        username, _, password = entry.partition(":")
        username = username.strip()
        if not username or not password:
            logger.warning(
                "Skipping a RECRUITER_CHAT_AUTH entry with an empty username or password."
            )
            continue
        pairs.append((username, password))

    return pairs or None


def _session_id(request: gr.Request | None) -> str:
    """Gradio's per-page-load session hash — the natural unit of "one
    recruiter's conversation" without requiring login. A page reload gets a
    brand-new session_hash, so this alone is trivially resettable;
    _client_ip below is the backstop against that.
    """
    session_hash = getattr(request, "session_hash", None) if request else None
    return session_hash or "no-session"


def _client_ip(request: gr.Request | None) -> str:
    """Best-effort client IP, for the broader rate limit that catches one
    visitor opening many sessions (tabs/reloads) to dodge the per-session
    limit.
    """
    if request is None:
        return "unknown"
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None
    return host or "unknown"


def build_demo(
    client: anthropic.Anthropic,
    system_prompt: str,
    name: str,
    settings: Settings,
) -> gr.ChatInterface:
    """Assemble the Gradio chat interface."""
    limits = settings.rate_limits
    session_limiter = RateLimiter(
        max_calls=limits.session_max_messages,
        period_seconds=limits.session_window_seconds,
    )
    ip_limiter = RateLimiter(
        max_calls=limits.ip_max_messages,
        period_seconds=limits.ip_window_seconds,
    )

    def chat_fn(
        message: str,
        history: list[dict[str, str]],
        request: gr.Request | None = None,
    ) -> str:
        session_id = _session_id(request)

        if not session_limiter.allow(session_id):
            wait = session_limiter.retry_after_seconds(session_id)
            logger.info("Session rate limit hit (session=%s).", session_id)
            return (
                f"⏳ You're sending messages a bit fast in this conversation — please "
                f"wait about {wait}s and try again."
            )

        client_ip = _client_ip(request)
        if not ip_limiter.allow(client_ip):
            wait = ip_limiter.retry_after_seconds(client_ip)
            logger.warning("IP rate limit hit (ip=%s).", client_ip)
            return (
                f"⏳ Too many requests from your network — please wait about {wait}s "
                "and try again."
            )

        # Gradio hands us the full turn history as {"role", "content"}
        # dicts — the same plain-text turn format the CLI loop uses, so no
        # conversion is needed.
        messages: list[dict[str, object]] = [
            {"role": turn["role"], "content": turn["content"]} for turn in history
        ]
        messages.append({"role": "user", "content": message})

        try:
            # Scopes leave_contact_request's record matching to this Gradio
            # session — see session.py for why.
            with use_session(session_id):
                reply = run_turn(client, system_prompt, messages)
        except anthropic.APIStatusError as exc:
            logger.error(
                "Anthropic API error (HTTP %s) for session=%s: %s",
                exc.status_code,
                session_id,
                exc.message,
            )
            return _API_ERROR_MESSAGE
        except anthropic.APIConnectionError:
            logger.error("Could not reach the Anthropic API.", exc_info=True)
            return _NETWORK_ERROR_MESSAGE
        except RecruiterChatError as exc:
            logger.error("Agent error for session=%s: %s", session_id, exc)
            return _API_ERROR_MESSAGE
        except Exception:
            logger.exception("Unexpected error handling a turn (session=%s).", session_id)
            return _API_ERROR_MESSAGE

        return reply or _EMPTY_REPLY_MESSAGE

    return gr.ChatInterface(
        fn=chat_fn,
        chatbot=gr.Chatbot(show_label=False, height=480),
        title=f"Chat with {name}'s recruiting assistant",
        description=(
            f"Ask about {name}'s background, skills, and experience. "
            f"If you'd like {name} to follow up, just leave your contact info."
        ),
        examples=[
            "What's your experience with distributed systems?",
            "Tell me about your most recent role.",
            "What are you looking for in your next role?",
        ],
    )


def create_app() -> tuple[gr.ChatInterface, dict[str, object]]:
    """Build the chat UI and the kwargs it should be launched with.

    Shared by `main()` and by the Hugging Face Spaces entry point
    (`app.py` at the repo root), so a hosted deployment and a local run
    can't drift apart. Callers own the actual `demo.launch(...)`, because
    what belongs there differs: locally we want to pop open a browser tab,
    while a Space is already serving on its own host and port.

    Raises RecruiterChatError if the profile or configuration is unusable.
    """
    load_dotenv()
    # Configure logging from the raw env var first, so that a *bad* setting
    # further down is reported as a clean log line rather than a traceback.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    settings = Settings.from_env()
    name = candidate_name(settings)
    if has_placeholder_values(settings):
        logger.warning(
            "%s's profile still contains 'TODO:' placeholder values — fill them "
            "in before pointing recruiters at this.",
            name,
        )
    client = build_client()

    auth = parse_auth_credentials(settings.auth_raw)
    if auth is not None:
        logger.info("Login required — %d account(s) configured.", len(auth))
    else:
        logger.info(
            "No login wall (the default). Rate limits: %d msg/%ds per session, "
            "%d msg/%ds per IP.",
            settings.rate_limits.session_max_messages,
            settings.rate_limits.session_window_seconds,
            settings.rate_limits.ip_max_messages,
            settings.rate_limits.ip_window_seconds,
        )
    logger.info(
        "Inquiries store: %s (encryption %s, push notifications %s).",
        settings.inquiries_path,
        "on" if settings.encryption_enabled else "off",
        "on" if settings.notifications_enabled else "off",
    )
    if settings.notifications_enabled is False:
        logger.warning(
            "NTFY_TOPIC is not set. On a host with an ephemeral filesystem "
            "(most free tiers), %s is wiped on restart — without push "
            "notifications a recruiter's details could be lost.",
            settings.inquiries_path.name,
        )

    demo = build_demo(client, build_system_prompt(name), name, settings)
    launch_kwargs: dict[str, object] = {
        "theme": _THEME,
        "css_paths": _STYLE_CSS_PATH,
        "auth": auth,
        "auth_message": f"Sign in to chat with {name}'s recruiting assistant.",
    }
    return demo, launch_kwargs


def main() -> int:
    """Entry point for `recruiter-chat-web`. Returns a process exit code."""
    try:
        demo, launch_kwargs = create_app()
    except RecruiterChatError as exc:
        logger.error("%s", exc)
        return 1

    # Handle both stop signals by closing the server, which breaks Gradio
    # out of its blocking loop. SIGTERM is what a cloud platform sends on
    # deploy; Python's default action for it is to die instantly with no
    # cleanup at all. SIGINT is handled here too because Gradio's own
    # KeyboardInterrupt path only fires when there's a controlling
    # terminal — a backgrounded or containerized process ignores SIGINT
    # entirely without this.
    shutdown = GracefulShutdown()
    shutdown.on_shutdown(demo.close)
    shutdown.install(signals=(signal.SIGINT, signal.SIGTERM))

    print("Starting the recruiter chat UI — press Ctrl+C to stop.\n")
    try:
        # inbrowser=True is local-only: Gradio doesn't auto-open a tab by
        # default, it just prints the URL. A hosted deployment (app.py)
        # omits it, since there's no browser on the server.
        demo.launch(inbrowser=True, **launch_kwargs)
    except KeyboardInterrupt:
        # Gradio's own shutdown path can raise a second KeyboardInterrupt
        # while it's mid-cleanup — swallow it rather than printing a raw
        # traceback over an intentional Ctrl+C.
        logger.info("Interrupted — shutting down.")
    except OSError as exc:
        # Most often "address already in use".
        logger.error("Could not start the web server: %s", exc)
        return 1
    except Exception:
        logger.exception("The web server stopped unexpectedly.")
        return 1
    finally:
        try:
            demo.close()
        except Exception:
            logger.debug("Error while closing the Gradio server.", exc_info=True)

    print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
