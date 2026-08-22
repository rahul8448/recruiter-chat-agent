"""Shared pytest fixtures.

The important one is `isolated_store`: it points the app's storage and
profile paths at per-test temp locations, so nothing in the suite ever
reads or writes the real data/ files.

Because paths and secrets are read from the environment (see config.py),
isolating a test is just a matter of setting env vars — no monkeypatching
of module internals, which used to break whenever those internals moved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recruiter_chat_agent import profile


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the inquiries store to a temp file for every test, and clear
    any encryption/notification config a developer happens to have in their
    own .env so it can't change test behavior.

    autouse=True so isolation is guaranteed even for a test that forgets to
    request it by name — but it can still be requested by name when a test
    needs the path itself.
    """
    store_path = tmp_path / "inquiries.json"
    monkeypatch.setenv("INQUIRIES_PATH", str(store_path))

    # A real key or topic leaking in from the developer's .env would
    # silently change what these tests exercise.
    monkeypatch.delenv("INQUIRIES_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    # The profile is cached per-path; clear it so a test that points
    # PROFILE_PATH somewhere else isn't served a stale parse.
    profile.clear_cache()
    yield store_path
    profile.clear_cache()
