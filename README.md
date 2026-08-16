# Temporary Email

A disposable email service with real-time WebSocket push, built on Cloudflare Email Routing + Workers and a lightweight Flask backend. No Postfix, no Dovecot, no open port 25 on the VPS.

![demo](https://img.shields.io/badge/demo-live-brightgreen) ![architecture](https://img.shields.io/badge/architecture-Cloudflare%20%2B%20VPS-blueviolet) ![expiry](https://img.shields.io/badge/inbox%20TTL-10%20minutes-pink) ![license](https://img.shields.io/badge/license-MIT-green)

**Live Demo:** https://email.takahasii.my.id

---

## Summary

Temporary Email lets you generate a throwaway email address in one click and watch messages arrive in real time through a WebSocket connection. Each inbox self-destructs after 10 minutes.

The trick: instead of running a full SMTP stack on the VPS (which requires inbound port 25 — blocked by most cloud providers), email reception is delegated to **Cloudflare Email Routing**. A Cloudflare Worker parses each incoming message and forwards it via HTTPS to your VPS. The VPS only needs port 443.

The UI is a pink "soft pixel" theme inspired by retro game interfaces, using Pixelify Sans and DM Sans.

---

## Features

- **One-click random address** — generates an 8-character inbox instantly
- **Custom address** — pick your own local part
- **Real-time WebSocket push** — new messages appear without refresh, with polling fallback every 5 seconds
- **10-minute expiry** — countdown timer, auto-cleanup of mailbox + emails from the database on expiry
- **No auto-create** — visiting `/inbox/<name>` for a non-existent mailbox redirects to home; you must create it first
- **Attachment support** — email attachments are stored in SQLite and downloadable
- **HTML email rendering** — sandboxed iframe for HTML emails, plain-text fallback
- **No secrets exposed** — the web UI never shows ports, passwords, or server details
- **Cloudflare TLS** — automatic HTTPS with Let's Encrypt certificates

---

## Architecture Flowchart

```
                         INTERNET
                            |
                            | email (SMTP port 25)
                            v
                  +-------------------+
                  |  Gmail / external |
                  |  mail server      |
                  +---------+---------+
                            |
                            v
                  +-------------------+
                  |  Cloudflare MX    |
                  |  route1/2/3.mx    |
                  |  .cloudflare.net  |
                  +---------+---------+
                            |
                            v
                  +-------------------+
                  | Cloudflare Email  |
                  | Routing (catch-   |
                  | all -> Worker)    |
                  +---------+---------+
                            |
                            | email event
                            v
                  +-------------------+
                  | Cloudflare Worker |
                  | tempmail-router   |
                  |                   |
                  | - read sender     |
                  | - read recipient  |
                  | - parse raw email |
                  | - POST to VPS     |
                  +---------+---------+
                            |
                            | HTTPS POST (port 443)
                            | X-Webhook-Secret
                            v
        +-----------------------------------+
        |              VPS                  |
        |                                   |
        |  +-----------------------------+   |
        |  |     Flask backend (app.py)  |   |
        |  |                             |   |
        |  |  POST /incoming-email       |   |
        |  |  - verify webhook secret     |   |
        |  |  - parse recipient          |   |
        |  |  - store in SQLite          |   |
        |  +--------------+--------------+   |
        |                 |                  |
        |                 v                  |
        |  +-----------------------------+   |
        |  |       SQLite database       |   |
        |  |                             |   |
        |  |  mailboxes (localpart, TTL) |   |
        |  |  emails (subject, body...) |   |
        |  |  attachments (blob)         |   |
        |  +--------------+--------------+   |
        |                 |                  |
        |                 | new email        |
        |                 v                  |
        |  +-----------------------------+   |
        |  |    WebSocket (Socket.IO)    |   |
        |  |                             |   |
        |  |  broadcast: new_mail        |   |
        |  |  -> joined room <localpart>|   |
        |  +--------------+--------------+   |
        |                 |                  |
        +-----------------|------------------+
                          |
                          | WSS (port 443)
                          v
                +-------------------+
                |   Browser (UI)    |
                |                   |
                |  abc123@domain    |
                |                   |
                |  +-------------+  |
                |  | New Email!  |  |
                |  | From: ...    |  |
                |  | Subject: ... |  |
                |  +-------------+  |
                |                   |
                |  countdown: 9:47  |
                +-------------------+
```

### Data flow summary

```
EMAIL IN
  -> Cloudflare MX (port 25, on CF side)
  -> Email Routing catch-all
  -> Worker tempmail-router
  -> HTTPS POST /incoming-email
  -> VPS Flask app
  -> SQLite database
  -> WebSocket push
  -> Browser (real-time update)
```

### What is NOT used

| Component     | Why not                          |
|---------------|----------------------------------|
| Postfix       | Cloudflare handles MX            |
| Dovecot       | No IMAP needed, web UI only      |
| Port 25 (VPS) | Blocked by providers; CF receives|
| Port 587/465  | Only for sending, not receiving  |

---

## File Structure

```
temporary-email/
├── app/
│   └── app.py              # Flask backend (routes, WebSocket, DB, email parsing)
├── worker/
│   └── worker.js           # Cloudflare Worker (email -> HTTPS POST to VPS)
├── deploy/
│   ├── tempmail.service    # systemd unit file
│   └── tempmail.nginx      # nginx reverse proxy config
├── email.js                # Cloudflare diagnostic + fix script (node)
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .gitignore
└── README.md
```

---

## Setup Tutorial

### Prerequisites

- A VPS with:
  - Debian/Ubuntu Linux
  - Python 3.11+
  - Root access
  - Ports 80 + 443 open (port 25 NOT needed)
  - A domain pointing to it (A record)
- A Cloudflare account with the domain added (free plan works)
- Node.js installed (for the Worker deploy script)

### Step 1: DNS — Move domain to Cloudflare

1. Add your domain to Cloudflare (dashboard or API)
2. Update your registrar nameservers to Cloudflare's (e.g. `bonnie.ns.cloudflare.com`)
3. Wait for zone to become active

### Step 2: VPS — Install dependencies

```bash
# System packages
apt-get update
apt-get install -y python3 python3-venv nginx certbot git

# Create vmail user for the app
groupadd -g 5000 vmail
useradd -u 5000 -g 5000 -d /var/lib/tempmail -s /usr/sbin/nologin vmail
mkdir -p /var/lib/tempmail
chown -R vmail:vmail /var/lib/tempmail
```

### Step 3: VPS — Deploy the app

```bash
# Clone
git clone https://github.com/ayashiiiyo/temporary-email-.git /opt/tempmail
cd /opt/tempmail

# Python venv
python3 -m venv /opt/tempmail/venv
/opt/tempmail/venv/bin/pip install -r requirements.txt

# Generate secrets
WEBHOOK_SECRET=$(openssl rand -base64 32)
echo "$WEBHOOK_SECRET" > /opt/tempmail/.webhook_secret
chown vmail:vmail /opt/tempmail/.webhook_secret
chmod 640 /opt/tempmail/.webhook_secret

# Initialize database
/opt/tempmail/venv/bin/python -c "
import sys; sys.path.insert(0,'/opt/tempmail/app')
from app import init_db; init_db()
"
chown -R vmail:vmail /var/lib/tempmail
```

### Step 4: VPS — Configure environment

Edit `deploy/tempmail.service` and set:
```ini
Environment=TEMPMAIL_SECRET=<run: python3 -c "import secrets;print(secrets.token_hex(32))">
Environment=DOMAIN=yourdomain.com
Environment=WEBHOOK_SECRET_FILE=/opt/tempmail/.webhook_secret
Environment=DB_PATH=/var/lib/tempmail/tempmail.db
```

Install the service:
```bash
cp deploy/tempmail.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tempmail
```

### Step 5: VPS — TLS certificate

```bash
# Get Let's Encrypt cert
mkdir -p /var/www/html
certbot certonly --standalone -d mail.yourdomain.com

# Set up auto-renew deploy hook
cat > /etc/letsencrypt/renewal-hooks/deploy/mail.sh <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/mail.sh
```

### Step 6: VPS — nginx reverse proxy

Edit `deploy/tempmail.nginx` and replace `mail.yourdomain.com` with your domain.

```bash
cp deploy/tempmail.nginx /etc/nginx/sites-available/tempmail
ln -sf /etc/nginx/sites-available/tempmail /etc/nginx/sites-enabled/tempmail
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
```

> **Important:** Do NOT enable `http2` in nginx for the 443 block. WebSocket upgrade does not work over HTTP/2 in nginx 1.x. The config uses plain `ssl` (HTTP/1.1) to support WebSocket.

### Step 7: Cloudflare — Deploy the Worker

```bash
# Set credentials
export CF_TOKEN="your_cf_api_token"
export CF_ACCOUNT="your_cf_account_id"
export CF_ZONE="your_cf_zone_id"
export WEBHOOK_SECRET="the_secret_you_generated"

# Deploy worker via API
curl -s -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/workers/scripts/tempmail-router" \
  -H "Authorization: Bearer $CF_TOKEN" \
  -F "metadata={\"main_module\":\"worker.js\",\"compatibility_date\":\"2024-09-01\",\"bindings\":[{\"type\":\"secret_text\",\"name\":\"WEBHOOK_SECRET\",\"text\":\"$WEBHOOK_SECRET\"}]};type=application/json" \
  -F "worker.js=@worker/worker.js;type=application/javascript+module;filename=worker.js"
```

The Worker needs the `email` handler and a `WEBHOOK_SECRET` binding matching the VPS secret.

### Step 8: Cloudflare — Enable Email Routing

1. Go to Cloudflare Dashboard -> your domain -> **Email** -> **Email Routing**
2. Click **Enable Email Routing** (this sets MX records to `route1/2/3.mx.cloudflare.net`)
3. Go to **Routing rules** -> **Catch-all address** -> **Edit**
4. Action: **Send to a Worker** -> select `tempmail-router` -> **Save**

> Your CF API token needs these permissions:
> - Workers Scripts: Edit
> - Email Routing: Edit
> - DNS: Edit
>
> If your token lacks Email Routing access, use the dashboard instead.

### Step 9: Verify

```bash
# Check VPS health
curl https://mail.yourdomain.com/healthz

# Run the diagnostic script
node email.js check
```

Open `https://mail.yourdomain.com/home`, generate a random address, send an email to it from Gmail, and watch it arrive in real time.

---

## API Endpoints

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/`                           | Redirect to `/home`                |
| GET    | `/home`                       | Landing page (create inbox)        |
| GET    | `/random`                     | Create random mailbox, redirect   |
| POST   | `/create`                     | Create custom mailbox (form: name) |
| GET    | `/inbox/<local>`              | Inbox page (404 if not created)    |
| GET    | `/api/inbox/<local>`           | List messages as JSON              |
| GET    | `/api/message/<local>/<id>`   | Get full message as JSON           |
| GET    | `/api/attachment/<local>/<id>` | Download attachment                |
| POST   | `/incoming-email`             | Worker webhook (secret-protected)  |
| GET    | `/healthz`                    | Health check                       |
| WS     | `/ws/socket.io`               | WebSocket (join room, get pushes)  |

---

## Diagnostic Script

The `email.js` script checks and fixes Cloudflare configuration:

```bash
# Check only (read-only)
node email.js check

# Check and attempt to fix
node email.js fix
```

It verifies:
- MX records point to Cloudflare
- Email Routing is enabled
- Catch-all rule routes to the Worker
- Worker is deployed with email handler
- VPS backend is reachable

---

## Tech Stack

| Layer       | Technology                          |
|-------------|-------------------------------------|
| Email MX    | Cloudflare Email Routing            |
| Email parse | Cloudflare Worker (JavaScript)      |
| Backend     | Flask 3.1 + Flask-SocketIO 5.6     |
| Async       | Eventlet                            |
| Database    | SQLite (WAL mode)                   |
| Realtime    | WebSocket (Socket.IO)              |
| Reverse px | nginx 1.22 (HTTP/1.1 for WS)       |
| TLS         | Let's Encrypt (certbot)            |
| Frontend    | Vanilla HTML/CSS + Pixelify Sans   |

---

## Security

- Webhook endpoint (`/incoming-email`) requires `X-Webhook-Secret` header
- Mailbox names validated with regex (`^[a-z0-9][a-z0-9._-]{0,62}$`)
- SQLite uses WAL mode for concurrent access
- HTML emails rendered in sandboxed iframe (no script execution)
- No secrets exposed in the web UI
- nginx proxies WebSocket with proper upgrade headers

---

## License

MIT
