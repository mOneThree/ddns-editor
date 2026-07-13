# DDNS Editor

A web UI for editing [qdm12/ddns-updater](https://github.com/qdm12/ddns-updater)'s `config.json` — no more hand-editing JSON over SSH just to add a domain or rotate a token.

It's built to sit alongside `ddns-updater` in the same Docker Compose stack, sharing its config volume, so changes you make here take effect the next time `ddns-updater` restarts.

![Python](https://img.shields.io/badge/python-3.12-blue)
![Flask](https://img.shields.io/badge/flask-3.0-black)
![Docker](https://img.shields.io/badge/docker-multi--arch-2496ED)
![Tests](https://img.shields.io/badge/tests-pytest-green)

---

## Features

- **Configured Records view** — a table of every DNS record currently in `config.json`, with a per-provider summary, live update status (see below), and search/filter by domain or provider.
- **Add / Edit form**, schema-driven across **10 providers**: Cloudflare, Duck DNS, NoIP, Name.com, DigitalOcean, GoDaddy, Dynu, Porkbun, Namecheap, and OVH (DynHost mode). Anything outside these still works via the Advanced tab.
- **Live "Test Connection"** for Cloudflare, DigitalOcean, GoDaddy, and Porkbun — an actual read-only API call before you save, so a bad token surfaces immediately instead of days later. (Not offered for providers whose only API call *is* a real DNS update — testing those would trigger a live change as a side effect.)
- **Update status** — reads `ddns-updater`'s own `updates.json` and shows last-known IP/time per record, best-effort (that file's schema isn't officially documented; a raw-JSON debug panel is included in case it needs adjusting for your version).
- **Automatic backups** — every save backs up the previous `config.json` first (kept last 10, configurable), with one-click **Restore** and **Download** per backup.
- **Optional login** — set up a password through the app itself (`/setup`) or via an `EDITOR_PASSWORD` env var (which also works as a permanent recovery credential if you forget a GUI-set password). Includes login lockout after repeated failures and secure session cookie flags.
- **CSRF protection** on every state-changing action.
- **Activity log** — tracks record changes, backup restores, logins, and password changes.
- **Advanced (Raw JSON) tab** — full manual control, always available as a fallback.
- **Safe writes** — config is written to a temp file and atomically renamed into place, so a crash mid-save can't corrupt your live config.
- **`GET /healthz`** for container orchestrators / uptime monitors.
- **Multi-arch image** — published for `linux/amd64` and `linux/arm64`, so it runs on a Raspberry Pi as happily as a NAS or VM.
- **Tested in CI** — a 28-case pytest suite runs on every release before anything is built, plus a post-build smoke test that actually runs the container and hits `/healthz` before it's pushed to Docker Hub.

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

| Volume | Purpose |
|---|---|
| `/updater/data` | Must point at the **same** path/volume as `ddns-updater`'s `/updater/data`. Also where this app stores `auth.json` (password hash), `backups/`, and `activity.log`. |

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
├── PROVIDER_BACKLOG.md       # remaining ~35 ddns-updater providers not yet schema'd
├── .github/workflows/docker-publish.yml
├── docs/
│   └── REVERSE_PROXY.md      # HTTPS via Nginx Proxy Manager / Caddy / Traefik
├── tests/
│   └── test_app.py           # 28-case pytest suite, runs in CI before every build
└── app/
    ├── app.py
    ├── templates/
    │   ├── index.html        # Records / Add-Edit / Advanced / Backups / Activity tabs
    │   ├── login.html
    │   └── setup.html
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

## License

*(add your license here, e.g. MIT)*
