"""Shared agent logic, plus the terminal chat loop.

`run_turn` is the one place the tool-calling loop lives; both the CLI here
and the Gradio UI in web.py call it, so their behavior can't drift apart.
The agent's scope and guardrails live in context.py, not here.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid

import anthropic
from dotenv import load_dotenv

from .config import MAX_TOKENS, MODEL, Settings, has_api_credentials
from .context import build_system_prompt
from .errors import ConfigurationError, RecruiterChatError
from .lifecycle import GracefulShutdown
from .logging_setup import configure_logging
from .profile import candidate_name, has_placeholder_values
from .session import use_session
from .tools import ALL_TOOLS

logger = logging.getLogger(__name__)

__all__ = [
    "MODEL",
    "MAX_TOKENS",
    "build_system_prompt",
    "build_client",
    "run_turn",
    "main",
]

Message = dict[str, object]


def build_client() -> anthropic.Anthropic:
    """Construct the Anthropic client, failing early with a clear message.

    An unset ANTHROPIC_API_KEY doesn't by itself mean there are no
    credentials — a bare client also picks up an `ant auth login` profile —
    so this only refuses when the client itself can't resolve any.
    """
    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        raise ConfigurationError(
            "Could not construct the Anthropic client: %s" % exc
        ) from exc

    # The SDK doesn't validate credentials at construction — a client with
    # no key builds fine and only fails on the first request. Warn now
    # rather than letting every chat turn fail mysteriously later. This is
    # a warning, not an error, because an `ant auth login` profile is
    # resolved later and wouldn't show up on these attributes.
    if client.api_key is None and getattr(client, "auth_token", None) is None:
        logger.warning(
            "No ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN in the environment. "
            "Requests will fail unless an `ant auth login` profile is available."
        )
    return client


def run_turn(
    client: anthropic.Anthropic,
    system_prompt: str,
    messages: list[Message],
) -> str:
    """Run one agentic turn against `messages` (which must already end with
    the latest user turn) and return the final reply text.

    Propagates anthropic.APIStatusError / APIConnectionError so callers can
    decide how to present a failure; returns "" if the model produced no
    text at all.
    """
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        tools=ALL_TOOLS,
        messages=messages,
    )

    last_message = None
    for response in runner:
        last_message = response

    if last_message is None:
        logger.warning("Tool runner produced no messages for this turn.")
        return ""

    reply = "\n".join(
        block.text for block in last_message.content if block.type == "text"
    ).strip()

    if not reply:
        logger.warning(
            "Turn ended with no text content (stop_reason=%s).",
            getattr(last_message, "stop_reason", "unknown"),
        )
    return reply


def _warn_about_placeholders(name: str) -> None:
    logger.warning(
        "%s's profile still contains 'TODO:' placeholder values — fill them in "
        "before pointing recruiters at this.",
        name,
    )


def main() -> int:
    """Entry point for `recruiter-chat`. Returns a process exit code."""
    load_dotenv()
    # Configure logging from the raw env var first, so that a *bad* setting
    # further down is reported as a clean log line rather than a traceback.
    # configure_logging tolerates an invalid level on its own.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    shutdown = GracefulShutdown()
    shutdown.install()

    try:
        settings = Settings.from_env()
        name = candidate_name(settings)
        if has_placeholder_values(settings):
            _warn_about_placeholders(name)
        client = build_client()
    except RecruiterChatError as exc:
        # Startup problems are the operator's to fix — one clear line beats
        # a traceback.
        logger.error("%s", exc)
        return 1

    if not has_api_credentials():
        logger.debug("No API key env var set; relying on an `ant auth login` profile.")

    system_prompt = build_system_prompt(name)

    # One session id per run — scopes leave_contact_request's record
    # matching so a fresh run never merges into a contact saved by a
    # different run or a web session (see session.py).
    session_id = f"cli:{uuid.uuid4().hex}"
    logger.info("Starting terminal chat (session=%s).", session_id)

    print(
        f"Recruiter chat — ask about {name}'s background. "
        "Type 'exit' or press Ctrl+D to quit.\n"
    )

    messages: list[Message] = []
    while not shutdown.requested:
        try:
            user_input = input("recruiter> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        messages.append({"role": "user", "content": user_input})

        try:
            with use_session(session_id):
                reply = run_turn(client, system_prompt, messages)
        except anthropic.APIStatusError as exc:
            logger.error("Anthropic API error (HTTP %s): %s", exc.status_code, exc.message)
            print("\n[The assistant is unavailable right now — please try again.]\n")
            messages.pop()  # drop the user turn so retrying doesn't duplicate it
            continue
        except anthropic.APIConnectionError:
            logger.error("Could not reach the Anthropic API.", exc_info=True)
            print("\n[Network error — check your connection.]\n")
            messages.pop()
            continue
        except KeyboardInterrupt:
            print()
            break
        except Exception:
            logger.exception("Unexpected error while handling a turn.")
            print("\n[Something went wrong — please try again.]\n")
            messages.pop()
            continue

        if not reply:
            print("\n[No response was generated — please try again.]\n")
            messages.pop()
            continue

        # Keep history as plain text turns (matches the multi-turn pattern
        # in the Claude API docs) — tool calls stay internal to each turn.
        messages.append({"role": "assistant", "content": reply})
        print(f"\nagent> {reply}\n")

    logger.info("Terminal chat finished (session=%s).", session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
