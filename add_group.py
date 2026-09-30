#!/usr/bin/env python3
"""Create a new group: separate login, threads, and member names.

The generated password is written to .password.<slug> (mode 600) and NOT
printed. Run after the app has started once (it creates the groups table).
"""
import argparse, json, os, re, secrets, sqlite3, sys, time
from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")
WORDS = ["harbor", "cedar", "lumen", "maple", "north", "quiet", "ridge",
         "sail", "stone", "timber", "willow", "ember", "brook", "field"]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True, help="display name, e.g. 'Book club'")
    p.add_argument("--username", required=True, help="login username for the group")
    p.add_argument("--members", required=True,
                   help="comma-separated poster names, e.g. 'Pete,Sam'")
    p.add_argument("--slug", default=None, help="url slug (default: from name)")
    a = p.parse_args()
    slug = a.slug or re.sub(r"[^a-z0-9]+", "-", a.name.lower()).strip("-") or "group"
    members = [m.strip() for m in a.members.split(",") if m.strip()]
    if not members:
        sys.exit("need at least one member name")
    pw = "-".join(secrets.choice(WORDS) for _ in range(4)) + f"-{secrets.randbelow(90)+10}"
    d = sqlite3.connect(DB)
    try:
        cur = d.execute(
            "INSERT INTO groups(slug,name,username,password_hash,members,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (slug, a.name, a.username, generate_password_hash(pw),
             json.dumps(members), time.time()))
        gid = cur.lastrowid
        for m in members:
            d.execute(
                "INSERT INTO group_members(group_id,name,password_hash,created_at)"
                " VALUES(?,?,?,?)", (gid, m, "", time.time()))
        d.commit()
    except sqlite3.IntegrityError as e:
        sys.exit("group not created: %s" % e)
    pw_file = os.path.join(BASE, ".password." + slug)
    with open(pw_file, "w") as f:
        f.write(pw + "\n")
    os.chmod(pw_file, 0o600)
    print("group '%s' created: slug=%s username=%s members=%s" %
          (a.name, slug, a.username, ", ".join(members)))
    print("password written to .password.%s (mode 600, not shown here)" % slug)
    print("next: set each member's posting passcode with set_member_passcode.py"
          " --group %s --member <name>" % slug)

main()
