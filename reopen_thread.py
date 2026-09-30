#!/usr/bin/env python3
"""Reopen an archived thread: posting and watcher polling resume.

  reopen_thread.py --thread <slug> [--group household]
"""
import argparse, os, sqlite3, sys

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--thread", required=True)
    p.add_argument("--group", default="household")
    a = p.parse_args()
    d = sqlite3.connect(DB)
    grp = d.execute("SELECT id FROM groups WHERE slug=?", (a.group,)).fetchone()
    if not grp:
        sys.exit("no such group")
    t = d.execute("SELECT title, closed FROM threads WHERE slug=? AND group_id=?",
                  (a.thread, grp[0])).fetchone()
    if not t:
        sys.exit("no such thread")
    if not t[1]:
        print("'%s' is already open" % t[0])
    else:
        d.execute("UPDATE threads SET closed=0, closed_at=NULL, close_prompt_at=NULL"
                  " WHERE slug=?", (a.thread,))
        d.commit()
        print("reopened '%s' (posting and polling resumed)" % t[0])


main()
