#!/usr/bin/env python3
"""Post a message to a thread as the assistant (or a named group member).

The assistant name defaults to "Muse"; set ASSISTANT_NAME to match the name
configured in app.py and watch.py.
"""
import argparse, json, mimetypes, os, secrets, shutil, sqlite3, sys, time
from werkzeug.utils import secure_filename

ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Muse")

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")
UPLOADS = os.path.join(BASE, "uploads")
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--thread", required=True)
    p.add_argument("--text", default="")
    p.add_argument("--group", default="household",
                   help="group slug (default: household)")
    p.add_argument("--author", default=ASSISTANT_NAME,
                   help="assistant name or one of the group's member names")
    p.add_argument("--image", action="append", default=[],
                   help="photo to attach (repeatable)")
    a = p.parse_args()
    if not a.text.strip() and not a.image:
        print("nothing to post: need --text and/or --image", file=sys.stderr)
        sys.exit(1)
    d = sqlite3.connect(DB)
    grp = d.execute("SELECT id, members FROM groups WHERE slug=?", (a.group,)).fetchone()
    if not grp:
        print("no such group", file=sys.stderr); sys.exit(1)
    gid, members = grp[0], json.loads(grp[1])
    if a.author != ASSISTANT_NAME and a.author not in members:
        print("author must be %s or one of: %s" % (ASSISTANT_NAME, ", ".join(members)),
              file=sys.stderr)
        sys.exit(1)
    t = d.execute("SELECT closed FROM threads WHERE slug=? AND group_id=?",
                  (a.thread, gid)).fetchone()
    if not t:
        print("no such thread", file=sys.stderr); sys.exit(1)
    if t[0]:
        print("thread is archived", file=sys.stderr); sys.exit(1)
    cur = d.execute(
        "INSERT INTO messages(thread,author,text,created_at) VALUES(?,?,?,?)",
        (a.thread, a.author, a.text[:4000], time.time()))
    msg_id = cur.lastrowid
    for img in a.image:
        if not os.path.isfile(img):
            print("not a file: %s" % img, file=sys.stderr); sys.exit(1)
        size = os.path.getsize(img)
        if size > MAX_UPLOAD_BYTES:
            print("too large (12 MB max): %s" % img, file=sys.stderr); sys.exit(1)
        mime = mimetypes.guess_type(img)[0] or ""
        if mime not in IMAGE_MIMES:
            print("not a photo: %s" % img, file=sys.stderr); sys.exit(1)
        os.makedirs(os.path.join(UPLOADS, a.thread), exist_ok=True)
        name = secrets.token_hex(8) + "_" + secure_filename(os.path.basename(img))
        path = os.path.join(UPLOADS, a.thread, name)
        shutil.copyfile(img, path)
        d.execute(
            "INSERT INTO attachments(message_id,thread,filename,mime,size,path,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (msg_id, a.thread, os.path.basename(img), mime, size, path, time.time()))
    d.commit()
    print(msg_id)

main()
