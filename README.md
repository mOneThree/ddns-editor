# 🌐 DDNS Editor

### *A modern web UI for qdm12/ddns-updater*

![Pre-Alpha](https://img.shields.io/badge/status-%F0%9F%9A%A7%20Pre--Alpha-orange) ![Python](https://img.shields.io/badge/python-3.12-blue) ![Flask](https://img.shields.io/badge/flask-3.0-black) ![Docker](https://img.shields.io/badge/docker-multi--arch-2496ED) ![Tests](https://img.shields.io/badge/tests-pytest-green) ![License](https://img.shields.io/badge/license-Unlicense-brightgreen)

**Manage DNS records, providers, and tokens without hand-editing JSON over SSH**

[Installation](#-quick-start) • [Features](#-key-features) • [Configuration](#-configuration) • [Providers](#-supported-providers)

---

## ✨ About

> ⚠️ **Early Development:** This project is in pre-alpha and under active development. Features, APIs, and configuration may change significantly. Use at your own risk in production environments.

**DDNS Editor** is built to sit alongside `ddns-updater` in the same Docker Compose stack, sharing its config volume. Changes you make through the beautiful web interface take effect the next time `ddns-updater` restarts. No more manual JSON editing just to add a domain or rotate a token!

### 🎯 Key Features (🚧 Pre-Alpha)

|#### 📝 **Configured Records View**

A complete table of every DNS record currently in `config.json`, featuring a per-provider summary, live update status, and search/filter by domain or provider.

#### 🔌 **10 Supported Providers**

Schema-driven Add/Edit form for Cloudflare, Duck DNS, NoIP, Name.com, DigitalOcean, GoDaddy, Dynu, Porkbun, Namecheap, and OVH. Anything else works via the Advanced tab!

#### 🧪 **Live "Test Connection"**

Actual read-only API calls before you save for Cloudflare, DigitalOcean, GoDaddy, and Porkbun. Bad tokens surface immediately instead of days later. |#### 🔐 **Security & Access Control**

Optional login with 2FA (TOTP), multi-user accounts with roles (admin/read-only), secure session cookies, login lockouts, and CSRF protection on all actions.

#### 💾 **Safe Writes & Backups**

Config is written to a temp file and atomically renamed to prevent corruption. Every save automatically backs up the previous config with one-click restore.

#### 🔔 **Webhooks & API**

Get notifications (Discord, Slack, ntfy, JSON) on changes. Create scoped API tokens for scripts to manage records without a browser session. || --- | --- |

---

## 🚀 Quick Start

Run alongside `ddns-updater` itself, sharing a config volume.

**1. Docker Compose setup:**

```yaml
version: '3.8'
services:
  ddns-updater:
    image: qmcgaw/ddns-updater
    container_name: ddns-updater
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/updater/data
    networks:
      - homelab_network

  ddns-editor:
    image: mthirteenz/ddns-editor:latest
    container_name: ddns-editor
    restart: unless-stopped
    ports:
      - "5001:5000"
    volumes:
      - ./data:/updater/data
    depends_on:
      - ddns-updater
    networks:
      - homelab_network

networks:
  homelab_network:
    external: true
```

**2. Start the stack:**

```bash
docker network create homelab_network   # skip if it already exists
docker compose up -d
```

**3. Start using it:**

Open [**http://localhost:5001**](http://localhost:5001). If you haven't set a password yet, you'll see a banner prompting you to secure it via `/setup` — or skip it if this stays on a fully trusted LAN.

> 💡 **Note:** After saving a config change, restart `ddns-updater` (`docker restart ddns-updater` ) for it to pick up the new settings.

---

## ⚙️ Configuration

### Environment Variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `FLASK_SECRET_KEY` | No | random | Signs session/flash cookies. Set this if you want sessions to survive a container restart. |
| `EDITOR_USERNAME` | No | `admin` | Username when using `EDITOR_PASSWORD`. Ignored if you set a password via `/setup` instead. |
| `EDITOR_PASSWORD` | No | unset | Sets a login password. Also works as a permanent recovery credential alongside a GUI-set password. |
| `MAX_BACKUPS` | No | `10` | Number of automatic `config.json` backups to keep. |
| `SESSION_COOKIE_SECURE` | No | `false` | Set to `true` **only** once you're actually serving over HTTPS — enabling it too early breaks login entirely. |
| `TRUST_PROXY_HEADERS` | No | `false` | Set to `true` only if a trusted reverse proxy sets `X-Forwarded-For` — prevents login lockout bypass. |
| `LOGIN_LOCKOUT_THRESHOLD` | No | `5` | Failed login attempts (per IP) before a temporary lockout. |
| `LOGIN_LOCKOUT_WINDOW_SECONDS` | No | `300` | Lockout window length, in seconds. |
| `WEBHOOK_URL` | No | unset | If set, POSTs a notification here on record changes, backup restores, logins, and account events. |
| `WEBHOOK_FORMAT` | No | `generic` | Payload shape for `WEBHOOK_URL`: `generic` (plain JSON), `discord`, `slack`, or `ntfy`. |

### Volumes

| Volume | Purpose |
| --- | --- |
| `/updater/data` | Must point at the **same** path/volume as `ddns-updater`'s `/updater/data`. Also where this app stores `auth.json`, `api_tokens.json`, `backups/`, and `activity.log`. |

---

## 🛡️ Security Notes

- **Root execution:** This container runs as root. It shares a bind-mounted volume with `ddns-updater`, and pinning a non-root UID risked permission mismatches. Acceptable for a small, internal-only homelab tool.

- **Optional login:** If you never set a password, the app stays fully open (a banner will remind you). Set one via `/setup` or `EDITOR_PASSWORD` if this is reachable by anyone other than you.

- **Reverse Proxy:** If exposing this beyond a fully trusted LAN, put it behind HTTPS (see [`docs/REVERSE_PROXY.md`](docs/REVERSE_PROXY.md)) — without it, your password and provider tokens cross the network in cleartext.

- **Data storage:** API tokens/passwords are stored in plaintext in `config.json`, matching `ddns-updater`'s own expectations. Treat the `data/` volume as sensitive. The login password itself is stored **hashed** (`werkzeug.security`), never in plaintext.

---

## 🎭 Supported Providers

| Provider | Fields Required | Live "Test Connection" |
| --- | --- | --- |
| **Cloudflare** | Zone ID, API Token, Proxied, TTL | ✅ |
| **Duck DNS** | Token | — |
| **NoIP** | Username, Password | — |
| **Name.com** | Username, API Token, TTL | — |
| **DigitalOcean** | API Token | ✅ |
| **GoDaddy** | API Key, API Secret | ✅ |
| **Dynu** | Group, Username, Password | — |
| **Porkbun** | API Key, Secret API Key | ✅ |
| **Namecheap** | Dynamic DNS Password | — |
| **OVH (DynHost)** | Username, Password | — |

> Need a provider not listed? Use the **Advanced** tab to hand-write the JSON entry — see [ddns-updater's provider docs](https://github.com/qdm12/ddns-updater/tree/master/docs) for the exact shape each one expects, and check [`PROVIDER_BACKLOG.md`](PROVIDER_BACKLOG.md) for the full remaining list.

---

## 🔌 API Access

Create a token from the **API Tokens** page (admin users only), then call:

```bash
# List records
curl -H "Authorization: Bearer <token>" https://your-host:5001/api/v1/records

# Add a record (admin-role token required )
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"provider": "duckdns", "domain": "home.example.com", "token": "your-duckdns-token"}' \
  https://your-host:5001/api/v1/records

# Delete a record by index (admin-role token required )
curl -X DELETE -H "Authorization: Bearer <token>" https://your-host:5001/api/v1/records/0
```

Read-only tokens can call `GET /api/v1/records` and `GET /api/v1/status`; admin tokens can also add/delete. Tokens are shown once at creation and stored hashed.

---

## 🏗️ Development & Building

### Local Build

```bash
git clone https://github.com/mOneThree/ddns-editor.git
cd ddns-editor
docker build -t ddns-editor:local .
docker run -p 5001:5000 -v $(pwd )/data:/updater/data ddns-editor:local
```

### Running the test suite

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

### Multi-arch build

```bash
docker buildx create --use --name ddns-builder   # one-time
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t mthirteenz/ddns-editor:latest \
  --push .
```

This repo ships a GitHub Actions workflow that runs the test suite, then a post-build smoke test, then publishes multi-arch images to Docker Hub as `mthirteenz/ddns-editor` -- triggered by pushing a version tag (`vX.Y.Z`).

---

## 📜 License

Unlicense — This project is released into the public domain. See [`LICENSE`](LICENSE) for the full text.

### ⭐ If you find DDNS Editor helpful, please star this repository!
