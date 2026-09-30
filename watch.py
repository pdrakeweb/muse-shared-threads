#!/usr/bin/env python3
"""Adaptive thread watcher for shared threads.

Poll cadence follows each thread's activity, so busy threads surface fast
and quiet ones cost almost nothing:

  last activity < 30 min .... poll every 45s
  last activity < 24 h ...... poll every 5m
  last activity < 3 days .... poll every 15m
  idle > 7 days ............ emit one close_prompt event (then 15m until closed)
  closed ................... skipped entirely

Run every minute from your scheduler. Prints a JSON array of
events (usually []):

  {"type":"message","group","thread","title","author","text","created_at",
   "mentions_assistant":bool,"attachments":[{"id","filename","mime","path"}]}
  {"type":"close_prompt","group","thread","title","days_idle"}
  {"type":"new_thread","group","thread","title"}   (no attached chat yet)

Only human authors are surfaced (never the assistant). @mentions of the
assistant are flagged so the operator's agent can route them for a reply.

The assistant's name defaults to "Muse"; set ASSISTANT_NAME to match yours.
It must match the name your agent posts under (see post.py).
"""
import argparse, json, os, re, sqlite3, time

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")
STATE = os.path.join(BASE, ".watch_state")

ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Muse")

# (max age seconds, poll interval seconds)
TIERS = [(30 * 60, 45), (24 * 3600, 5 * 60), (3 * 86400, 15 * 60)]
IDLE_INTERVAL = 15 * 60
PROMPT_AGE = 7 * 86400

MENTION_RE = re.compile(r"@" + re.escape(ASSISTANT_NAME) + r"\b", re.I)


def tier_interval(age):
    for max_age, interval in TIERS:
        if age <= max_age:
            return interval
    return IDLE_INTERVAL


def load_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
        if isinstance(st, dict) and "groups" in st:
            return st
    except (OSError, ValueError):
        pass
    # legacy formats -> migrate into per-thread watermarks
    legacy = {}
    try:
        with open(STATE) as f:
            raw = f.read().strip()
        legacy = json.loads(raw)
        if not isinstance(legacy, dict):
            legacy = {}
    except (OSError, ValueError):
        try:
            legacy = {"household": int(raw)}
        except (ValueError, NameError):
            legacy = {}
    return {"groups": {g: {"last_id": int(v) if isinstance(v, int) else 0}
                       for g, v in legacy.items()}}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(st))
    os.replace(tmp, STATE)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--group", default=None, help="only watch this group slug")
    p.add_argument("--authors", default=None,
                   help="comma-separated author names to surface (default: all members)")
    a = p.parse_args()

    now = time.time()
    st = load_state()
    groups_state = st.setdefault("groups", {})
    d = sqlite3.connect(DB)
    d.row_factory = sqlite3.Row

    if a.group:
        group_rows = d.execute("SELECT * FROM groups WHERE slug=?", (a.group,)).fetchall()
    else:
        group_rows = d.execute("SELECT * FROM groups").fetchall()

    events = []
    for grp in group_rows:
        gid, gslug = grp["id"], grp["slug"]
        members = {r["name"] for r in d.execute(
            "SELECT name FROM group_members WHERE group_id=?", (gid,))}
        if not members:
            try:
                members = set(json.loads(grp["members"]))
            except ValueError:
                members = set()
        if a.authors is not None:
            wanted = {x.strip() for x in a.authors.split(",") if x.strip()}
        else:
            wanted = members

        gs = groups_state.setdefault(gslug, {})
        threads_state = gs.setdefault("threads", {})
        legacy_last = gs.pop("last_id", 0)

        threads = d.execute(
            "SELECT slug,title,created_at,closed,close_prompt_at FROM threads"
            " WHERE group_id=?", (gid,)).fetchall()
        chat_mapped = {r["thread"] for r in d.execute("SELECT thread FROM thread_chats")}

        for t in threads:
            slug = t["slug"]
            if t["closed"]:
                continue
            ts = threads_state.setdefault(
                slug, {"last_id": legacy_last, "last_poll": 0.0,
                       "prompted": 0.0, "chat_seen": False})
            last_msg = d.execute(
                "SELECT MAX(id), MAX(created_at) FROM messages WHERE thread=?",
                (slug,)).fetchone()
            last_id, last_activity = last_msg[0] or 0, last_msg[1]
            activity = last_activity or t["created_at"] or now
            age = now - activity

            is_new = not ts["chat_seen"]
            ts["chat_seen"] = True
            if is_new and slug not in chat_mapped:
                events.append({"type": "new_thread", "group": gslug,
                               "thread": slug, "title": t["title"]})

            interval = tier_interval(age)
            if ts["last_poll"] and now - ts["last_poll"] < interval and not is_new:
                continue
            ts["last_poll"] = now

            rows = d.execute(
                "SELECT id,author,text,created_at FROM messages"
                " WHERE thread=? AND id>? ORDER BY id", (slug, ts["last_id"])).fetchall()
            for r in rows:
                ts["last_id"] = max(ts["last_id"], r["id"])
                if r["author"] == ASSISTANT_NAME or r["author"] not in wanted:
                    continue
                atts = d.execute(
                    "SELECT id,filename,mime,path FROM attachments WHERE message_id=?"
                    " ORDER BY id", (r["id"],)).fetchall()
                events.append({
                    "type": "message", "group": gslug, "thread": slug,
                    "title": t["title"], "author": r["author"], "text": r["text"],
                    "created_at": r["created_at"],
                    "mentions_assistant": bool(MENTION_RE.search(r["text"] or "")),
                    "attachments": [dict(x) for x in atts],
                })

            # stale thread -> prompt Pete to close/archive it (once per idle spell)
            prompt_at = t["close_prompt_at"] or 0
            if age > PROMPT_AGE and activity > ts["prompted"] and now >= prompt_at:
                ts["prompted"] = now
                events.append({"type": "close_prompt", "group": gslug,
                               "thread": slug, "title": t["title"],
                               "days_idle": int(age // 86400)})

    save_state(st)
    print(json.dumps(events))


main()
