"""Exception types for this package.

Having our own types (rather than letting raw OSError / yaml.YAMLError /
json.JSONDecodeError escape) lets callers distinguish "the operator
misconfigured something" from "a genuine runtime failure", and lets entry
points print one clear, actionable message instead of a raw traceback.
"""

from __future__ import annotations


class RecruiterChatError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigurationError(RecruiterChatError):
    """Something the operator set (or failed to set) is wrong — a bad env
    var, a missing credential. Fatal at startup, and always actionable by
    the person running the app.
    """


class ProfileError(RecruiterChatError):
    """data/profile.yaml is missing, unreadable, or not the shape we expect.
    Fatal: with no profile there is nothing for the agent to talk about.
    """


class StorageError(RecruiterChatError):
    """Reading or writing the inquiries store failed in a way we could not
    safely recover from. Callers should surface this rather than silently
    dropping a recruiter's contact details.
    """
