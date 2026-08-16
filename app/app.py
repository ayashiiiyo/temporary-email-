import os
import re
import json
import time
import random
import string
import sqlite3
import email
import threading
import urllib.request
from email.header import decode_header, make_header
from email import policy
from email.utils import parseaddr
from flask import Flask, request, redirect, url_for, session, jsonify, render_template_string
from flask_socketio import SocketIO, join_room, emit

DOMAIN = os.environ.get("DOMAIN", "yourdomain.com")
DB_PATH = os.environ.get("DB_PATH", "/var/lib/tempmail/tempmail.db")
WEBHOOK_SECRET_FILE = os.environ.get("WEBHOOK_SECRET_FILE", "/opt/tempmail/.webhook_secret")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET and os.path.exists(WEBHOOK_SECRET_FILE):
    WEBHOOK_SECRET = open(WEBHOOK_SECRET_FILE).read().strip()
MAILBOX_TTL = int(os.environ.get("MAILBOX_TTL", "600"))
SAFE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

app = Flask(__name__)
app.secret_key = os.environ.get("TEMPMAIL_SECRET", os.urandom(24).hex())
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*", path="/ws/socket.io")
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            localpart TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            localpart TEXT NOT NULL,
            from_addr TEXT,
            to_addr TEXT,
            subject TEXT,
            date TEXT,
            text_body TEXT,
            html_body TEXT,
            size INTEGER,
            received_at REAL NOT NULL,
            is_read INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            email_id TEXT NOT NULL,
            filename TEXT,
            content_type TEXT,
            size INTEGER,
            data BLOB
        );
        CREATE INDEX IF NOT EXISTS idx_emails_local ON emails(localpart, received_at DESC);
        """
    )
    conn.commit()
    conn.close()


def sanitize(local):
    local = (local or "").strip().lower()
    return re.sub(r"[^a-z0-9._-]", "", local)


def valid(local):
    return bool(SAFE_RE.match(local))


def random_local():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def decode_hdr(val):
    try:
        return str(make_header(decode_header(val or "")))
    except Exception:
        return str(val or "")


def clean_expired():
    now = time.time()
    with _db_lock:
        conn = db()
        cur = conn.execute("SELECT localpart FROM mailboxes WHERE expires_at < ?", (now,))
        expired = [r["localpart"] for r in cur.fetchall()]
        for lp in expired:
            conn.execute("DELETE FROM attachments WHERE email_id IN (SELECT id FROM emails WHERE localpart=?)", (lp,))
            conn.execute("DELETE FROM emails WHERE localpart=?", (lp,))
            conn.execute("DELETE FROM mailboxes WHERE localpart=?", (lp,))
        conn.commit()
        conn.close()
    return len(expired)


def touch_mailbox(local):
    now = time.time()
    exp = now + MAILBOX_TTL
    with _db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO mailboxes(localpart,created_at,expires_at) VALUES(?,?,?) ON CONFLICT(localpart) DO UPDATE SET expires_at=?",
            (local, now, exp, exp),
        )
        conn.commit()
        conn.close()
    return exp


def get_mailbox_expiry(local):
    with _db_lock:
        conn = db()
        r = conn.execute("SELECT expires_at FROM mailboxes WHERE localpart=?", (local,)).fetchone()
        conn.close()
    return r["expires_at"] if r else None


def parse_and_store_email(raw, from_addr, to_addr, headers):
    msg = email.message_from_bytes(raw.encode() if isinstance(raw, str) else raw, policy=policy.default)
    to_decoded = decode_hdr(to_addr or msg.get("To", ""))
    _, to_email = parseaddr(to_decoded)
    if "@" in to_email:
        local = to_email.split("@", 1)[0].lower()
    else:
        local = sanitize(to_addr.split("@", 1)[0] if "@" in (to_addr or "") else "")
    if not valid(local):
        return None
    subject = decode_hdr(msg.get("Subject", "(no subject)"))
    frm = decode_hdr(from_addr or msg.get("From", ""))
    date = msg.get("Date", "")
    text_body = ""
    html_body = ""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp or part.get_filename():
                fn = part.get_filename() or "attachment"
                try:
                    payload = part.get_payload(decode=True) or b""
                except Exception:
                    payload = b""
                attachments.append((fn, ct, len(payload), payload))
                continue
            if ct == "text/plain" and not text_body:
                try:
                    text_body = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    text_body = payload.decode("utf-8", "replace") if payload else ""
            elif ct == "text/html" and not html_body:
                try:
                    html_body = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    html_body = payload.decode("utf-8", "replace") if payload else ""
    else:
        try:
            c = msg.get_content()
            if msg.get_content_type() == "text/html":
                html_body = c
            else:
                text_body = c
        except Exception:
            payload = msg.get_payload(decode=True)
            if payload:
                text_body = payload.decode("utf-8", "replace")
    msg_id = "{}.{}.{}".format(int(time.time()), local, random.choices(string.ascii_lowercase + string.digits, k=10))
    msg_id = re.sub(r"[^a-z0-9._-]", "", msg_id)
    now = time.time()
    size = len(raw.encode() if isinstance(raw, str) else raw)
    touch_mailbox(local)
    with _db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO emails(id,localpart,from_addr,to_addr,subject,date,text_body,html_body,size,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (msg_id, local, frm, to_decoded, subject, date, text_body, html_body, size, now),
        )
        for fn, ct, sz, data in attachments:
            aid = "{}.{}".format(int(time.time()), random.choices(string.ascii_lowercase + string.digits, k=8))
            conn.execute(
                "INSERT INTO attachments(id,email_id,filename,content_type,size,data) VALUES(?,?,?,?,?,?)",
                (aid, msg_id, fn, ct, sz, sqlite3.Binary(data)),
            )
        conn.commit()
        conn.close()
    return {"local": local, "id": msg_id, "from": frm, "subject": subject, "date": date}


def list_messages(local):
    clean_expired()
    with _db_lock:
        conn = db()
        rows = conn.execute(
            "SELECT id,from_addr,to_addr,subject,date,received_at,is_read FROM emails WHERE localpart=? ORDER BY received_at DESC",
            (local,),
        ).fetchall()
        conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "from": r["from_addr"] or "",
                "to": r["to_addr"] or "",
                "subject": r["subject"] or "(no subject)",
                "date": r["date"] or "",
                "unread": not r["is_read"],
            }
        )
    return out


def get_message(local, msg_id):
    with _db_lock:
        conn = db()
        r = conn.execute("SELECT * FROM emails WHERE localpart=? AND id=?", (local, msg_id)).fetchone()
        if r:
            conn.execute("UPDATE emails SET is_read=1 WHERE id=?", (msg_id,))
            conn.commit()
        atts = conn.execute("SELECT id,filename,content_type,size FROM attachments WHERE email_id=?", (msg_id,)).fetchall() if r else []
        conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "subject": r["subject"] or "(no subject)",
        "from": r["from_addr"] or "",
        "to": r["to_addr"] or "",
        "date": r["date"] or "",
        "plain": r["text_body"] or "",
        "html": r["html_body"] or "",
        "attachments": [{"id": a["id"], "filename": a["filename"], "content_type": a["content_type"], "size": a["size"]} for a in atts],
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Temp Mail</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Pixelify+Sans:wght@500;600;700&display=swap');
:root{--sky:#f4ecff;--paper:#fffaff;--lavender:#bba8e8;--purple:#71609c;--ink:#403957;--muted:#827995;--pink:#f1a8bd;--blue:#8abed6;--mint:#a9d8c2;--yellow:#f5d58d;--line:#d8cbea;--shadow:#c9b9dc;--danger:#d96f7f}
*{box-sizing:border-box}
html{color-scheme:light}
body{min-height:100vh;margin:0;color:var(--ink);background:linear-gradient(rgba(113,96,156,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(113,96,156,.045) 1px,transparent 1px),var(--sky);background-size:24px 24px;font-family:"DM Sans",sans-serif;overflow-x:hidden}
button,input{font:inherit}
button{color:inherit;cursor:pointer}
[hidden]{display:none!important}
.scene{position:fixed;inset:0;z-index:-1;overflow:hidden;pointer-events:none}
.cloud{position:absolute;opacity:.7;image-rendering:pixelated}
.cloud.one{width:180px;top:14%;left:-35px}
.cloud.two{width:135px;top:8%;right:4%;opacity:.55}
.cloud.three{width:115px;right:15%;bottom:8%;opacity:.45}
.spark{position:absolute;color:#c195c9;font:700 19px/1 "Pixelify Sans",sans-serif;animation:twinkle 2.8s steps(2) infinite}
.spark.s1{top:21%;left:9%}.spark.s2{top:16%;right:18%;animation-delay:.8s}.spark.s3{bottom:18%;left:15%;animation-delay:1.4s}.spark.s4{right:7%;bottom:29%;animation-delay:.35s}
.shell{width:min(960px,calc(100% - 36px));min-height:100vh;margin:0 auto;padding:25px 0;display:grid;grid-template-rows:auto 1fr auto;gap:30px}
header{display:flex;align-items:center;justify-content:space-between;gap:18px}
.brand{display:flex;align-items:center;gap:11px;font:700 19px/1 "Pixelify Sans",sans-serif}
.brand-icon{width:40px;height:40px;display:grid;place-items:center;border:2px solid var(--purple);color:var(--paper);background:var(--lavender);box-shadow:4px 4px 0 var(--shadow);clip-path:polygon(0 7px,7px 7px,7px 0,33px 0,33px 7px,40px 7px,40px 33px,33px 33px,33px 40px,7px 40px,7px 33px,0 33px)}
.brand-icon svg{width:22px;image-rendering:pixelated}
.status{padding:8px 11px;display:flex;align-items:center;gap:8px;border:2px solid var(--line);border-radius:8px;color:var(--muted);background:rgba(255,250,255,.78);font-size:11px;font-weight:700;box-shadow:3px 3px 0 rgba(201,185,220,.55)}
.dot{width:8px;height:8px;background:var(--danger);box-shadow:2px 0 0 rgba(217,111,127,.25),0 2px 0 rgba(217,111,127,.25)}
.dot.online{background:#71bd99;box-shadow:2px 0 0 #c4ead7,0 2px 0 #c4ead7}
main{align-self:center}
.hero{text-align:center;max-width:540px;margin:0 auto}
.quest{width:max-content;margin:0 auto 17px;padding:7px 10px 6px;border:2px solid var(--purple);color:var(--purple);background:#f8dbe5;box-shadow:3px 3px 0 #dec2df;font:700 11px/1 "Pixelify Sans",sans-serif;text-transform:uppercase;letter-spacing:.09em}
h1{margin:0;font:700 clamp(40px,5.5vw,68px)/.91 "Pixelify Sans",sans-serif;letter-spacing:-.045em;text-shadow:4px 4px 0 rgba(187,168,232,.28)}
h1 span{display:block;color:#8875b7}
.lead{max-width:420px;margin:24px auto 0;color:var(--muted);font-size:14px;line-height:1.75}
.window{position:relative;width:100%;max-width:520px;margin:34px auto 0;padding:12px;border:3px solid var(--purple);border-radius:11px;background:var(--paper);box-shadow:9px 9px 0 rgba(113,96,156,.2),13px 13px 0 rgba(255,255,255,.46)}
.window::before{content:"";position:absolute;top:-9px;left:28px;width:8px;height:8px;background:var(--yellow);box-shadow:9px -9px 0 var(--yellow),18px 0 0 var(--yellow),9px 9px 0 var(--yellow);transform:rotate(45deg)}
.window-bar{height:39px;margin:-2px -2px 11px;padding:0 12px;border:2px solid var(--line);border-radius:7px;display:flex;align-items:center;justify-content:space-between;background:#eee5fa}
.window-title{font:700 12px/1 "Pixelify Sans",sans-serif}
.window-dots{display:flex;gap:6px}
.window-dots i{width:8px;height:8px;display:block;background:var(--pink)}
.window-dots i:nth-child(2){background:var(--yellow)}.window-dots i:nth-child(3){background:var(--mint)}
.field-label{display:block;margin:0 0 7px;color:var(--muted);font:700 10px/1 "Pixelify Sans",sans-serif;text-transform:uppercase;letter-spacing:.08em}
.input-row{display:flex;gap:0;margin-bottom:14px}
.input-row input{flex:1;min-width:0;padding:12px 14px;border:2px solid var(--purple);border-radius:7px 0 0 7px;background:#fcf9ff;color:var(--ink);font-size:14px;outline:none}
.input-row input:focus{border-color:var(--purple);box-shadow:inset 0 0 0 1px var(--lavender)}
.input-row .suf{display:flex;align-items:center;padding:0 12px;border:2px solid var(--purple);border-left:0;border-radius:0 7px 7px 0;background:#efe7f8;color:var(--purple);font:700 12px/1 "Pixelify Sans",sans-serif;white-space:nowrap}
.btn{width:100%;min-height:48px;border:2px solid var(--purple);border-radius:7px;display:flex;align-items:center;justify-content:center;gap:8px;color:#fff;background:#8e7abd;box-shadow:0 5px 0 #67568f;font:700 14px/1 "Pixelify Sans",sans-serif;transition:transform .12s,box-shadow .12s}
.btn:hover{transform:translateY(2px);box-shadow:0 3px 0 #67568f}
.btn.ghost{color:var(--purple);background:#f8dbe5;box-shadow:0 5px 0 #dec2df}
.btn.ghost:hover{box-shadow:0 3px 0 #dec2df}
.btn.sm{min-height:38px;font-size:12px;padding:0 14px;width:auto}
.divider-text{display:flex;align-items:center;gap:10px;margin:14px 0;color:var(--muted);font:600 10px/1 "Pixelify Sans",sans-serif;text-transform:uppercase;letter-spacing:.08em}
.divider-text::before,.divider-text::after{content:"";flex:1;height:2px;background:var(--line)}
.chips{display:flex;flex-wrap:wrap;gap:9px;justify-content:center;margin-top:24px}
.chip{padding:8px 10px;border:2px solid var(--line);border-radius:7px;display:flex;align-items:center;gap:7px;background:rgba(255,250,255,.76);color:#706881;font-size:10px;font-weight:700;box-shadow:3px 3px 0 rgba(201,185,220,.5)}
.chip::before{content:"";width:7px;height:7px;background:var(--mint)}
.chip:nth-child(2)::before{background:var(--pink)}.chip:nth-child(3)::before{background:var(--yellow)}
.addr-bar{display:flex;align-items:center;gap:0;margin-bottom:10px}
.addr-bar .em{flex:1;min-width:0;padding:11px 14px;border:2px solid var(--purple);border-radius:7px 0 0 7px;background:#fcf9ff;font:600 15px/1.3 "Pixelify Sans",sans-serif;color:var(--ink);word-break:break-all;user-select:all}
.addr-bar .cp{flex:0 0 auto;padding:0 14px;border:2px solid var(--purple);border-left:0;border-radius:0 7px 7px 0;background:#8e7abd;color:#fff;font:700 11px/1 "Pixelify Sans",sans-serif;box-shadow:0 3px 0 #67568f;transition:transform .12s,box-shadow .12s;min-height:46px;display:flex;align-items:center}
.addr-bar .cp:hover{transform:translateY(2px);box-shadow:0 1px 0 #67568f}
.addr-bar .cp.copied{background:#71bd99;box-shadow:0 3px 0 #4a9c72}
.expiry{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:14px;padding:8px 12px;border:2px solid var(--line);border-radius:7px;background:#fff4f8;color:var(--muted);font:700 11px/1 "Pixelify Sans",sans-serif}
.expiry .clk{color:var(--danger);font-size:14px;min-width:42px;text-align:center}
.expiry.warn{border-color:var(--danger);background:#fff0f3;animation:pulsewarn 1s steps(2) infinite}
@keyframes pulsewarn{50%{opacity:.6}}
.switch-row{display:flex;gap:8px;margin-bottom:10px}
.switch-row input{flex:1;min-width:0;padding:10px 12px;border:2px solid var(--purple);border-radius:7px;background:#fcf9ff;color:var(--ink);font-size:13px;outline:none}
.switch-row input:focus{box-shadow:inset 0 0 0 1px var(--lavender)}
.new-btn{width:100%;min-height:40px;margin-bottom:16px;border:2px solid var(--purple);border-radius:7px;background:#f8dbe5;color:var(--purple);font:700 12px/1 "Pixelify Sans",sans-serif;box-shadow:0 4px 0 #dec2df;cursor:pointer;transition:transform .12s,box-shadow .12s}
.new-btn:hover{transform:translateY(2px);box-shadow:0 2px 0 #dec2df}
.inbox-head{display:flex;align-items:center;justify-content:space-between;margin:18px 0 10px;padding:0 2px}
.inbox-head h2{margin:0;font:700 14px/1 "Pixelify Sans",sans-serif;color:var(--purple)}
.inbox-head .cnt{color:var(--muted);font:600 10px/1 "Pixelify Sans",sans-serif}
.mail-list{display:grid;gap:8px}
.mail-row{border:2px solid var(--line);border-radius:7px;background:rgba(255,250,255,.8);box-shadow:3px 3px 0 rgba(201,185,220,.4);padding:12px 14px;display:flex;align-items:flex-start;gap:10px;cursor:pointer;transition:transform .12s,box-shadow .12s}
.mail-row:hover{transform:translateY(-2px);box-shadow:3px 5px 0 rgba(113,96,156,.25)}
.mail-row .ind{flex:0 0 auto;width:8px;height:8px;margin-top:5px;background:var(--pink)}
.mail-row .ind.read{background:var(--line)}
.mail-row .body{flex:1;min-width:0}
.mail-row .top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.mail-row .from{font-size:12px;font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mail-row .date{flex:0 0 auto;color:var(--muted);font:600 9px/1 "Pixelify Sans",sans-serif}
.mail-row .subj{margin-top:2px;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{text-align:center;padding:40px 20px;color:var(--muted)}
.empty .ico{width:56px;height:56px;margin:0 auto 14px;border:2px solid var(--line);border-radius:7px;background:#f7f1fc;display:grid;place-items:center;box-shadow:3px 3px 0 rgba(201,185,220,.4)}
.empty .ico svg{width:28px;color:var(--lavender)}
.empty p{margin:0;font-size:13px}
.empty .sub{margin-top:4px;font-size:11px;color:var(--shadow)}
.fade-in{animation:fadeIn .35s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.slide-up{animation:slideUp .3s ease}
@keyframes slideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
dialog{border:0;padding:0;background:transparent;max-width:none;max-height:none}
dialog::backdrop{background:rgba(64,57,87,.45)}
.modal-box{width:min(560px,calc(100vw - 28px));max-height:calc(100vh - 56px);overflow-y:auto;margin:auto;padding:16px;border:3px solid var(--purple);border-radius:11px;background:var(--paper);box-shadow:9px 9px 0 rgba(113,96,156,.2)}
.modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}
.modal-head h3{margin:0;font:700 18px/1.25 "Pixelify Sans",sans-serif;word-break:break-word}
.modal-meta{margin-top:8px;display:grid;gap:3px;font-size:12px;color:var(--muted)}
.modal-meta b{color:var(--ink)}
.modal-close{flex:0 0 auto;width:30px;height:30px;border:2px solid var(--purple);border-radius:5px;background:var(--pink);color:var(--ink);font:700 14px/1 "Pixelify Sans",sans-serif;box-shadow:2px 2px 0 rgba(113,96,156,.27);display:grid;place-items:center}
.modal-close:hover{transform:translateY(1px);box-shadow:1px 1px 0 rgba(113,96,156,.27)}
.modal-div{height:2px;background:var(--line);margin:10px 0}
.modal-body{margin-top:8px}
.modal-body iframe{width:100%;min-height:380px;border:2px solid var(--line);border-radius:7px;background:#fff}
.modal-body pre{white-space:pre-wrap;word-wrap:break-word;background:#f7f1fc;border:2px solid var(--line);border-radius:7px;padding:14px;font-size:13px;line-height:1.55;font-family:ui-monospace,monospace;overflow:auto;max-height:50vh}
.modal-att{margin-top:12px;display:flex;flex-wrap:wrap;gap:6px}
.modal-att a{padding:7px 10px;border:2px solid var(--purple);border-radius:5px;background:#f8dbe5;color:var(--purple);font:700 10px/1 "Pixelify Sans",sans-serif;text-decoration:none;box-shadow:2px 2px 0 rgba(113,96,156,.2)}
.modal-att a:hover{transform:translateY(1px);box-shadow:1px 1px 0 rgba(113,96,156,.2)}
.toast{position:fixed;right:20px;bottom:20px;z-index:100;max-width:calc(100% - 40px);padding:12px 15px;border:2px solid var(--purple);border-radius:6px;color:var(--ink);background:#f7fff9;box-shadow:5px 5px 0 rgba(113,96,156,.25);font-size:11px;transform:translateY(15px);opacity:0;pointer-events:none;transition:.2s}
.toast.show{transform:translateY(0);opacity:1}
footer{display:flex;align-items:center;justify-content:center;gap:16px;color:#90869f;font-size:10px}
@keyframes twinkle{50%{opacity:.25;transform:scale(.8)}}
@media(max-width:520px){.shell{width:calc(100% - 22px);padding:14px 0 22px;gap:24px}.brand{font-size:15px}.brand-icon{width:34px;height:34px}.status{font-size:9px}.window{padding:8px;box-shadow:6px 6px 0 rgba(113,96,156,.2)}.cloud{transform:scale(.75)}.spark.s1,.spark.s4{display:none}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div class="scene" aria-hidden="true">
<svg class="cloud one" viewBox="0 0 180 75"><path fill="#fffaff" stroke="#d8cbea" stroke-width="3" d="M8 57V44h15V31h17V17h31v10h22V14h30v12h18v13h23v18z"/></svg>
<svg class="cloud two" viewBox="0 0 180 75"><path fill="#fffaff" stroke="#d8cbea" stroke-width="3" d="M8 57V44h15V31h17V17h31v10h22V14h30v12h18v13h23v18z"/></svg>
<svg class="cloud three" viewBox="0 0 180 75"><path fill="#fffaff" stroke="#d8cbea" stroke-width="3" d="M8 57V44h15V31h17V17h31v10h22V14h30v12h18v13h23v18z"/></svg>
<span class="spark s1">x</span><span class="spark s2">x</span><span class="spark s3">x</span><span class="spark s4">x</span>
</div>
<div class="shell">
__HEADER__
<main>
__CONTENT__
</main>
__FOOTER__
</div>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
__SCRIPT__
</body>
</html>"""


HEADER = """<header>
<div class="brand">
<span class="brand-icon">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>
</span>
Temp Mail
</div>
<div class="status"><i class="dot" id="connDot"></i><span id="connText">connecting</span></div>
</header>"""


FOOTER = """<footer><span>yourdomain.com</span></footer>"""


HOME_BODY = """
<div class="hero fade-in">
<p class="quest">New quest unlocked</p>
<h1>Temp Mail.<span>Keep it short.</span></h1>
<p class="lead">Disposable email address in one click. Receive messages instantly. Every inbox expires in 10 minutes.</p>
</div>
<div class="window fade-in">
<div class="window-bar"><span class="window-title">temp_mail.exe</span><span class="window-dots"><i></i><i></i><i></i></span></div>
<label class="field-label" for="customName">Custom address (optional)</label>
<div class="input-row">
<input id="customName" placeholder="pick a name" autocomplete="off" spellcheck="false">
<span class="suf">@yourdomain.com</span>
</div>
<button class="btn" onclick="goCustom()">Create inbox</button>
<div class="divider-text">or</div>
<button class="btn ghost" onclick="goRandom()">Generate random address</button>
</div>
<div class="chips">
<span class="chip">10 min expiry</span>
<span class="chip">Real-time</span>
<span class="chip">No signup</span>
</div>
"""


INBOX_BODY = """
<div class="window fade-in">
<div class="window-bar"><span class="window-title">inbox.exe</span><span class="window-dots"><i></i><i></i><i></i></span></div>
<div class="addr-bar">
<div class="em" id="emailDisplay">__EMAIL__</div>
<button class="cp" id="copyBtn" onclick="copyEmail()">Copy</button>
</div>
<div class="expiry" id="expiryBox">
<span>Expires in</span>
<span class="clk" id="expiryClock">10:00</span>
</div>
<div class="switch-row">
<input id="switchName" placeholder="switch to another name" autocomplete="off" spellcheck="false">
<button class="btn sm ghost" onclick="switchAddr()">Switch</button>
</div>
<button class="new-btn" onclick="location.href='/random'">New random address</button>
<div class="inbox-head">
<h2>Inbox</h2>
<span class="cnt" id="msgCount"></span>
</div>
<div class="mail-list" id="mailList"></div>
<div class="empty" id="emptyState">
<div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg></div>
<p>Waiting for incoming mail</p>
<p class="sub">Messages will appear here automatically</p>
</div>
</div>
<dialog id="mailModal">
<div class="modal-box">
<div class="modal-head">
<div style="min-width:0;flex:1">
<h3 id="modalSubject"></h3>
<div class="modal-meta">
<p><b>From:</b> <span id="modalFrom"></span></p>
<p><b>Date:</b> <span id="modalDate"></span></p>
</div>
</div>
<button class="modal-close" onclick="closeModal()">X</button>
</div>
<div class="modal-div"></div>
<div class="modal-body" id="modalBody"></div>
<div class="modal-att" id="modalAtt"></div>
</div>
</dialog>
"""


SCRIPT_HOME = """
<script>
function goRandom(){location.href='/random'}
function goCustom(){
var v=document.getElementById('customName').value.trim().toLowerCase();
if(!v){location.href='/random';return}
var f=document.createElement('form');
f.method='POST';f.action='/create';
var i=document.createElement('input');
i.type='hidden';i.name='name';i.value=v;
f.appendChild(i);document.body.appendChild(f);f.submit()
}
document.getElementById('customName').addEventListener('keydown',function(e){if(e.key==='Enter')goCustom()})
</script>
"""


SCRIPT_INBOX = """
<script>
var LOCAL=__LOCAL_JSON__;
var EMAIL=__EMAIL_JSON__;
var EXPIRES=__EXPIRES_JSON__;
var socket;
function connect(){
socket=io({transports:['polling','websocket'],path:'/ws/socket.io',upgrade:true,rememberUpgrade:true});
socket.on('connect',function(){
document.getElementById('connDot').className='dot online';
document.getElementById('connText').textContent='live';
socket.emit('join',{room:LOCAL});
});
socket.on('disconnect',function(){
document.getElementById('connDot').className='dot';
document.getElementById('connText').textContent='offline';
});
socket.on('new_mail',function(){refresh(true)});
socket.on('connect_error',function(){
document.getElementById('connDot').className='dot';
document.getElementById('connText').textContent='polling';
});
}
var pollTimer=setInterval(function(){refresh(false)},5000);
function copyEmail(){
navigator.clipboard.writeText(EMAIL);
var b=document.getElementById('copyBtn');
b.classList.add('copied');b.textContent='Copied';
setTimeout(function(){b.classList.remove('copied');b.textContent='Copy'},1200);
}
function switchAddr(){
var v=document.getElementById('switchName').value.trim().toLowerCase();
if(!v)return;
var f=document.createElement('form');
f.method='POST';f.action='/create';
var i=document.createElement('input');
i.type='hidden';i.name='name';i.value=v;
f.appendChild(i);document.body.appendChild(f);f.submit()
}
function escapeHtml(s){return(s||'').replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function tickExpiry(){
var now=Date.now()/1000;
var remain=EXPIRES-now;
if(remain<=0){
document.getElementById('expiryClock').textContent='0:00';
location.href='/';
return;
}
var m=Math.floor(remain/60);
var s=Math.floor(remain%60);
document.getElementById('expiryClock').textContent=m+':'+(s<10?'0':'')+s;
if(remain<60){document.getElementById('expiryBox').classList.add('warn')}
}
tickExpiry();setInterval(tickExpiry,1000);
async function refresh(notify){
try{
var r=await fetch('/api/inbox/'+encodeURIComponent(LOCAL),{cache:'no-store'});
var d=await r.json();
var list=document.getElementById('mailList');
var empty=document.getElementById('emptyState');
document.getElementById('msgCount').textContent=d.count?d.count+' total':'';
if(!d.messages.length){list.innerHTML='';empty.style.display='block';return}
empty.style.display='none';
var prevCount=parseInt(list.dataset.count||'0');
list.dataset.count=d.count;
list.innerHTML=d.messages.map(function(m){
var dot=m.unread?'<div class="ind"></div>':'<div class="ind read"></div>';
return '<div class="mail-row slide-up" data-msg-id="'+escapeHtml(m.id)+'">'+dot+
'<div class="body"><div class="top"><span class="from">'+escapeHtml(m.from||'unknown')+'</span><span class="date">'+escapeHtml(m.date||'')+'</span></div>'+
'<div class="subj">'+escapeHtml(m.subject||'(no subject)')+'</div></div></div>';
}).join('');
list.querySelectorAll('.mail-row').forEach(function(el){el.addEventListener('click',function(){openMessage(el.dataset.msgId)})});
if(notify&&d.count>prevCount){showToast('New message received')}
}catch(e){console.error(e)}
}
function showToast(msg){
var t=document.createElement('div');
t.className='toast show';
t.textContent=msg;
document.body.appendChild(t);
setTimeout(function(){t.remove()},3000);
}
async function openMessage(id){
var r=await fetch('/api/message/'+encodeURIComponent(LOCAL)+'/'+encodeURIComponent(id),{cache:'no-store'});
var m=await r.json();
document.getElementById('modalSubject').textContent=m.subject||'(no subject)';
document.getElementById('modalFrom').textContent=m.from||'';
document.getElementById('modalDate').textContent=m.date||'';
var body=document.getElementById('modalBody');
if(m.html){body.innerHTML='<iframe sandbox="allow-same-origin" srcdoc="'+m.html.replace(/"/g,'&quot;')+'"></iframe>'}
else{body.innerHTML='<pre>'+escapeHtml(m.plain||'(empty)')+'</pre>'}
var attDiv=document.getElementById('modalAtt');
if(m.attachments&&m.attachments.length){
attDiv.innerHTML=m.attachments.map(function(a){
return '<a href="/api/attachment/'+encodeURIComponent(LOCAL)+'/'+encodeURIComponent(a.id)+'" download>'+escapeHtml(a.filename)+' ('+Math.round(a.size/1024)+'KB)</a>';
}).join('');
}else{attDiv.innerHTML=''}
document.getElementById('mailModal').showModal();
refresh(false);
}
function closeModal(){document.getElementById('mailModal').close()}
connect();
refresh(false);
</script>
"""




@app.route("/")
def index():
    return redirect(url_for("home"))


@app.route("/home")
def home():
    html = PAGE.replace("__HEADER__", HEADER).replace("__CONTENT__", HOME_BODY)
    html = html.replace("__FOOTER__", FOOTER).replace("__SCRIPT__", SCRIPT_HOME)
    return html


@app.route("/random")
def random_inbox():
    local = random_local()
    exp = touch_mailbox(local)
    session["local"] = local
    return redirect(url_for("inbox", local=local))


@app.route("/create", methods=["POST"])
def create_inbox():
    local = sanitize(request.form.get("name", ""))
    if not valid(local):
        return redirect(url_for("home"))
    touch_mailbox(local)
    session["local"] = local
    return redirect(url_for("inbox", local=local))


@app.route("/inbox/<local>")
def inbox(local):
    local = sanitize(local)
    if not valid(local):
        return redirect(url_for("home"))
    exp = get_mailbox_expiry(local)
    if exp is None:
        return redirect(url_for("home"))
    now = time.time()
    if exp < now:
        return redirect(url_for("home"))
    session["local"] = local
    email_addr = f"{local}@{DOMAIN}"
    body = INBOX_BODY.replace("__EMAIL__", email_addr)
    script = SCRIPT_INBOX.replace("__LOCAL_JSON__", json.dumps(local)).replace("__EMAIL_JSON__", json.dumps(email_addr)).replace("__EXPIRES_JSON__", json.dumps(exp))
    html = PAGE.replace("__HEADER__", HEADER).replace("__CONTENT__", body)
    html = html.replace("__FOOTER__", FOOTER).replace("__SCRIPT__", script)
    return html


@app.route("/api/inbox/<local>")
def api_inbox(local):
    local = sanitize(local)
    if not valid(local):
        return jsonify({"error": "invalid"}), 400
    msgs = list_messages(local)
    return jsonify({
        "address": f"{local}@{DOMAIN}",
        "count": len(msgs),
        "messages": msgs,
    })


@app.route("/api/message/<local>/<path:msgid>")
def api_message(local, msgid):
    local = sanitize(local)
    if not valid(local):
        return jsonify({"error": "invalid"}), 400
    m = get_message(local, msgid)
    if not m:
        return jsonify({"error": "not found"}), 404
    return jsonify(m)


@app.route("/api/attachment/<local>/<path:attid>")
def api_attachment(local, attid):
    local = sanitize(local)
    if not valid(local):
        return ("Invalid name", 400)
    with _db_lock:
        conn = db()
        r = conn.execute("SELECT a.filename,a.content_type,a.data FROM attachments a JOIN emails e ON a.email_id=e.id WHERE e.localpart=? AND a.id=?", (local, attid)).fetchone()
        conn.close()
    if not r:
        return ("Not found", 404)
    from flask import Response
    return Response(r["data"], mimetype=r["content_type"] or "application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{r["filename"]}"'})


@app.route("/incoming-email", methods=["POST"])
def incoming_email():
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    raw = data.get("raw", "")
    if not raw:
        return jsonify({"error": "empty"}), 400
    result = parse_and_store_email(raw, data.get("from", ""), data.get("to", ""), data.get("headers", {}))
    if not result:
        return jsonify({"error": "invalid recipient"}), 400
    socketio.emit("new_mail", {"to": f"{result['local']}@{DOMAIN}"}, room=result["local"])
    return jsonify({"ok": True, "local": result["local"], "id": result["id"]})


@socketio.on("join")
def on_join(data):
    room = sanitize(str(data.get("room", "")))
    if valid(room):
        touch_mailbox(room)
        join_room(room)
        emit("joined", {"room": room})


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    init_db()
    socketio.run(app, host="127.0.0.1", port=8000)
