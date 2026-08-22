"""Entry point for a hosted deployment.

Most platforms look for `app.py` at the repo root, run it, and serve
whatever it launches. Everything of substance lives in the package — this
file only bridges the two, so a hosted app and a local
`recruiter-chat-web` run share exactly the same construction path
(`web.create_app`).

Bind address and port come from GRADIO_SERVER_NAME / GRADIO_SERVER_PORT,
which Gradio reads directly; most hosts set a $PORT you should map to the
latter. The rest is configured with ordinary environment variables:

  ANTHROPIC_API_KEY         (secret, required)
  NTFY_TOPIC                (secret, strongly recommended — see below)
  INQUIRIES_ENCRYPTION_KEY  (secret, optional)

**Most free tiers have an ephemeral filesystem**: `inquiries.json` is wiped
whenever the app restarts, sleeps, or is rebuilt. Set NTFY_TOPIC so each
recruiter's details are pushed to your phone the moment they arrive, rather
than existing only in a file that won't survive the next restart. Where a
persistent volume is available, point INQUIRIES_PATH at it instead.
"""

import sys
from pathlib import Path

# The package uses a src/ layout, which isn't importable from the repo root
# on its own. Hosts that install requirements.txt don't install this
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
        "ANTHROPIC_API_KEY is not set. Add it to this deployment's environment "
        "variables / secrets and restart. (NTFY_TOPIC and "
        "INQUIRIES_ENCRYPTION_KEY are optional but recommended.)"
    )
    raise SystemExit(1)

try:
    demo, launch_kwargs = create_app()
except RecruiterChatError as exc:
    # By far the most common first-deploy failure is a secret that hasn't
    # been set yet. Say so in terms that match where it's actually
    # configured — a Space has no .env file to point someone at.
    log.error(
        "%s\n\nSet this in the deployment's environment variables "
        "(ANTHROPIC_API_KEY is required; NTFY_TOPIC and "
        "INQUIRIES_ENCRYPTION_KEY are optional).",
        exc,
    )
    raise SystemExit(1) from exc

# No inbrowser=True — there's no browser on the server. Host and port
# are left to Gradio to read from GRADIO_SERVER_NAME /
# GRADIO_SERVER_PORT, which the platform sets.
demo.launch(**launch_kwargs)
