# DDNS Editor

A web UI for editing [qdm12/ddns-updater](https://github.com/qdm12/ddns-updater)'s `config.json` — no more hand-editing JSON over SSH just to add a domain or rotate a token.

It's built to sit alongside `ddns-updater` in the same Docker Compose stack, sharing its config volume, so changes you make here take effect the next time `ddns-updater` restarts.

![Python](https://img.shields.io/badge/python-3.12-blue)
![Flask](https://img.shields.io/badge/flask-3.0-black)
![Docker](https://img.shields.io/badge/docker-multi--arch-2496ED)
![Tests](https://img.shields.io/badge/tests-pytest-green)
![License](https://img.shields.io/badge/license-MIT-yellow)

---

## Features

- **Configured Records view** — a table of every DNS record currently in `config.json`, with a per-provider summary, live update status (see below), and search/filter by domain or provider.
- **Add / Edit form**, schema-driven across **10 providers**: Cloudflare, Duck DNS, NoIP, Name.com, DigitalOcean, GoDaddy, Dynu, Porkbun, Namecheap, and OVH (DynHost mode). Anything outside these still works via the Advanced tab.
- **Live "Test Connection"** for Cloudflare, DigitalOcean, GoDaddy, and Porkbun — an actual read-only API call before you save, so a bad token surfaces immediately instead of days later. (Not offered for providers whose only API call *is* a real DNS update — testing those would trigger a live change as a side effect.)
- **Update status** — reads `ddns-updater`'s own `updates.json` and shows last-known IP/time per record, best-effort (that file's schema isn't officially documented; a raw-JSON debug panel is included in case it needs adjusting for your version).
- **Automatic backups** — every save backs up the previous `config.json` first (kept last 10, configurable), with one-click **Restore** and **Download** per backup.
- **Optional login** — set up a password through the app itself (`/setup`) or via an `EDITOR_PASSWORD` env var (which also works as a permanent recovery credential alongside GUI-managed users). Includes login lockout after repeated failures and secure session cookie flags.
- **Multi-user accounts with roles** — add additional users (admin or read-only) from the Users page; read-only accounts can view records/status/activity and run Test Connection, but can't save, delete, or restore anything.
- **Two-factor authentication (TOTP)** — optional per-user, self-service setup via any standard authenticator app, with a scannable QR code (manual key entry also available as a fallback).
- **Account menu** — click your username to Change Password, enable/disable 2FA, or Log out, all in one place. Dark mode toggle available everywhere, including the login screen.
- **Version footer** — shows the running version (set automatically from the git tag at build time) with a subtle indicator when a newer release is available on GitHub.
- **Token-based API** — create scoped API tokens (admin or read-only) for scripts/automation to list, add, or delete records via `GET/POST /api/v1/records` without a browser session.
- **Webhook notifications** — optional `WEBHOOK_URL` (generic JSON, Discord, Slack, or ntfy format) pings on record changes, backup restores, logins, and account/token changes.
- **CSRF protection** on every state-changing action.
- **Activity log** — tracks record changes, backup restores, logins, password/2FA changes, and user/token management.
- **Advanced (Raw JSON) tab** — full manual control, always available as a fallback.
- **Safe writes** — config is written to a temp file and atomically renamed into place, so a crash mid-save can't corrupt your live config.
- **`GET /healthz`** for container orchestrators / uptime monitors.
- **Multi-arch image** — published for `linux/amd64` and `linux/arm64`, so it runs on a Raspberry Pi as happily as a NAS or VM.
- **Tested in CI** — a 47-case pytest suite runs on every release before anything is built, plus a post-build smoke test that actually runs the container and hits `/healthz` before it's pushed to Docker Hub.

---

## Quick start

Run alongside `ddns-updater` itself, sharing a config volume:

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

```bash
docker network create homelab_network   # skip if it already exists
docker compose up -d
```

Then open **http://localhost:5001**. If you haven't set a password yet, you'll see a banner prompting you to secure it via `/setup` — or skip it if this stays on a fully trusted LAN.

> After saving a config change, restart `ddns-updater` (`docker restart ddns-updater`) for it to pick up the new settings.

**Exposing this beyond your LAN?** See [`docs/REVERSE_PROXY.md`](docs/REVERSE_PROXY.md) for HTTPS setup (Nginx Proxy Manager, Caddy, or Traefik) — don't skip this if it's reachable from the internet.

---

## Configuration

| Environment variable | Required | Default | Description |
|---|---|---|---|
| `FLASK_SECRET_KEY` | No | random on each restart | Signs session/flash cookies. Set this if you want sessions to survive a container restart. |
| `EDITOR_USERNAME` | No | `admin` | Username when using `EDITOR_PASSWORD`. Ignored if you set a password via `/setup` instead. |
| `EDITOR_PASSWORD` | No | unset (editor stays open) | Sets a login password. Also works as a permanent recovery credential alongside a GUI-set password. |
| `MAX_BACKUPS` | No | `10` | Number of automatic `config.json` backups to keep. |
| `SESSION_COOKIE_SECURE` | No | `false` | Set to `true` **only** once you're actually serving over HTTPS (see reverse proxy doc) — enabling it too early breaks login entirely. |
| `TRUST_PROXY_HEADERS` | No | `false` | Set to `true` only if a trusted reverse proxy sets `X-Forwarded-For` — otherwise the login lockout can be bypassed by a spoofed header. |
| `LOGIN_LOCKOUT_THRESHOLD` | No | `5` | Failed login attempts (per IP) before a temporary lockout. |
| `LOGIN_LOCKOUT_WINDOW_SECONDS` | No | `300` | Lockout window length, in seconds. |
| `WEBHOOK_URL` | No | unset | If set, POSTs a notification here on record changes, backup restores, logins, and account/token management events. |
| `WEBHOOK_FORMAT` | No | `generic` | Payload shape for `WEBHOOK_URL`: `generic` (plain JSON), `discord`, `slack`, or `ntfy`. |
| `APP_VERSION` | No (set automatically) | `dev` | Baked in at Docker build time from the git tag being built -- shown in the footer. Only needs setting manually for a plain local `docker build`. |
| `UPDATE_CHECK_ENABLED` | No | `true` | Checks GitHub for a newer tag periodically and shows a subtle dot next to the version in the footer. Set to `false` to disable entirely (e.g. air-gapped deployments). |
| `UPDATE_CHECK_REPO` | No | `mOneThree/ddns-editor` | Which GitHub repo to check for newer tags. |
| `UPDATE_CHECK_INTERVAL_SECONDS` | No | `3600` | How often to re-check for a newer version. |

| Volume | Purpose |
|---|---|
| `/updater/data` | Must point at the **same** path/volume as `ddns-updater`'s `/updater/data`. Also where this app stores `auth.json` (user accounts/2FA), `api_tokens.json`, `backups/`, and `activity.log`. |

---

## Building locally

```bash
git clone https://github.com/mOneThree/ddns-editor.git
cd ddns-editor
docker build -t ddns-editor:local .
docker run -p 5001:5000 -v $(pwd)/data:/updater/data ddns-editor:local
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

This repo ships a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that runs the test suite, then a post-build smoke test, then publishes multi-arch images to Docker Hub as `mthirteenz/ddns-editor` -- triggered by pushing a version tag (`vX.Y.Z`), not by every commit to `main`.

```bash
git tag v1.7.0
git push origin v1.7.0
```

---

## Project structure

```
.
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── PROVIDER_BACKLOG.md        # remaining ~35 ddns-updater providers not yet schema'd
├── ENHANCEMENT_BACKLOG.md     # other deferred feature ideas
├── .github/workflows/docker-publish.yml
├── docs/
│   └── REVERSE_PROXY.md      # HTTPS via Nginx Proxy Manager / Caddy / Traefik
├── tests/
│   └── test_app.py           # pytest suite, runs in CI before every build
└── app/
    ├── app.py
    ├── templates/
    │   ├── index.html         # Records / Add-Edit / Advanced / Backups / Activity tabs
    │   ├── login.html
    │   ├── login_2fa.html
    │   ├── setup.html
    │   ├── setup_2fa.html     # includes QR code generation
    │   ├── users.html         # admin-only user management
    │   ├── api_tokens.html    # admin-only API token management
    │   ├── _sidebar.html      # shared nav, desktop sidebar / mobile icon bar
    │   ├── _account_menu.html # username dropdown: change password / 2FA / log out
    │   ├── _account_modals.html
    │   ├── _theme_init.html   # sets light/dark before first paint
    │   ├── _theme_toggle.html
    │   ├── _footer.html       # version + update-available indicator
    │   └── _shell_style.html  # shared design tokens/CSS
    └── static/
```

---

## Security notes

- This container **runs as root**. It shares a bind-mounted volume with `ddns-updater`, and pinning a non-root UID risked permission mismatches against whatever UID that image writes as. Acceptable for a small, internal-only homelab tool.
- Login is **optional** — if you never set a password, the app stays fully open (a banner will remind you). Set one via `/setup` or `EDITOR_PASSWORD` if this is reachable by anyone other than you.
- If exposing this beyond a fully trusted LAN, put it behind HTTPS (see [`docs/REVERSE_PROXY.md`](docs/REVERSE_PROXY.md)) — without it, your password and provider tokens cross the network in cleartext.
- API tokens/passwords are stored in plaintext in `config.json`, matching `ddns-updater`'s own expectations. Treat the `data/` volume as sensitive. The login password itself is stored **hashed** (`werkzeug.security`), never in plaintext.

---

## Supported providers (Add/Edit form)

| Provider | Fields | Live "Test Connection" |
|---|---|---|
| Cloudflare | Zone ID, API Token, Proxied, TTL | ✅ |
| Duck DNS | Token | — |
| NoIP | Username, Password | — |
| Name.com | Username, API Token, TTL | — |
| DigitalOcean | API Token | ✅ |
| GoDaddy | API Key, API Secret | ✅ |
| Dynu | Group, Username, Password | — |
| Porkbun | API Key, Secret API Key | ✅ |
| Namecheap | Dynamic DNS Password | — |
| OVH (DynHost mode) | Username, Password | — |

Need a provider not listed? Use the **Advanced** tab to hand-write the JSON entry — see [ddns-updater's provider docs](https://github.com/qdm12/ddns-updater/tree/master/docs) for the exact shape each one expects, and check [`PROVIDER_BACKLOG.md`](PROVIDER_BACKLOG.md) for the full remaining list.

---

## API access

Create a token from the **API Tokens** page (admin users only), then call:

```bash
# List records
curl -H "Authorization: Bearer <token>" https://your-host:5001/api/v1/records

# Add a record (admin-role token required)
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"provider": "duckdns", "domain": "home.example.com", "token": "your-duckdns-token"}' \
  https://your-host:5001/api/v1/records

# Delete a record by index (admin-role token required)
curl -X DELETE -H "Authorization: Bearer <token>" https://your-host:5001/api/v1/records/0
```

Read-only tokens can call `GET /api/v1/records` and `GET /api/v1/status`; admin tokens can also add/delete. Tokens are shown once at creation and stored hashed — there's no way to retrieve a lost token, only revoke and re-create.

---

## License

MIT — see [`LICENSE`](LICENSE) for the full text.
