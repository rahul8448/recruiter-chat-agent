"""Loading and validating the candidate profile (data/profile.yaml).

Split out of tools.py so that "read the YAML, check it's sane, cache it"
lives apart from "expose facts to the model as tools". The tools stay thin
and this owns every way the file can be wrong — missing, unreadable,
malformed YAML, or valid YAML of entirely the wrong shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .errors import ProfileError

logger = logging.getLogger(__name__)

# Cached per resolved path, so repeated tool calls in one conversation
# don't re-read and re-parse the file on every single question.
_cache: dict[Path, dict[str, Any]] = {}

PLACEHOLDER_PREFIX = "TODO"


def load_profile(settings: Settings | None = None) -> dict[str, Any]:
    """Load (and cache) the candidate profile.

    Raises ProfileError with an actionable message for every way the file
    can be unusable — entry points turn that into one clear line instead of
    a traceback.
    """
    settings = settings or Settings.from_env()
    path = settings.profile_path

    cached = _cache.get(path)
    if cached is not None:
        return cached

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError(
            f"No profile found at {path}. Copy the template and fill it in, or set "
            "PROFILE_PATH to point at your own file."
        ) from exc
    except OSError as exc:
        raise ProfileError(f"Could not read the profile at {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path} is not valid YAML: {exc}") from exc

    if data is None:
        raise ProfileError(f"{path} is empty — it needs at least a `candidate:` section.")
    if not isinstance(data, dict):
        raise ProfileError(
            f"{path} should be a YAML mapping, found {type(data).__name__}."
        )
    if not isinstance(data.get("candidate"), dict):
        raise ProfileError(
            f"{path} is missing a `candidate:` section (with at least a `name:`)."
        )
    if not str(data["candidate"].get("name", "")).strip():
        raise ProfileError(f"{path} is missing `candidate.name`.")

    _cache[path] = data
    logger.debug("Loaded profile from %s", path)
    return data


def clear_cache() -> None:
    """Drop the cached profile — mainly for tests and for picking up an
    edit to profile.yaml without restarting.
    """
    _cache.clear()


def candidate_name(settings: Settings | None = None) -> str:
    """The candidate's name, as used in prompts and UI copy."""
    return str(load_profile(settings)["candidate"]["name"]).strip()


def has_placeholder_values(settings: Settings | None = None) -> bool:
    """Whether the profile still contains unfilled `TODO:` template values.

    Not an error — the app runs fine — but worth warning about before
    someone points a recruiter at it.
    """
    return _contains_placeholder(load_profile(settings))


def _contains_placeholder(node: Any) -> bool:
    if isinstance(node, str):
        return node.lstrip().startswith(PLACEHOLDER_PREFIX)
    if isinstance(node, dict):
        return any(_contains_placeholder(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_placeholder(v) for v in node)
    return False
