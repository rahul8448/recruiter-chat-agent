---
title: Recruiter Chat Agent
emoji: 💼
colorFrom: indigo
colorTo: purple
sdk: gradio
sdk_version: 6.25.0
app_file: app.py
pinned: false
short_description: An agentic chat assistant that answers recruiters' questions
---

# recruiter-chat-agent

An agentic chat — terminal or browser — that talks to recruiters about your
skills, work history, and projects, grounded in your own profile data, not
the model's guesswork.

Claude answers recruiter questions by calling tools that read from
[`src/recruiter_chat_agent/data/profile.yaml`](src/recruiter_chat_agent/data/profile.yaml),
so it can't invent experience you haven't listed, and updating your
background is just editing that one YAML file.

## Setup

1. **Fill in your profile.** Edit
   [`src/recruiter_chat_agent/data/profile.yaml`](src/recruiter_chat_agent/data/profile.yaml)
   and replace every `TODO: ...` placeholder with your real name, skills,
   experience, projects, education, and contact info.

2. **Add credentials.** Either:
   ```bash
   cp .env.example .env
   # then put your key in .env: ANTHROPIC_API_KEY=sk-ant-...
   ```
   or run `ant auth login` if you use the Anthropic CLI — the agent picks up
   either automatically.

3. **Run it — terminal:**
   ```bash
   uv run recruiter-chat
   ```
   **or browser (Gradio):**
   ```bash
   uv run recruiter-chat-web
   ```
   auto-opens a browser tab at `http://127.0.0.1:7860`. On a headless/remote
   machine with no display, the tab open will just fail silently — open the
   `Running on local URL: ...` link printed in the terminal instead. It's a
   normal Gradio app, so the usual env vars work too — e.g.
   `GRADIO_SERVER_PORT=8080` to change the port, or set `demo.launch(share=True)`
   in `web.py` for a temporary public link.

## How it works

**Entry points**

- `agent.py` — the terminal chat loop, plus the shared agent logic
  (`run_turn`, `build_client`) both front ends call. Each recruiter message
  is sent to Claude (`claude-opus-5`) along with a system prompt instructing
  it to speak on your behalf, in the third person, using only tool-provided
  facts.
- `web.py` — the same agent logic behind a Gradio `ChatInterface`.
- `inquiries.py` — the `recruiter-chat-inquiries` report.

**Domain**

- `tools.py` — the tools Claude calls: `get_summary`, `list_skills`,
  `get_experience`, `get_projects`, `get_education`, `get_certifications`,
  `get_work_authorization`, and `get_contact_info` read the profile;
  `leave_contact_request` records a recruiter's details.
- `context.py` — the agent's role, scope, and guardrails (what it must
  refuse or redirect).
- `profile.py` — loads and validates `data/profile.yaml`.
- `storage.py` — the inquiries store: serialized read-modify-write, atomic
  writes, optional encryption.
- `notify.py` — optional ntfy.sh push when a recruiter leaves their info.
- `session.py` — scopes `leave_contact_request`'s record matching to the
  current conversation, so one visitor can't touch another's saved details.
- `sanitize.py` — control-character stripping and length clamping for
  untrusted text.
- `ratelimit.py` — the sliding-window limiter behind `web.py`'s abuse
  protection.

**Infrastructure**

- `config.py` — every environment variable, in one place, validated.
- `logging_setup.py` — logging configuration for the entry points.
- `lifecycle.py` — graceful shutdown on SIGINT/SIGTERM.
- `errors.py` — the exception types the app raises deliberately.
- `data/profile.yaml` — your background. The only file most people need to
  touch.

## Running in production

**Configuration.** Everything is environment-driven (see `.env.example` for
the full list with defaults). The ones that matter for a deployment:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required, unless using an `ant auth login` profile |
| `INQUIRIES_PATH` | Point the store at a **mounted volume** — the default lives inside the installed package, which is ephemeral in a container |
| `PROFILE_PATH` | Override the profile location without rebuilding the image |
| `INQUIRIES_ENCRYPTION_KEY` | Encrypt recruiter PII at rest |
| `NTFY_TOPIC` | Push notification on each new contact request |
| `LOG_LEVEL` | `DEBUG` / `INFO` (default) / `WARNING` / `ERROR` |
| `SESSION_*` / `IP_RATE_LIMIT_*` | Abuse protection thresholds |
| `RECRUITER_CHAT_AUTH` | Optional login wall; off by default |

**Logging.** Structured, timestamped, to stderr. Log lines deliberately
record *what happened* (a contact was saved, which session, how many
records) and never a recruiter's name, email, phone, or message text —
logs usually end up somewhere less protected than the encrypted store
those details live in. Keep it that way when adding log lines.

**Graceful shutdown.** SIGTERM (what a cloud platform sends on deploy) and
SIGINT both close the web server cleanly instead of killing the process
mid-request. A second signal exits immediately, so a stuck shutdown can
still be forced.

**Durability.** Writes to the inquiries store are atomic (temp file +
`os.replace`) and the read-modify-write cycle is serialized with a lock, so
concurrent submissions can't lose records and an interrupted write can't
corrupt the file. An unreadable store raises rather than looking empty —
which would otherwise let the next write destroy real contact details.

**Failure behavior.** A failing tool returns an error string the model can
explain rather than aborting the turn; a failed push notification never
turns a successful save into a failure; a misconfiguration produces one
actionable log line and a non-zero exit rather than a traceback.

**One caveat:** the store is a JSON file guarded by an in-process lock, so
this runs correctly as a **single instance**. Multiple replicas would need a
real database — the lock doesn't coordinate across processes.

## Access control & abuse protection (recruiter-chat-web)

**No login wall by default** — the point is for recruiters to open the link
and start chatting, so `recruiter-chat-web` doesn't require signing in.
Instead, every message is rate limited on two tiers before it's ever sent to
the API:

- **Per session** — a generous cap on one conversation (default 20
  messages / 10 min). A real recruiter chatting normally will never hit it.
- **Per IP** — a broader cap across sessions from the same origin (default
  60 messages / 10 min), since a page reload gets a brand-new session and
  would otherwise dodge the per-session limit.

Both are on by default with no setup — override via `SESSION_RATE_LIMIT_*`
/ `IP_RATE_LIMIT_*` in `.env` if you need to (see `.env.example`). A
rejected message never reaches the Anthropic API, so it doesn't cost
anything.

If you specifically want a login wall anyway (e.g. only sharing this with a
handful of recruiters directly), set `RECRUITER_CHAT_AUTH=user:pass` — see
`.env.example`.

## Recruiter contact requests

If a recruiter wants you to reach back out, the agent takes down their name,
email, and phone number — all required, it'll ask for whichever is missing —
and calls `leave_contact_request` to save it. A message is required too, but
if the recruiter doesn't state a reason for reaching out, the agent writes a
short one itself from the conversation (role, company, why they're
interested) rather than leaving it blank.

Entries match on **email or phone overlap within the same conversation**, so
follow-up info from the same recruiter later in that chat — "here's an
alternative email", "actually my number is now X", "update that message" —
lands on their existing entry instead of creating a duplicate:

- A recruiter can have more than one email or phone on file (comma-separated
  in the tool call). New values are **added** to what's stored by default —
  an alternate email doesn't erase the first one.
- Correcting a value (not adding a second one) replaces it instead — the
  agent uses `replace_email` / `replace_phone` when the recruiter says
  something like "actually my number is now...".
- A new message is appended as a note by default; `replace_message` fixes
  the most recent note in place if the recruiter asks for a correction.

Matching is scoped to the conversation that created the entry (see
`session.py`) — a *different* conversation, even one claiming the exact same
email, always creates a new record rather than merging. This is deliberate:
without it, anyone who merely knew a real recruiter's email could submit a
message from an unrelated chat that silently overwrote that recruiter's
stored phone number or appended a fabricated note onto their record. The
tradeoff is that the same recruiter returning in a brand-new session (a
different day, a fresh tab) creates a second entry rather than merging into
their first — reviewing `inquiries.json`, you can still recognize repeat
visitors by matching email yourself.

Stored in `src/recruiter_chat_agent/data/inquiries.json` — created on first
use, gitignored since it holds other people's contact details, permission-
restricted to your user (`chmod 600`) on every write, and optionally
encrypted at rest if you set `INQUIRIES_ENCRYPTION_KEY` (see
`.env.example`).

Notifications sent via `notify.py` (see below) strip control characters,
cap length, and always carry an "unverified, submitted by a chat visitor"
disclaimer — the note text ultimately traces back to something a chat
visitor typed, so treat anything urgent or link-containing in a push
notification with the same skepticism as an email from a stranger.

View what's been recorded:

```bash
uv run recruiter-chat-inquiries
```

### Get notified immediately (optional)

Since this checks-the-file approach doesn't help much once the app is
deployed somewhere you're not actively watching, `leave_contact_request` can
also fire a push notification via [ntfy.sh](https://ntfy.sh) — free, no
account, works identically whether the app runs locally or in the cloud
(it's just an outbound HTTPS call).

1. Pick a private, hard-to-guess topic name — anyone who knows an ntfy.sh
   topic name can read or publish to it, since public topics aren't
   access-controlled. Something like `rahul-recruiter-abc123xyz` works.
2. Install the [ntfy app](https://ntfy.sh/app) (iOS/Android/web) and
   subscribe to that topic — or just watch
   `https://ntfy.sh/<your-topic>` in a browser tab.
3. Set `NTFY_TOPIC=<your-topic>` in `.env` (locally) or as a secret in
   whatever cloud platform you deploy to.

That's it — leave `NTFY_TOPIC` unset and this is a no-op; the contact
request still saves normally either way, and a failed push (e.g. ntfy.sh is
unreachable) is logged to stderr rather than breaking the save. See
[`notify.py`](src/recruiter_chat_agent/notify.py).

## Testing

```bash
uv run pytest
```

- `test_leave_contact_request.py` — validation, session-scoped record
  matching, merge/replace behavior, control-character stripping.
- `test_storage.py` — the durability guarantees: atomic writes, concurrent
  writers not losing records, encryption round-trips, and failing loud on
  an unreadable store.
- `test_profile_and_config.py` — every way the profile or an env var can be
  wrong produces a clear error rather than a traceback.
- `test_resilience.py` — a failing tool, a failing notification, or a
  failing shutdown callback each degrade gracefully.

Every test runs against isolated temp paths (`tests/conftest.py`'s
`isolated_store` fixture, autouse) — the suite never reads or writes your
real `data/` files.

## Extending

- Add a new field to `profile.yaml` and a matching tool in `tools.py` (decorate
  it with `@beta_tool`, add a docstring — that's the whole schema) — then add
  it to `ALL_TOOLS`.
- Want a resume PDF attached? Point a tool at a file and return its text, or
  use the Claude API's Files/document support.
- Want a different web framework than Gradio? `web.py` is a thin wrapper —
  swap the `gr.ChatInterface` for e.g. a small FastAPI app and call
  `run_turn(client, system_prompt, messages)` from `agent.py` the same way;
  the agent logic itself doesn't change.

## Project layout

```
recruiter-chat-agent/
├── pyproject.toml
├── .env.example
├── tests/
│   ├── conftest.py                   # isolated_store fixture (autouse)
│   ├── test_leave_contact_request.py
│   ├── test_storage.py
│   ├── test_profile_and_config.py
│   └── test_resilience.py
└── src/recruiter_chat_agent/
    ├── agent.py        # shared agent logic + terminal chat loop
    ├── web.py           # Gradio browser chat UI
    ├── inquiries.py      # `recruiter-chat-inquiries` viewer
    │
    ├── tools.py           # tools Claude calls
    ├── context.py          # role, scope, and guardrails
    ├── profile.py           # loads + validates profile.yaml
    ├── storage.py            # inquiries store (atomic, locked, encryptable)
    ├── notify.py              # optional ntfy.sh push
    ├── session.py              # per-conversation scoping
    ├── sanitize.py              # untrusted-text cleaning
    ├── ratelimit.py              # session/IP rate limiting
    │
    ├── config.py                  # all env vars, validated
    ├── logging_setup.py            # logging configuration
    ├── lifecycle.py                 # graceful SIGINT/SIGTERM shutdown
    ├── errors.py                     # exception types
    │
    ├── static/
    │   └── style.css                   # Gradio UI theme/styling
    └── data/
        └── profile.yaml                 # <- edit this with your real info
```
