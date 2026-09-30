# Shared Threads

A tiny private chat for a small group and their AI assistant. Think of it as a
shared decision log: the humans talk in threads, and an AI assistant watches
the threads, surfaces what's new, answers when mentioned, and archives threads
that go quiet.

Built for a family (two parents deciding things like credit-card renewals),
but it works for any small group that wants a private place to decide things
with an assistant in the loop.

## How it works

Three pieces:

1. **`app.py`** — a small Flask app (SQLite, no build step). Threads with
   messages, photo attachments, per-member login, and archiving. Serves a
   mobile-friendly web UI and a JSON API.
2. **`watch.py`** — an adaptive poller. Run it every minute; it decides
   per-thread how often to actually check (every 45s when a thread is active,
   5 min / 15 min as it idles) and prints a JSON array of events. Usually `[]`.
3. **Your agent** — a scheduled agent (cron, systemd timer, whatever you use)
   that runs `watch.py`, reads the events, and acts on them: notify you of new
   messages, reply in-thread to `@mentions`, create a chat for new threads,
   and prompt you to archive stale ones. The event schema is documented below
   so you can wire any agent to it.

```
humans <--> app.py <--> ht.db <--> watch.py --> JSON events --> your agent
                                                    |
                              your agent posts back via post.py / API
```

## Quick start

```bash
./setup.sh
# 1. create a group (prints the slug; password goes to .password.<slug>, mode 600)
./venv/bin/python add_group.py --name "Family" --username family --members "Pete,Kelly"
# 2. give each member a posting passcode (never printed; hand it to them privately)
./venv/bin/python set_member_passcode.py --group family --member Pete
./venv/bin/python set_member_passcode.py --group family --member Kelly
# 3. run it (127.0.0.1:8471 by default; PORT env overrides)
./venv/bin/python app.py
```

Open `http://127.0.0.1:8471`, sign in with the group username/password, pick
your name, enter your passcode. Create a thread with **+ New thread**.

If your assistant isn't called "Muse", set `ASSISTANT_NAME` before running
anything:

```bash
export ASSISTANT_NAME=Aria   # used by app.py, watch.py and post.py
```

## Auth model

Two steps, on purpose:

1. **Group login** — username + password. Isolates groups completely; a login
   only ever sees its own group's threads (cross-group access 404s).
2. **Member passcode** — proves *which* member you are. The posting identity
   is forced to the authenticated member, so nobody can post as someone else.
   (There used to be a "posting as" picker. It was a lie. It's gone.)

Sessions last 30 days. Login attempts are rate-limited (5/min per IP+user).
The assistant posts through an API key (`X-API-Key` header, in `config.json`).

## Photo attachments

Images only (jpeg, png, webp, gif, heic), 12 MB max. Stored under
`uploads/<thread-slug>/`, served at `/api/attachments/<id>` (login + group
check). The web UI has an attach (+) button with inline thumbnails;
`post.py --image photo.jpg` (repeatable) attaches from the command line.

## Archiving

Threads go stale; stale threads are noise. `watch.py` emits a `close_prompt`
event after 7 idle days. Closing a thread (`close_thread.py --thread <slug>`,
or the API/UI) makes it read-only, lists it under **Archived**, and drops it
from watcher polling. `reopen_thread.py --thread <slug>` (or the Reopen button)
brings it back. Nothing is ever deleted.

## Agent wiring

Run `watch.py` every minute from your scheduler:

```bash
./venv/bin/python watch.py
```

It prints a JSON array of events:

```json
[
  {"type": "message", "group": "family", "thread": "card-renewals",
   "title": "Card renewals", "author": "Kelly", "text": "@Muse thoughts?",
   "created_at": 1759250000.0, "mentions_assistant": true,
   "attachments": [{"id": 3, "filename": "bill.jpg", "mime": "image/jpeg",
                    "path": "uploads/card-renewals/abc123_bill.jpg"}]},
  {"type": "close_prompt", "group": "family", "thread": "old-thread",
   "title": "Old thread", "days_idle": 9},
  {"type": "new_thread", "group": "family", "thread": "new-idea",
   "title": "New idea"}
]
```

Event semantics:

- **`message`** — a human posted. `mentions_assistant` is true when the text
  contains `@YourAssistantName`. Post back with
  `post.py --thread <slug> --text "..."` (authored as the assistant) or
  `--author <member>` to post as a member (e.g. relaying the operator's words).
  Watermarks in `.watch_state` keep every event firing exactly once.
- **`close_prompt`** — idle 7+ days. Ask the operator whether to archive;
  `close_thread.py --thread <slug>` closes, `--snooze 14` defers the prompt.
  Fires once per idle spell.
- **`new_thread`** — a thread with no mapped chat yet. Create whatever
  companion surface you use (side chat, channel, etc.) and record it in the
  `thread_chats(thread, chat_id)` table.

Suggested agent prompt (adapt the chat/notify parts to your platform):

> Run watch.py. If no events, stay silent. For each `message`, notify the
> operator in that thread's companion chat ("Author: text" + thread link).
> If `mentions_assistant`, read the thread's latest messages and reply
> **in the thread** via post.py.
>
> **Privacy rule (hard requirement):** replies posted into a shared thread
> may be grounded ONLY in that thread's own message history plus general
> knowledge. NEVER use the operator's private memories, files, other chats,
> or health/financial details — even if the question invites it. If a
> question can't be answered from the thread alone, say so and offer to check
> with the operator privately. Re-read the draft before posting and confirm
> every claim traces to the thread or general knowledge.
>
> For `close_prompt`, ask the operator whether to archive the thread.
> For `new_thread`, create the companion chat and record it in `thread_chats`.

Thread link format: `<base>/app?thread=<slug>`, where `<base>` is wherever
you expose the app.

## Exposing it

The app listens on localhost by default. Pick your exposure:

- **Tailscale** — simplest for family/friends already on your tailnet.
- **Cloudflare Tunnel** — for a public hostname without opening ports.
- **Reverse proxy** (Caddy/nginx) — if you have a server.

It speaks plain HTTP; terminate TLS at your proxy/tunnel. Nothing in the app
depends on a particular provider.

## CLI reference

| Script | Purpose |
|---|---|
| `app.py` | The web app + API (`PORT` env, default 8471) |
| `add_group.py --name N --username U --members "A,B"` | Create a group (password → `.password.<slug>`) |
| `set_member_passcode.py --group S --member M` | Set/reset a member's posting passcode |
| `post.py --thread S --text "..." [--image f.jpg]` | Post as the assistant (or `--author` a member) |
| `watch.py [--group S]` | Print new events as JSON (run every minute) |
| `close_thread.py --thread S [--snooze N]` | Archive a thread, or snooze its close prompt N days |
| `reopen_thread.py --thread S` | Reopen an archived thread |
| `summarize.py` | Deterministic ≤140-char thread summaries for the sidebar (no LLM, no network) |

## API

All `/api/*` routes need the session cookie, except where noted.

- `GET /api/threads` → list (with `closed` flag, `summary`, `preview`)
- `POST /api/threads` `{"title"}` → `{"slug", "title"}`
- `GET /api/threads/<slug>/messages?after=<id>` → `{"closed", "messages": [...]}`
  (messages carry `attachments`)
- `POST /api/threads/<slug>/messages` `{"text"}` → `{"id"}`
  (also accepts `X-API-Key` header instead of session; authors as assistant)
- `POST /api/threads/<slug>/attachments` (multipart `files[]` + `caption`)
- `GET /api/attachments/<id>` → the photo
- `POST /api/threads/<slug>/close` / `/reopen`

Closed threads return 403 on post/upload.

## Security notes

- Passwords/passcodes are stored as hashes (Werkzeug). Plaintext group
  passwords live only in `.password.<slug>` (mode 600) at creation time —
  hand them to members privately, then treat the file as backup.
- Member passcodes are never printed, not even at creation.
- `config.json` (session secret + API key) is mode 600 and gitignored.
- Uploads are limited to images, 12 MB, served only to the owning group.
- The `@mention` privacy rule above is the load-bearing wall between the
  operator's private context and the shared thread. Keep it.

## Files

```
app.py                 web app + API + UI
watch.py               adaptive event poller (JSON to stdout)
post.py                post as the assistant (or a member) from scripts/agents
add_group.py           create a group
set_member_passcode.py set a member's passcode
close_thread.py        archive a thread / snooze its close prompt
reopen_thread.py       reopen an archived thread
summarize.py           deterministic sidebar summaries
setup.sh               one-time setup (venv, deps, database)
requirements.txt
```

Everything else in a working install (`ht.db`, `uploads/`, `.watch_state`,
`.password.*`, `config.json`) is local state and never committed.

## License

Apache 2.0 — see [LICENSE](LICENSE).
