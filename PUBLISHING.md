# Publishing `ddns-editor` to Docker Hub

Two things are included here:

1. `.github/workflows/docker-publish.yml` — automates the build/push on every
   push to `main` or every version tag (e.g. `v1.0.0`), and builds for both
   `linux/amd64` and `linux/arm64` (so it runs on a Pi too).
2. `compose/ddns/docker-compose.yml` — updated to `image: yourusername/ddns-editor:latest`
   instead of `build: ../../ddns-editor`, so any environment can deploy it
   with just this one compose file, no source needed.

Everywhere you see `yourusername`, replace it with your real Docker Hub
username/namespace (in both the workflow file's `IMAGE_NAME` env var and the
compose file's `image:` line).

## One-time setup

1. **Create the Docker Hub repo** (or just let the first push create it
   automatically — Docker Hub does this for you):
   - Go to https://hub.docker.com → Repositories → Create Repository
   - Name it `ddns-editor`, visibility public or private, your call.

2. **Create a Docker Hub access token** (don't use your account password):
   - https://hub.docker.com/settings/security → New Access Token
   - Scope: Read & Write. Copy the token, you won't see it again.

3. **Add two GitHub repo secrets** (Settings → Secrets and variables →
   Actions → New repository secret):
   - `DOCKERHUB_USERNAME` — your Docker Hub username
   - `DOCKERHUB_TOKEN` — the access token from step 2

4. **Drop these files into your repo** at the matching paths:
   - `.github/workflows/docker-publish.yml`
   - (compose file already lives at `compose/ddns/docker-compose.yml`)

5. **Edit the placeholder**: in
   `.github/workflows/docker-publish.yml`, change
   `IMAGE_NAME: yourusername/ddns-editor` to your real namespace.

That's it — push to `main`, or tag a release (`git tag v1.0.0 && git push --tags`),
and GitHub Actions builds + pushes the image for you.

## Manual push (no GitHub Actions, one-off)

If you'd rather just push once by hand from your machine:

```bash
cd ddns-editor

# Log in (only needed once per machine, or after token expiry)
docker login -u yourusername

# Build for your current architecture only
docker build -t yourusername/ddns-editor:1.0.0 -t yourusername/ddns-editor:latest .

# Push both tags
docker push yourusername/ddns-editor:1.0.0
docker push yourusername/ddns-editor:latest
```

### Multi-arch manual push (amd64 + arm64, e.g. for Raspberry Pi)

```bash
docker buildx create --use --name ddns-builder   # one-time
cd ddns-editor
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t yourusername/ddns-editor:1.0.0 \
  -t yourusername/ddns-editor:latest \
  --push .
```

## Deploying elsewhere

On any other machine, all you need is `compose/ddns/docker-compose.yml`
(the source code isn't required):

```bash
mkdir -p ddns && cd ddns
# copy docker-compose.yml here
docker network create homelab_network   # if it doesn't already exist
docker compose up -d
```

## Notes carried over from the original Dockerfile

- The image runs as **root** intentionally, since it shares a bind-mounted
  volume with `ddns-updater` and pinning a non-root UID risked permission
  mismatches against whatever UID that image writes as. Fine for a small
  internal-only homelab tool, but worth knowing if you ever expose this
  beyond your LAN.
- `ddns-updater` itself (`qmcgaw/ddns-updater`) is already a public image —
  nothing to build or publish there, it's pulled as-is.
