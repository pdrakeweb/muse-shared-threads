#!/usr/bin/env python3
"""Shared Threads - a tiny private chat for a small group and their AI assistant.

Multi-group: each group has its own login (username + password), its own
threads, and its own member names. Groups are fully isolated - a login only
sees its own group's threads.

Two-step auth: group username+password, then a per-member passcode. The
"posting as" identity is the authenticated member - nobody can post as
someone else.

Threads go stale -> the watcher prompts the owner to close them. Closed threads
are read-only (archived, still visible) and excluded from polling until
reopened. Messages can carry photo attachments.

The assistant's display name defaults to "Muse" and can be changed with the
ASSISTANT_NAME environment variable (read by app.py, watch.py and post.py).
"""
import json, os, secrets, sqlite3, time, functools
from datetime import timedelta

from flask import Flask, request, session, redirect, jsonify, Response, g, send_file
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ht.db")
CONFIG = os.path.join(BASE, "config.json")
UPLOADS = os.path.join(BASE, "uploads")
PORT = int(os.environ.get("PORT", "8471"))

ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Muse")

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def load_config():
    """Load config.json, generating secrets on first run."""
    if not os.path.exists(CONFIG):
        cfg = {"secret_key": secrets.token_urlsafe(32),
               "api_key": secrets.token_urlsafe(32)}
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
        os.chmod(CONFIG, 0o600)
        print("generated new config.json (mode 600) - keep a backup somewhere safe")
        return cfg
    with open(CONFIG) as f:
        cfg = json.load(f)
    changed = False
    for key in ("secret_key", "api_key"):
        if not cfg.get(key):
            cfg[key] = secrets.token_urlsafe(32)
            changed = True
    if changed:
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
    return cfg


CONFIG_CACHE = load_config()
app.secret_key = CONFIG_CACHE["secret_key"]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    d = sqlite3.connect(DB)
    d.execute("""CREATE TABLE IF NOT EXISTS threads (
        slug TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL)""")
    d.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL,
        author TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL)""")
    d.execute("""CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, members TEXT NOT NULL,
        created_at REAL NOT NULL)""")
    cols = [r[1] for r in d.execute("PRAGMA table_info(threads)")]
    if "group_id" not in cols:
        d.execute("ALTER TABLE threads ADD COLUMN group_id INTEGER")
    if "summary" not in cols:
        d.execute("ALTER TABLE threads ADD COLUMN summary TEXT")
    if "summary_updated_at" not in cols:
        d.execute("ALTER TABLE threads ADD COLUMN summary_updated_at REAL")
    if "closed" not in cols:
        d.execute("ALTER TABLE threads ADD COLUMN closed INTEGER DEFAULT 0")
    if "closed_at" not in cols:
        d.execute("ALTER TABLE threads ADD COLUMN closed_at REAL")
    if "close_prompt_at" not in cols:
        d.execute("ALTER TABLE threads ADD COLUMN close_prompt_at REAL")
    d.execute("""CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER NOT NULL, name TEXT NOT NULL,
        password_hash TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        PRIMARY KEY (group_id, name))""")
    d.execute("""CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
        thread TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
        size INTEGER NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL)""")
    d.execute("CREATE INDEX IF NOT EXISTS idx_att_msg ON attachments(message_id)")
    # maps threads to the operator's chat for agent notifications (used by watch.py)
    d.execute("""CREATE TABLE IF NOT EXISTS thread_chats (
        thread TEXT PRIMARY KEY, chat_id TEXT NOT NULL)""")
    # one-time migration: groups.members JSON -> group_members rows
    for gid, members_json in d.execute("SELECT id, members FROM groups"):
        try:
            names = json.loads(members_json)
        except ValueError:
            names = []
        for n in names:
            d.execute(
                "INSERT OR IGNORE INTO group_members(group_id,name,password_hash,created_at)"
                " VALUES(?,?,?,?)", (gid, n, "", time.time()))
    d.execute("CREATE INDEX IF NOT EXISTS idx_threads_group ON threads(group_id)")
    d.commit()
    d.close()


def get_group_by_username(username):
    d = db()
    return d.execute("SELECT * FROM groups WHERE username=?", (username,)).fetchone()


def group_members(gid):
    d = db()
    return [r["name"] for r in d.execute(
        "SELECT name FROM group_members WHERE group_id=? ORDER BY rowid", (gid,))]


def thread_in_group(slug, gid):
    d = db()
    return d.execute("SELECT 1 FROM threads WHERE slug=? AND group_id=?",
                     (slug, gid)).fetchone() is not None


def thread_closed(slug):
    d = db()
    r = d.execute("SELECT closed FROM threads WHERE slug=?", (slug,)).fetchone()
    return bool(r and r["closed"])


def message_attachments(d, msg_id):
    return [dict(r) for r in d.execute(
        "SELECT id, filename, mime, size FROM attachments WHERE message_id=? ORDER BY id",
        (msg_id,))]


# ---------- auth ----------

LOGIN_ATTEMPTS = {}  # (ip, username) -> [count, locked_until]


def rate_ok(key):
    now = time.time()
    count, locked = LOGIN_ATTEMPTS.get(key, (0, 0))
    return now >= locked


def rate_fail(key):
    now = time.time()
    count, locked = LOGIN_ATTEMPTS.get(key, (0, 0))
    count += 1
    LOGIN_ATTEMPTS[key] = (count, now + 60 if count >= 5 else 0)


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not (session.get("gid") and session.get("member")):
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect("/")
        return fn(*a, **kw)
    return wrapper


def api_key_ok():
    return request.headers.get("X-API-Key") == CONFIG_CACHE.get("api_key", "")


def api_group():
    slug = request.args.get("group") or ((request.json or {}) if request.is_json else {}).get("group") or "household"
    d = db()
    return d.execute("SELECT * FROM groups WHERE slug=?", (slug,)).fetchone()


# ---------- pages ----------

PAGE_CSS = """body{font-family:system-ui,sans-serif;background:#101418;color:#e8eaed;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1a2027;padding:32px;border-radius:12px;width:320px}
h1{font-size:20px;margin:0 0 16px}label{display:block;font-size:13px;color:#9aa0a6;margin:10px 0 4px}
input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #3c4043;
background:#20262d;color:#e8eaed;font-size:15px}
button{margin-top:18px;width:100%;padding:12px;border:0;border-radius:8px;background:#8ab4f8;
color:#101418;font-size:15px;font-weight:600;cursor:pointer}
.err{color:#f28b82;font-size:13px;margin-top:10px;min-height:18px}
.hint{font-size:12px;color:#9aa0a6;margin-top:12px;line-height:1.5}"""

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title><style>__CSS__</style></head>
<body><div class="card"><h1>Sign in</h1>
<form method="post" action="/login">
<label>Username</label><input name="username" autocomplete="username" autofocus>
<label>Password</label><input name="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button>
<div class="err">__ERR__</div></form></div></body></html>""".replace("__CSS__", PAGE_CSS)

MEMBER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Who is signing in?</title><style>__CSS__</style></head>
<body><div class="card"><h1>__GROUP__</h1>
<form method="post" action="/pick-member">
<label>I am</label><select name="member">__OPTIONS__</select>
<label>Passcode</label><input name="passcode" type="password" autocomplete="current-password" autofocus>
<button type="submit">Continue</button>
<div class="err">__ERR__</div>
<div class="hint">Your passcode proves which member you are, so nobody can post as you.</div>
</form></div></body></html>""".replace("__CSS__", PAGE_CSS)

APP_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__GROUP_NAME__</title>
<style>*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#101418;color:#e8eaed;
margin:0;display:flex;height:100vh}#threads{width:280px;min-width:220px;background:#171c22;
border-right:1px solid #2a3138;display:flex;flex-direction:column;overflow:hidden}
#threads h2{font-size:14px;color:#9aa0a6;padding:14px 14px 6px;margin:0;text-transform:uppercase;
letter-spacing:.5px}#gname{font-size:15px;font-weight:700;padding:12px 14px 0}
#list{overflow-y:auto;flex:1}.th{padding:12px 14px;cursor:pointer;
border-bottom:1px solid #22282f}.th:hover{background:#1f262e}.th.active{background:#26303a}
.th .t{font-size:14px;font-weight:600}.th .p{font-size:12px;color:#9aa0a6;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.th.arch{opacity:.55}.th.arch .t::after{content:" (archived)";font-weight:400;color:#9aa0a6;font-size:12px}
.sect{font-size:11px;color:#5f6368;padding:10px 14px 2px;text-transform:uppercase;letter-spacing:.5px}
#newth{margin:10px;padding:10px;border-radius:8px;border:1px dashed #3c4043;background:none;
color:#8ab4f8;font-size:13px;cursor:pointer}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#who{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid #2a3138;
font-size:13px;color:#9aa0a6}
#who a{color:#8ab4f8;text-decoration:none}
#banner{display:none;padding:10px 16px;background:#3a2b12;color:#fdd663;font-size:13px;
border-bottom:1px solid #2a3138}#banner button{margin-left:12px;background:#fdd663;color:#101418;
border:0;border-radius:6px;padding:4px 10px;font-size:12px;font-weight:600;cursor:pointer}
#msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.m{max-width:75%;padding:10px 12px;border-radius:12px;font-size:14px;line-height:1.45;
white-space:pre-wrap;word-wrap:break-word}.m .a{font-size:11px;color:#9aa0a6;margin-bottom:4px}
.m.me{align-self:flex-end;background:#2b4a6f}.m.them{align-self:flex-start;background:#232a32}
.m.asst{align-self:flex-start;background:#1d2b22;border:1px solid #2c4636}
.m img.att{display:block;max-width:240px;border-radius:8px;margin-top:8px;cursor:pointer}
.m .file{display:block;margin-top:8px;font-size:12px;color:#8ab4f8}
#box{display:flex;gap:8px;padding:12px 16px;border-top:1px solid #2a3138;align-items:flex-end}
#box textarea{flex:1;background:#20262d;color:#e8eaed;border:1px solid #3c4043;border-radius:8px;
padding:10px;font-size:14px;font-family:inherit;resize:none;height:44px}
#box button{background:#8ab4f8;color:#101418;border:0;border-radius:8px;padding:0 18px;
font-size:14px;font-weight:600;cursor:pointer;height:44px}
#clip{background:#20262d!important;color:#e8eaed!important;border:1px solid #3c4043!important;
padding:0 12px!important;font-size:18px!important}
@media(max-width:700px){#threads{width:38vw;min-width:150px}.m{max-width:88%}}</style></head>
<body>
<div id="threads"><div id="gname">__GROUP_NAME__</div><h2>Threads</h2><div id="list"></div>
<button id="newth">+ New thread</button></div>
<div id="main">
<div id="who"><span>Posting as <b>__MEMBER__</b></span>
<span style="flex:1"></span><a href="/pick-member">switch</a><a href="/logout">sign out</a>
<span id="status"></span></div>
<div id="banner"></div>
<div id="msgs"></div>
<div id="box"><button id="clip" title="Attach photo">+</button>
<input type="file" id="file" accept="image/*" multiple style="display:none">
<textarea id="text" placeholder="Write a message"></textarea>
<button id="send">Send</button></div></div>
<script>
let slug=new URLSearchParams(location.search).get('thread');
let lastId=0, timer=null, closed=false;
const me="__MEMBER__";
const ASSISTANT="__ASSISTANT__";
function role(author){return author===ASSISTANT?'asst':(author===me?'me':'them');}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
async function loadThreads(){
  const r=await fetch('/api/threads'); if(!r.ok){location.href='/';return;}
  const ts=await r.json(); const list=document.getElementById('list'); list.innerHTML='';
  const open=ts.filter(t=>!t.closed), arch=ts.filter(t=>t.closed);
  const mk=(t,cls)=>{const d=document.createElement('div');d.className='th '+cls+(t.slug===slug?' active':'');
    d.innerHTML='<div class="t"></div><div class="p"></div>';
    d.querySelector('.t').textContent=t.title; d.querySelector('.p').textContent=t.summary||t.preview||'';
    d.onclick=()=>{slug=t.slug;history.replaceState(null,'','/app?thread='+encodeURIComponent(slug));
      lastId=0;document.getElementById('msgs').innerHTML='';loadThreads();poll();};
    return d;};
  if(open.length){const s=document.createElement('div');s.className='sect';s.textContent='Active';
    list.appendChild(s);open.forEach(t=>list.appendChild(mk(t,'')));}
  if(arch.length){const s=document.createElement('div');s.className='sect';s.textContent='Archived';
    list.appendChild(s);arch.forEach(t=>list.appendChild(mk(t,'arch')));}
  if(!slug&&ts.length){slug=ts[0].slug;
    history.replaceState(null,'','/app?thread='+encodeURIComponent(slug));}
}
function renderMsg(m,box){
  lastId=Math.max(lastId,m.id);
  const d=document.createElement('div');d.className='m '+role(m.author);
  let inner='<div class="a"></div><div class="b"></div>';
  (m.attachments||[]).forEach(a=>{
    inner+='<a class="file" target="_blank" href="/api/attachments/'+a.id+'">'+
      '<img class="att" src="/api/attachments/'+a.id+'" alt="">'+esc(a.filename)+'</a>';});
  d.innerHTML=inner;
  d.querySelector('.a').textContent=m.author+' - '+new Date(m.created_at*1000).toLocaleString();
  d.querySelector('.b').textContent=m.text;
  box.appendChild(d);
}
async function poll(){
  if(!slug)return;
  const r=await fetch('/api/threads/'+encodeURIComponent(slug)+'/messages?after='+lastId);
  if(!r.ok)return; const data=await r.json(); const box=document.getElementById('msgs');
  closed=!!data.closed;
  const banner=document.getElementById('banner'), bx=document.getElementById('box');
  if(closed){banner.style.display='block';
    banner.innerHTML='Archived - read only. <button id="reopen">Reopen thread</button>';
    document.getElementById('reopen').onclick=reopenThread; bx.style.display='none';}
  else{banner.style.display='none';bx.style.display='flex';}
  let n=0; data.messages.forEach(m=>{renderMsg(m,box);n++;});
  if(n)box.scrollTop=box.scrollHeight;
}
async function reopenThread(){
  const r=await fetch('/api/threads/'+encodeURIComponent(slug)+'/reopen',{method:'POST'});
  if(r.ok){lastId=0;document.getElementById('msgs').innerHTML='';poll();loadThreads();}
}
async function send(){
  const ta=document.getElementById('text'); const text=ta.value.trim(); if(!text||!slug||closed)return;
  const r=await fetch('/api/threads/'+encodeURIComponent(slug)+'/messages',
    {method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({text})});
  if(r.ok){ta.value='';poll();loadThreads();}
}
document.getElementById('send').onclick=send;
document.getElementById('text').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}};
document.getElementById('clip').onclick=()=>document.getElementById('file').click();
document.getElementById('file').onchange=async e=>{
  const files=e.target.files; if(!files.length||!slug||closed)return;
  const fd=new FormData();
  for(const f of files)fd.append('files',f);
  fd.append('caption',document.getElementById('text').value.trim());
  const r=await fetch('/api/threads/'+encodeURIComponent(slug)+'/attachments',
    {method:'POST',body:fd});
  e.target.value='';
  if(r.ok){document.getElementById('text').value='';poll();loadThreads();}
  else{const j=await r.json().catch(()=>({}));alert(j.error||'Upload failed');}
};
document.getElementById('newth').onclick=async()=>{
  const title=prompt('Thread title:'); if(!title)return;
  const r=await fetch('/api/threads',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title})});
  if(r.ok){const t=await r.json();slug=t.slug;
    history.replaceState(null,'','/app?thread='+encodeURIComponent(slug));
    lastId=0;document.getElementById('msgs').innerHTML='';loadThreads();poll();}};
loadThreads().then(()=>{poll();timer=setInterval(poll,15000);});
</script></body></html>"""


def group_auth_group():
    """Group context for the member step: pre_gid (just authed) or gid (switching)."""
    gid = session.get("pre_gid") or session.get("gid")
    if not gid:
        return None
    return db().execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()


@app.route("/")
def index():
    if session.get("gid") and session.get("member"):
        return redirect("/app")
    if session.get("pre_gid") or (session.get("gid") and not session.get("member")):
        return redirect("/pick-member")
    err = request.args.get("err", "")
    return LOGIN_HTML.replace("__ERR__", err)


@app.route("/login", methods=["POST"])
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    username = request.form.get("username", "").strip()
    key = (ip, username)
    if not rate_ok(key):
        return redirect("/?err=Too+many+attempts,+try+again+in+a+minute")
    grp = get_group_by_username(username)
    if grp and check_password_hash(grp["password_hash"], request.form.get("password", "")):
        session.clear()
        session["pre_gid"] = grp["id"]
        session["pre_gname"] = grp["name"]
        session.permanent = True
        LOGIN_ATTEMPTS.pop(key, None)
        return redirect("/pick-member")
    rate_fail(key)
    return redirect("/?err=Wrong+username+or+password")


@app.route("/pick-member", methods=["GET"])
def pick_member_page():
    grp = group_auth_group()
    if not grp:
        return redirect("/")
    err = request.args.get("err", "")
    opts = "".join("<option>%s</option>" % m for m in group_members(grp["id"]))
    html = MEMBER_HTML.replace("__GROUP__", grp["name"])
    html = html.replace("__OPTIONS__", opts).replace("__ERR__", err)
    return Response(html, mimetype="text/html")


@app.route("/pick-member", methods=["POST"])
def pick_member():
    grp = group_auth_group()
    if not grp:
        return redirect("/")
    member = request.form.get("member", "").strip()
    passcode = request.form.get("password", "") or request.form.get("passcode", "")
    row = db().execute(
        "SELECT password_hash FROM group_members WHERE group_id=? AND name=?",
        (grp["id"], member)).fetchone()
    if row and row["password_hash"] and check_password_hash(row["password_hash"], passcode):
        session.clear()
        session["gid"] = grp["id"]
        session["gslug"] = grp["slug"]
        session["gname"] = grp["name"]
        session["member"] = member
        session.permanent = True
        return redirect("/app")
    return redirect("/pick-member?err=Wrong+passcode")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/app")
@login_required
def app_page():
    html = APP_HTML.replace("__GROUP_NAME__", session.get("gname", "Thread"))
    html = html.replace("__MEMBER__", session.get("member", ""))
    html = html.replace("__ASSISTANT__", ASSISTANT_NAME.replace('"', ""))
    return Response(html, mimetype="text/html")


# ---------- api ----------

def slugify(d, title):
    for _ in range(5):
        s = "".join(c.lower() if c.isalnum() else "-" for c in title)
        s = "-".join(p for p in s.split("-") if p)
        slug = (s[:48] or "thread") + "-" + secrets.token_hex(2)
        if not d.execute("SELECT 1 FROM threads WHERE slug=?", (slug,)).fetchone():
            return slug
    raise RuntimeError("slug collision")


@app.route("/api/threads")
@login_required
def list_threads():
    d = db()
    rows = d.execute(
        """SELECT t.slug,t.title,t.summary,t.closed,MAX(m.created_at) mc,
           (SELECT text FROM messages WHERE thread=t.slug ORDER BY id DESC LIMIT 1) preview
           FROM threads t LEFT JOIN messages m ON m.thread=t.slug
           WHERE t.group_id=? GROUP BY t.slug
           ORDER BY t.closed ASC, mc DESC NULLS LAST, t.created_at DESC""",
        (session["gid"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/threads", methods=["POST"])
@login_required
def create_thread():
    title = (request.json or {}).get("title", "").strip()[:120] or "Untitled"
    d = db()
    slug = slugify(d, title)
    d.execute("INSERT INTO threads(slug,title,created_at,group_id) VALUES(?,?,?,?)",
              (slug, title, time.time(), session["gid"]))
    d.commit()
    return jsonify({"slug": slug, "title": title})


@app.route("/api/threads/<slug>/messages")
@login_required
def get_messages(slug):
    if not thread_in_group(slug, session["gid"]):
        return jsonify({"error": "no such thread"}), 404
    after = int(request.args.get("after", 0))
    d = db()
    closed = thread_closed(slug)
    rows = d.execute(
        "SELECT id,author,text,created_at FROM messages WHERE thread=? AND id>? ORDER BY id",
        (slug, after)).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m["attachments"] = message_attachments(d, r["id"])
        out.append(m)
    return jsonify({"closed": closed, "messages": out})


def _post_message_record(d, slug, author, text):
    cur = d.execute(
        "INSERT INTO messages(thread,author,text,created_at) VALUES(?,?,?,?)",
        (slug, author, text[:4000], time.time()))
    return cur.lastrowid


def _store_uploads(d, slug, msg_id, files):
    os.makedirs(os.path.join(UPLOADS, slug), exist_ok=True)
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        mime = f.mimetype or ""
        if mime not in IMAGE_MIMES:
            raise ValueError("only photos are accepted (got %s)" % (mime or "unknown type"))
        data = f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("photo too large (12 MB max)")
        if len(data) == 0:
            continue
        name = secrets.token_hex(8) + "_" + secure_filename(f.filename)
        path = os.path.join(UPLOADS, slug, name)
        with open(path, "wb") as fh:
            fh.write(data)
        cur = d.execute(
            "INSERT INTO attachments(message_id,thread,filename,mime,size,path,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (msg_id, slug, secure_filename(f.filename), mime, len(data), path, time.time()))
        saved.append({"id": cur.lastrowid, "filename": f.filename, "mime": mime})
    return saved


@app.route("/api/threads/<slug>/messages", methods=["POST"])
def post_message(slug):
    body = request.json or {}
    if api_key_ok():
        grp = api_group()
        if not grp:
            return jsonify({"error": "no such group"}), 404
        gid, author = grp["id"], ASSISTANT_NAME
    elif session.get("gid") and session.get("member"):
        gid, author = session["gid"], session["member"]
    else:
        return jsonify({"error": "login required"}), 401
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    if not thread_in_group(slug, gid):
        return jsonify({"error": "no such thread"}), 404
    if thread_closed(slug):
        return jsonify({"error": "thread is archived"}), 403
    d = db()
    msg_id = _post_message_record(d, slug, author, text)
    d.commit()
    return jsonify({"id": msg_id})


@app.route("/api/threads/<slug>/attachments", methods=["POST"])
@login_required
def post_attachments(slug):
    if not thread_in_group(slug, session["gid"]):
        return jsonify({"error": "no such thread"}), 404
    if thread_closed(slug):
        return jsonify({"error": "thread is archived"}), 403
    files = request.files.getlist("files")
    if not files or all(not f.filename for f in files):
        return jsonify({"error": "no files"}), 400
    caption = (request.form.get("caption") or "").strip()
    d = db()
    try:
        msg_id = _post_message_record(d, slug, session["member"], caption)
        saved = _store_uploads(d, slug, msg_id, files)
        if not saved:
            d.rollback()
            return jsonify({"error": "no valid photos"}), 400
        d.commit()
    except ValueError as e:
        d.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": msg_id, "attachments": saved})


@app.route("/api/attachments/<int:aid>")
@login_required
def get_attachment(aid):
    d = db()
    r = d.execute(
        """SELECT a.path, a.mime, a.filename FROM attachments a
           JOIN threads t ON t.slug = a.thread
           WHERE a.id=? AND t.group_id=?""",
        (aid, session["gid"])).fetchone()
    if not r or not os.path.exists(r["path"]):
        return jsonify({"error": "not found"}), 404
    return send_file(r["path"], mimetype=r["mime"], download_name=r["filename"])


@app.route("/api/threads/<slug>/close", methods=["POST"])
@login_required
def close_thread_route(slug):
    if not thread_in_group(slug, session["gid"]):
        return jsonify({"error": "no such thread"}), 404
    d = db()
    d.execute("UPDATE threads SET closed=1, closed_at=? WHERE slug=?",
              (time.time(), slug))
    d.commit()
    return jsonify({"ok": True})


@app.route("/api/threads/<slug>/reopen", methods=["POST"])
@login_required
def reopen_thread_route(slug):
    if not thread_in_group(slug, session["gid"]):
        return jsonify({"error": "no such thread"}), 404
    d = db()
    d.execute("UPDATE threads SET closed=0, closed_at=NULL, close_prompt_at=NULL WHERE slug=?",
              (slug,))
    d.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=PORT)
