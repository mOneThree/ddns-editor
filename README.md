# DDNS Editor

A tiny web UI for editing [qdm12/ddns-updater](https://github.com/qdm12/ddns-updater)'s `config.json` — no more hand-editing JSON over SSH just to update a token or swap a domain.

It's built to sit alongside `ddns-updater` in the same Docker Compose stack, sharing its config volume, so changes you make here take effect the next time `ddns-updater` restarts.

![Python](https://img.shields.io/badge/python-3.12-blue)
![Flask](https://img.shields.io/badge/flask-3.0-black)
![Docker](https://img.shields.io/badge/docker-multi--arch-2496ED)

---

## Features

- **Simple mode** — a friendly form for the two most common providers, [Cloudflare](https://www.cloudflare.com/) and [Duck DNS](https://www.duckdns.org/), covering zone ID, API token, proxy toggle, and TTL for Cloudflare, and token for Duck DNS.
- **Advanced mode** — a raw JSON editor for any of the ~50 providers `ddns-updater` supports, or for multi-domain/multi-provider setups Simple mode doesn't cover.
- **Safe writes** — config is written to a temp file and atomically renamed into place, so a crash mid-save can't corrupt your live config.
- **Existing tokens preserved** — re-saving the same provider in Simple mode keeps your existing token if you leave the field blank.
- **Health check endpoint** — `GET /healthz` for container orchestrators / uptime monitors.
- **Multi-arch image** — published for `linux/amd64` and `linux/arm64`, so it runs on a Raspberry Pi as happily as a NAS or VM.

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

Then open **http://localhost:5001**.

> After saving a config change, restart `ddns-updater` (`docker restart ddns-updater`) for it to pick up the new settings.

---

## Configuration

| Environment variable | Required | Default | Description |
|---|---|---|---|
| `FLASK_SECRET_KEY` | No | random on each restart | Used to sign flash-message cookies. Set this if you want flash messages to survive a container restart. |

| Volume | Purpose |
|---|---|
| `/updater/data` | Must point at the **same** path/volume as `ddns-updater`'s `/updater/data`, so both containers read and write the identical `config.json`. |

---

## Building locally

```bash
git clone https://github.com/mOneThree/ddns-editor.git
cd ddns-editor
docker build -t ddns-editor:local .
docker run -p 5001:5000 -v $(pwd)/data:/updater/data ddns-editor:local
```

### Multi-arch build

```bash
docker buildx create --use --name ddns-builder   # one-time
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t mthirteenz/ddns-editor:latest \
  --push .
```

This repo also ships a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that does this automatically on every push to `main` or version tag (`vX.Y.Z`), publishing to Docker Hub as `mthirteenz/ddns-editor`.

---

## Project structure

```
.
├── Dockerfile
├── requirements.txt
├── README.md
├── .github/workflows/docker-publish.yml
└── app/
    ├── app.py            # Flask routes: index, update_simple, update, healthz
    ├── templates/
    │   └── index.html    # Simple + Advanced tabs
    └── static/
```

---

## Security notes

- This container **runs as root**. It shares a bind-mounted volume with `ddns-updater`, and pinning a non-root UID risked permission mismatches against whatever UID that image writes as. Acceptable for a small, internal-only homelab tool — but **do not expose this directly to the internet**. Put it behind your reverse proxy / VPN / internal network only, since it has no authentication of its own.
- API tokens (Cloudflare, Duck DNS) are stored in plaintext in `config.json`, matching `ddns-updater`'s own expectations. Treat the `data/` volume as sensitive.

---

## Supported providers (Simple mode)

| Provider | Fields |
|---|---|
| Cloudflare | Domain, Zone ID, API Token, Proxied (on/off), TTL |
| Duck DNS | Domain, Token |

Need a different provider? Use the **Advanced** tab to hand-write the JSON entry — see [ddns-updater's provider docs](https://github.com/qdm12/ddns-updater#configuration) for the exact shape each one expects.

---

## License

*(add your license here, e.g. MIT)*
