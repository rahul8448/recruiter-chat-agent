"""Graceful shutdown handling shared by the entry points.

Why this exists: cloud platforms stop a container by sending SIGTERM and
then SIGKILL a short grace period later. Python's default SIGTERM action is
to die immediately — no cleanup, no `finally` blocks, no chance to close
the HTTP server. That's how you get a half-finished write or a dropped
in-flight request during a routine deploy.

Installing a handler converts SIGTERM into an orderly stop: run the
registered cleanup callbacks, then exit. (The storage layer writes
atomically, so even an ungraceful kill can't corrupt the store — this is
about closing the server cleanly and leaving a clear log line, not about
data integrity, which is handled a layer down.)
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

_SIGNAL_NAMES = {
    signal.SIGINT: "SIGINT",
    signal.SIGTERM: "SIGTERM",
}

DEFAULT_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)


class GracefulShutdown:
    """Turns SIGINT/SIGTERM into cleanup callbacks plus a `requested` flag."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._previous: dict[int, object] = {}

    @property
    def requested(self) -> bool:
        """True once a shutdown signal has been received."""
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until shutdown is requested (or `timeout` elapses)."""
        return self._event.wait(timeout)

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        """Register cleanup to run when a shutdown signal arrives.

        Callbacks run in registration order, and one raising doesn't stop
        the rest — a failure to clean up shouldn't block the shutdown.
        """
        self._callbacks.append(callback)

    def install(self, signals: Iterable[signal.Signals] = DEFAULT_SIGNALS) -> None:
        """Install handlers for the given signals (SIGINT and SIGTERM by
        default).

        Pass a narrower set when something else already handles a signal
        well: Gradio's blocking server loop, for instance, relies on the
        default SIGINT behavior of raising KeyboardInterrupt to break out
        and close cleanly, so web.py installs SIGTERM only rather than
        breaking Ctrl+C.

        Signal handlers can only be set from the main thread; if that's not
        where we are, this logs and does nothing rather than raising, since
        losing graceful shutdown shouldn't take down the app.
        """
        for sig in signals:
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError) as exc:
                logger.debug(
                    "Could not install a %s handler: %s",
                    _SIGNAL_NAMES.get(sig, sig),
                    exc,
                )

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        name = _SIGNAL_NAMES.get(signum, str(signum))
        if self._event.is_set():
            # A second signal means "I really mean it" — stop immediately
            # rather than waiting on cleanup that's evidently stuck.
            logger.warning("Received %s again — exiting immediately.", name)
            raise SystemExit(130)

        logger.info("Received %s — shutting down gracefully.", name)
        self._event.set()
        self._run_callbacks()

    def _run_callbacks(self) -> None:
        for callback in self._callbacks:
            try:
                callback()
            except Exception:
                logger.exception("A shutdown callback failed; continuing.")
