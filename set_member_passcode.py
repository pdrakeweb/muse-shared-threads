#!/usr/bin/env python3
"""Set a member's posting passcode. The passcode is never printed.

  set_member_passcode.py --group household --member Kelly
"""
import argparse, getpass, os, sqlite3, sys
from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--group", required=True)
    p.add_argument("--member", required=True)
    a = p.parse_args()
    d = sqlite3.connect(DB)
    grp = d.execute("SELECT id FROM groups WHERE slug=?", (a.group,)).fetchone()
    if not grp:
        sys.exit("no such group")
    row = d.execute("SELECT 1 FROM group_members WHERE group_id=? AND name=?",
                    (grp[0], a.member)).fetchone()
    if not row:
        sys.exit("no such member in group")
    pw = getpass.getpass("New passcode for %s: " % a.member)
    if len(pw) < 4:
        sys.exit("passcode too short (4+ characters)")
    d.execute("UPDATE group_members SET password_hash=? WHERE group_id=? AND name=?",
              (generate_password_hash(pw), grp[0], a.member))
    d.commit()
    print("passcode set for %s" % a.member)


main()
