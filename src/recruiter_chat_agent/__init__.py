"""An agentic chat assistant that talks to recruiters about a candidate's
skills, experience, and projects — grounded in data/profile.yaml.
"""

from .errors import (
    ConfigurationError,
    ProfileError,
    RecruiterChatError,
    StorageError,
)

__all__ = [
    "ConfigurationError",
    "ProfileError",
    "RecruiterChatError",
    "StorageError",
    "main",
]

__version__ = "0.1.0"


def main() -> int:
    """Convenience alias for the terminal chat entry point.

    Imported lazily so `import recruiter_chat_agent` stays cheap and
    doesn't pull in the Anthropic SDK for callers that only want the
    exception types.
    """
    from .agent import main as _main

    return _main()
