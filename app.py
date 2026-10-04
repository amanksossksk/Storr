"""
One-time Email Pool (Flask)

1) Dashboard      GET  /
2) Input API      POST /api/input    -> add email(s)
3) Output API     GET|POST /api/output -> take 1 email (each email is given only ONCE)

Run:
    pip install flask
    python app.py

Optional protection:
    API_KEY=mysecret python app.py
    then send header  X-API-Key: mysecret   (or ?key=mysecret)
"""
import os
import re
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request

DB_PATH = os.environ.get("DB_PATH", "emails.db")
API_KEY = os.environ.get("API_KEY", "")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)


def db():
    # isolation_level=None -> we control transactions manually
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS emails (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   email TEXT NOT NULL UNIQUE,
                   used INTEGER NOT NULL DEFAULT 0,
                   added_at TEXT NOT NULL,
                   used_at TEXT
               )"""
        )


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.before_request
def check_key():
    if not API_KEY:
        return
    key = request.headers.get("X-API-Key") or request.args.get("key")
    if key != API_KEY:
        return jsonify(error="unauthorized"), 401


# ---------------------------------------------------------------- INPUT API
@app.post("/api/input")
def api_input():
    """Body (JSON): {"email": "a@b.com"}  or  {"emails": ["a@b.com", ...]}
    Plain text body (one email per line / comma separated) also works."""
    data = request.get_json(silent=True)
    raw = []
    if isinstance(data, dict):
        if "email" in data:
            raw.append(str(data["email"]))
        if isinstance(data.get("emails"), list):
            raw += [str(x) for x in data["emails"]]
    elif isinstance(data, list):
        raw = [str(x) for x in data]
    else:
        raw = re.split(r"[\s,;]+", request.get_data(as_text=True))

    emails = [e.strip().lower() for e in raw if e.strip()]
    if not emails:
        return jsonify(error="no email provided"), 400

    added, duplicates, invalid = [], [], []
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for e in emails:
            if not EMAIL_RE.match(e):
                invalid.append(e)
                continue
            try:
                conn.execute(
                    "INSERT INTO emails (email, added_at) VALUES (?, ?)", (e, now())
                )
                added.append(e)
            except sqlite3.IntegrityError:
                duplicates.append(e)
        conn.execute("COMMIT")
    finally:
        conn.close()

    return jsonify(added=added, duplicates=duplicates, invalid=invalid), (
        201 if added else 200
    )


# --------------------------------------------------------------- OUTPUT API
@app.route("/api/output", methods=["GET", "POST"])
def api_output():
    """Gives out the oldest unused email and marks it as used (atomic).
    Optional ?count=N to take several at once."""
    try:
        count = max(1, min(int(request.args.get("count", 1)), 100))
    except ValueError:
        count = 1

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")  # lock -> two callers can never get the same email
        rows = conn.execute(
            "SELECT id, email FROM emails WHERE used = 0 ORDER BY id LIMIT ?", (count,)
        ).fetchall()
        if not rows:
            conn.execute("ROLLBACK")
            return jsonify(error="no email available"), 404
        ts = now()
        conn.executemany(
            "UPDATE emails SET used = 1, used_at = ? WHERE id = ?",
            [(ts, r["id"]) for r in rows],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    emails = [r["email"] for r in rows]
    if count == 1:
        return jsonify(email=emails[0])
    return jsonify(emails=emails)


# ------------------------------------------------------------ DASHBOARD DATA
@app.get("/api/stats")
def api_stats():
    conn = db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        used = conn.execute("SELECT COUNT(*) FROM emails WHERE used=1").fetchone()[0]
        rows = conn.execute(
            "SELECT email, used, added_at, used_at FROM emails ORDER BY id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return jsonify(
        total=total,
        used=used,
        available=total - used,
        rows=[dict(r) for r in rows],
    )


# ---------------------------------------------------------------- DASHBOARD
PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Email Pool</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}
 .wrap{max-width:760px;margin:auto;padding:16px}
 .cards{display:flex;gap:10px}
 .card{flex:1;background:#fff;border-radius:10px;padding:14px;text-align:center;box-shadow:0 1px 3px #0001}
 .card b{display:block;font-size:26px}
 .box{background:#fff;border-radius:10px;padding:14px;margin-top:14px;box-shadow:0 1px 3px #0001}
 textarea{width:100%;box-sizing:border-box;height:90px;padding:8px;border:1px solid #ccc;border-radius:6px}
 button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:9px 14px;margin-top:8px;cursor:pointer}
 button.alt{background:#16a34a}
 table{width:100%;border-collapse:collapse;font-size:14px}
 td,th{padding:6px;border-bottom:1px solid #eee;text-align:left;word-break:break-all}
 .used{color:#b91c1c}.free{color:#15803d}
 #msg{margin-top:8px;font-size:14px;white-space:pre-wrap}
</style></head><body><div class="wrap">
<h2>Email Pool</h2>
<div class="cards">
 <div class="card"><b id="total">0</b>Total</div>
 <div class="card"><b id="avail">0</b>Available</div>
 <div class="card"><b id="used">0</b>Used</div>
</div>
<div class="box">
 <b>Add emails</b> (one per line)
 <textarea id="emails" placeholder="a@example.com&#10;b@example.com"></textarea>
 <button onclick="addEmails()">Add</button>
 <button class="alt" onclick="takeOne()">Take 1 email</button>
 <div id="msg"></div>
</div>
<div class="box">
 <b>Latest 200</b>
 <table><thead><tr><th>Email</th><th>Status</th><th>Used at</th></tr></thead>
 <tbody id="rows"></tbody></table>
</div>
</div>
<script>
const KEY = new URLSearchParams(location.search).get("key") || "";
const H = {"Content-Type":"application/json","X-API-Key":KEY};
const $ = id => document.getElementById(id);
async function load(){
  const r = await fetch("/api/stats",{headers:H}); const d = await r.json();
  $("total").textContent=d.total; $("avail").textContent=d.available; $("used").textContent=d.used;
  $("rows").innerHTML = d.rows.map(x=>
    `<tr><td>${x.email}</td><td class="${x.used?'used':'free'}">${x.used?'used':'available'}</td><td>${x.used_at||''}</td></tr>`
  ).join("");
}
async function addEmails(){
  const list = $("emails").value.split(/[\\s,;]+/).filter(Boolean);
  const r = await fetch("/api/input",{method:"POST",headers:H,body:JSON.stringify({emails:list})});
  const d = await r.json();
  $("msg").textContent = r.ok ? `Added: ${d.added.length} | Duplicate: ${d.duplicates.length} | Invalid: ${d.invalid.length}` : (d.error||"error");
  if(r.ok) $("emails").value=""; load();
}
async function takeOne(){
  const r = await fetch("/api/output",{headers:H}); const d = await r.json();
  $("msg").textContent = r.ok ? "Your email: "+d.email : (d.error||"error"); load();
}
load(); setInterval(load,5000);
</script></body></html>"""


@app.get("/")
def dashboard():
    return render_template_string(PAGE)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
