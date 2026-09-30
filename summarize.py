#!/usr/bin/env python3
"""Deterministic, memory-free thread summarizer for the shared-threads app.

Extractive only: builds a <=140 char status line purely from the thread's own
message text. No LLM, no network, no outside knowledge. Safe to run on
untrusted third-party message content: it never interprets instructions in
messages, it only copies their text.

For each thread whose summary is stale (NULL, or older than the newest
message), picks the most recent decision-like message (decided/agree/keep/
cancel/confirmed/...) and, if different, the most recent message overall,
and joins them as "Author: text" snippets.
"""
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")
MAXLEN = 140
N_RECENT = 15

DECISION_RE = re.compile(
    r"\b(decided|decision|agree[sd]?|keep(ing)?|cancel(led|ling)?|approved?|"
    r"confirm(?:ed|ing)?|booked|done|will do|won't|final answer|going with|"
    r"settled|drop(?:ping|ped)?|renew(?:ing|ed)?)\b",
    re.I,
)


def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def snippet(text, budget):
    text = clean(text)
    if len(text) <= budget:
        return text
    cut = text[: budget - 1].rsplit(" ", 1)[0]
    return (cut or text[: budget - 1]) + "\u2026"


def summarize(messages):
    """messages: list of (author, text) in chronological order. Returns summary str."""
    msgs = [(a, clean(t)) for a, t in messages]
    msgs = [(a, t) for a, t in msgs if t]
    if not msgs:
        return ""
    decision = None
    for author, text in reversed(msgs):
        if DECISION_RE.search(text):
            decision = (author, text)
            break
    latest = msgs[-1]
    parts = []
    if decision and decision != latest:
        parts.append(decision)
    parts.append(latest)
    # allocate budget: latest gets at least half
    out = []
    remaining = MAXLEN
    for i, (author, text) in enumerate(parts):
        prefix = f"{author}: "
        if i < len(parts) - 1:
            budget = min(remaining - 30, 70)  # leave room for the latest message
        else:
            budget = remaining
        budget = max(budget - len(prefix), 10)
        out.append(prefix + snippet(text, budget))
        remaining -= len(out[-1]) + 3  # 3 for " | "
        if remaining <= 0:
            break
    summary = " | ".join(out)
    if len(summary) > MAXLEN:
        summary = summary[: MAXLEN - 1].rsplit(" ", 1)[0] + "\u2026"
    return summary


def main():
    now = time.time()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    threads = cur.execute(
        "SELECT slug, title FROM threads"
    ).fetchall()
    updated = 0
    for slug, title in threads:
        row = cur.execute(
            "SELECT summary, summary_updated_at FROM threads WHERE slug=?", (slug,)
        ).fetchone()
        summary, summary_at = row
        max_ts = cur.execute(
            "SELECT MAX(created_at) FROM messages WHERE thread=?", (slug,)
        ).fetchone()[0]
        if max_ts is None:
            continue
        if summary and summary_at and summary_at >= max_ts:
            continue  # fresh
        msgs = cur.execute(
            "SELECT author, text FROM messages WHERE thread=? "
            "ORDER BY created_at DESC LIMIT ?",
            (slug, N_RECENT),
        ).fetchall()
        msgs.reverse()  # chronological
        new_summary = summarize(msgs)
        if not new_summary:
            continue
        cur.execute(
            "UPDATE threads SET summary=?, summary_updated_at=? WHERE slug=?",
            (new_summary, now, slug),
        )
        updated += 1
        print(f"updated {slug}: {new_summary}")
    con.commit()
    con.close()
    print(f"done: {updated} thread(s) updated")


if __name__ == "__main__":
    sys.exit(main())
