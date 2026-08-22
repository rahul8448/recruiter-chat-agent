"""Tests for profile loading and configuration parsing — the two places
where an operator mistake should produce one clear message instead of a
traceback from deep inside a request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recruiter_chat_agent import profile
from recruiter_chat_agent.config import RateLimitSettings, Settings
from recruiter_chat_agent.errors import ConfigurationError, ProfileError

VALID_PROFILE = """
candidate:
  name: "Test Person"
  headline: "Staff Engineer"
skills:
  languages:
    - Python
"""


def _write_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("PROFILE_PATH", str(path))
    profile.clear_cache()
    return path


def test_loads_a_valid_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_profile(tmp_path, monkeypatch, VALID_PROFILE)
    assert profile.candidate_name() == "Test Person"


def test_missing_profile_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILE_PATH", "/nonexistent/profile.yaml")
    profile.clear_cache()

    with pytest.raises(ProfileError, match="No profile found"):
        profile.load_profile()


def test_malformed_yaml_raises_profile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, monkeypatch, "candidate: [unclosed\n  bad: : :")

    with pytest.raises(ProfileError, match="not valid YAML"):
        profile.load_profile()


def test_empty_profile_raises_profile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, monkeypatch, "")

    with pytest.raises(ProfileError, match="empty"):
        profile.load_profile()


def test_profile_without_candidate_section_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, monkeypatch, "skills:\n  languages: [Python]\n")

    with pytest.raises(ProfileError, match="candidate"):
        profile.load_profile()


def test_profile_without_a_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, monkeypatch, "candidate:\n  headline: Engineer\n")

    with pytest.raises(ProfileError, match="candidate.name"):
        profile.load_profile()


def test_placeholder_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_profile(tmp_path, monkeypatch, VALID_PROFILE)
    assert profile.has_placeholder_values() is False

    _write_profile(
        tmp_path,
        monkeypatch,
        'candidate:\n  name: "Test Person"\n  headline: "TODO: fill me in"\n',
    )
    assert profile.has_placeholder_values() is True


def test_profile_is_cached_until_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_profile(tmp_path, monkeypatch, VALID_PROFILE)

    assert profile.candidate_name() == "Test Person"
    path.write_text(VALID_PROFILE.replace("Test Person", "Someone Else"), encoding="utf-8")
    assert profile.candidate_name() == "Test Person"  # still cached

    profile.clear_cache()
    assert profile.candidate_name() == "Someone Else"


# --- Configuration ----------------------------------------------------------


def test_rate_limit_defaults() -> None:
    limits = RateLimitSettings.from_env()
    assert limits.session_max_messages == 20
    assert limits.ip_max_messages == 60


def test_rate_limits_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_RATE_LIMIT_MAX_MESSAGES", "3")
    monkeypatch.setenv("IP_RATE_LIMIT_WINDOW_SECONDS", "42")

    limits = RateLimitSettings.from_env()
    assert limits.session_max_messages == 3
    assert limits.ip_window_seconds == 42


def test_unparseable_int_setting_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_RATE_LIMIT_MAX_MESSAGES", "twenty")

    with pytest.raises(ConfigurationError, match="whole number"):
        RateLimitSettings.from_env()


def test_out_of_range_int_setting_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_RATE_LIMIT_MAX_MESSAGES", "0")

    with pytest.raises(ConfigurationError, match="at least"):
        RateLimitSettings.from_env()


def test_settings_read_optional_features_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "some-topic")
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.example.com/")
    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", "some-key")

    settings = Settings.from_env()
    assert settings.notifications_enabled is True
    assert settings.encryption_enabled is True
    # Trailing slash trimmed so URL joining stays predictable.
    assert settings.ntfy_server == "https://ntfy.example.com"


def test_features_are_off_by_default() -> None:
    settings = Settings.from_env()
    assert settings.notifications_enabled is False
    assert settings.encryption_enabled is False
