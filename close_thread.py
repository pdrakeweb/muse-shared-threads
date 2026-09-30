#!/usr/bin/env python3
"""Close (archive) a thread, or snooze its close prompt.

Closing makes the thread read-only in the app and drops it from watcher
polling. It stays visible under Archived. Reopen with reopen_thread.py.

  close_thread.py --thread <slug> [--group household]
  close_thread.py --thread <slug> --snooze 14   (prompt again in 14 days, stays open)
"""
import argparse, os, sqlite3, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--thread", required=True)
    p.add_argument("--group", default="household")
    p.add_argument("--snooze", type=int, default=0,
                   help="days to snooze the close prompt (without closing)")
    a = p.parse_args()
    d = sqlite3.connect(DB)
    grp = d.execute("SELECT id FROM groups WHERE slug=?", (a.group,)).fetchone()
    if not grp:
        sys.exit("no such group")
    t = d.execute("SELECT title, closed FROM threads WHERE slug=? AND group_id=?",
                  (a.thread, grp[0])).fetchone()
    if not t:
        sys.exit("no such thread")
    if a.snooze:
        d.execute("UPDATE threads SET close_prompt_at=? WHERE slug=?",
                  (time.time() + a.snooze * 86400, a.thread))
        print("close prompt for '%s' snoozed %d days" % (t[0], a.snooze))
    else:
        if t[1]:
            print("'%s' is already closed" % t[0])
        else:
            d.execute("UPDATE threads SET closed=1, closed_at=? WHERE slug=?",
                      (time.time(), a.thread))
            print("closed '%s' (read-only, polling stopped)" % t[0])
    d.commit()


main()
