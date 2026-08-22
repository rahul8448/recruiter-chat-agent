"""Hugging Face Spaces entry point.

Spaces looks for `app.py` at the repo root, runs it, and serves whatever
Gradio app it launches. Everything of substance lives in the package —
this file only bridges the two, so the hosted app and a local
`recruiter-chat-web` run share exactly the same construction path
(`web.create_app`).

Configuration comes from the Space's *Settings → Variables and secrets*,
which Spaces exposes as ordinary environment variables:

  ANTHROPIC_API_KEY         (secret, required)
  NTFY_TOPIC                (secret, strongly recommended — see below)
  INQUIRIES_ENCRYPTION_KEY  (secret, optional)

**On free Spaces the filesystem is ephemeral**: `inquiries.json` is wiped
whenever the Space restarts, sleeps, or is rebuilt. Set NTFY_TOPIC so each
recruiter's details are pushed to your phone the moment they arrive, rather
than existing only in a file that won't survive the next restart. With
persistent storage attached, also set INQUIRIES_PATH=/data/inquiries.json.
"""

import sys
from pathlib import Path

# The package uses a src/ layout, which isn't importable from the repo root
# on its own. Spaces installs requirements.txt but doesn't install this
# project itself, so put src/ on the path rather than requiring a build step.
sys.path.insert(0, str(Path(__file__).parent / "src"))

import logging  # noqa: E402

from recruiter_chat_agent.config import has_api_credentials  # noqa: E402
from recruiter_chat_agent.errors import RecruiterChatError  # noqa: E402
from recruiter_chat_agent.web import create_app  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# Unlike a local run, a Space has no `ant auth login` profile to fall back
# on — the secret is the only way in. Fail immediately with a pointer to
# where it's set, rather than starting a UI where every message errors.
if not has_api_credentials():
    log.error(
        "ANTHROPIC_API_KEY is not set. On Hugging Face Spaces, add it under "
        "Settings -> Variables and secrets, then restart the Space. "
        "(NTFY_TOPIC and INQUIRIES_ENCRYPTION_KEY are optional but recommended.)"
    )
    raise SystemExit(1)

try:
    demo, launch_kwargs = create_app()
except RecruiterChatError as exc:
    # By far the most common first-deploy failure is a secret that hasn't
    # been set yet. Say so in terms that match where it's actually
    # configured — a Space has no .env file to point someone at.
    log.error(
        "%s\n\nOn Hugging Face Spaces, set this under "
        "Settings -> Variables and secrets (ANTHROPIC_API_KEY is required; "
        "NTFY_TOPIC and INQUIRIES_ENCRYPTION_KEY are optional).",
        exc,
    )
    raise SystemExit(1) from exc

# No inbrowser=True — there's no browser on the server. Spaces sets
# GRADIO_SERVER_NAME / GRADIO_SERVER_PORT itself, so host and port are
# left to Gradio to pick up from the environment.
demo.launch(**launch_kwargs)
