"""Tests for the failure-handling behavior that keeps the app serving.

The theme: a failure in a non-essential part (one tool, a push
notification, a cleanup callback) must degrade gracefully rather than take
down the conversation or lose a recruiter's details.
"""

from __future__ import annotations

import signal

import pytest

from recruiter_chat_agent import notify, tools
from recruiter_chat_agent.errors import ProfileError, StorageError
from recruiter_chat_agent.lifecycle import GracefulShutdown
from recruiter_chat_agent.session import use_session

# --- Tool guard -------------------------------------------------------------


def test_tool_returns_error_string_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside the SDK's tool runner aborts the whole turn, so
    tools convert failures into a result the model can talk about.
    """

    def boom(*_args, **_kwargs):
        raise ProfileError("profile.yaml is missing")

    monkeypatch.setattr(tools, "load_profile", boom)

    result = tools.get_summary.func()
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "profile.yaml is missing" in result


def test_tool_hides_unexpected_error_details_from_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our own errors are operator-actionable and safe to relay; an
    unexpected one may carry internals, so it gets a generic message.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("connection string: postgres://user:hunter2@internal-db")

    monkeypatch.setattr(tools, "load_profile", boom)

    result = tools.get_summary.func()
    assert "hunter2" not in result
    assert "system error" in result


def test_storage_failure_surfaces_through_leave_contact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args, **_kwargs):
        raise StorageError("disk is full")

    monkeypatch.setattr(tools, "transaction", boom)

    result = tools.leave_contact_request.func(
        name="Jane", email="jane@example.com", phone="555-1111", message="hi"
    )
    assert result.startswith("Error:")
    assert "disk is full" in result


def test_notification_failure_does_not_fail_the_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contact is already committed by the time we notify — a push
    failure must not make a successful save look like a failure.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("ntfy is down")

    monkeypatch.setattr(tools, "notify_new_contact", boom)

    with use_session("session-1"):
        tools.leave_contact_request.func(
            name="Jane", email="jane@example.com", phone="555-1111", message="hi"
        )

    # The transaction commits before the notification is attempted, so the
    # record must be on disk even though notifying blew up.
    assert len(tools.load_inquiries()) == 1


# --- Notifications ----------------------------------------------------------


def test_notify_is_skipped_when_no_topic_configured() -> None:
    assert notify.notify_new_contact({"name": "Jane"}, is_update=False) is False


def test_notify_rejects_an_invalid_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "some-topic")
    monkeypatch.setenv("NTFY_SERVER", "not-a-url")

    assert notify.notify_new_contact({"name": "Jane"}, is_update=False) is False


def test_notify_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "some-topic")

    def boom(*_args, **_kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)

    # Returns False rather than propagating.
    assert notify.notify_new_contact({"name": "Jane"}, is_update=False) is False


def test_notify_sanitizes_control_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "some-topic")
    sent: dict[str, bytes] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def capture(request, timeout=None):  # noqa: ARG001
        sent["data"] = request.data
        return FakeResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", capture)

    notify.notify_new_contact(
        {
            "name": "Evil\x1b[31mName\x07",
            "emails": ["e@example.com"],
            "messages": [{"text": "line\x00break"}],
        },
        is_update=False,
    )

    payload = sent["data"].decode("utf-8")
    assert "\\u001b" not in payload and "\x1b" not in payload
    assert "\\u0000" not in payload and "\x00" not in payload
    assert "not verified" in payload  # the anti-phishing disclaimer


# --- Graceful shutdown ------------------------------------------------------


def test_shutdown_runs_callbacks_and_sets_flag() -> None:
    shutdown = GracefulShutdown()
    calls: list[str] = []
    shutdown.on_shutdown(lambda: calls.append("closed"))

    assert shutdown.requested is False
    shutdown._handle(signal.SIGTERM, None)

    assert shutdown.requested is True
    assert calls == ["closed"]


def test_one_failing_shutdown_callback_does_not_block_the_others() -> None:
    shutdown = GracefulShutdown()
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("cleanup failed")

    shutdown.on_shutdown(boom)
    shutdown.on_shutdown(lambda: calls.append("second"))

    shutdown._handle(signal.SIGTERM, None)
    assert calls == ["second"]


def test_second_signal_exits_immediately() -> None:
    """A repeated signal means "I really mean it" — don't wait on cleanup
    that's evidently stuck.
    """
    shutdown = GracefulShutdown()
    shutdown._handle(signal.SIGTERM, None)

    with pytest.raises(SystemExit):
        shutdown._handle(signal.SIGTERM, None)


def test_install_is_safe_to_call() -> None:
    """Installing and restoring handlers shouldn't explode in a test run."""
    original = signal.getsignal(signal.SIGTERM)
    try:
        shutdown = GracefulShutdown()
        shutdown.install(signals=(signal.SIGTERM,))
        assert signal.getsignal(signal.SIGTERM) is not original
    finally:
        signal.signal(signal.SIGTERM, original)
