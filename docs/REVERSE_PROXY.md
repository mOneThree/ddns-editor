# Putting ddns-editor behind HTTPS

By default, ddns-editor serves plain HTTP. That's fine on a fully trusted
LAN, but it means:

- Your login password crosses the network in cleartext
- Provider tokens/secrets in the Advanced JSON tab do too
- The session cookie could be intercepted on the same network segment

If you access this editor from anywhere other than `localhost` on a
machine you trust completely, put a reverse proxy with TLS in front of it.

After you do, also set this in your `docker-compose.yml` so the session
cookie gets the `Secure` flag (browsers refuse to send `Secure` cookies over
plain HTTP, so **do not** set this until HTTPS is actually working end to
end, or you'll lock yourself out):

```yaml
environment:
  - SESSION_COOKIE_SECURE=true
```

If your reverse proxy is on a different Docker network/host than
ddns-editor and forwards the real client IP via `X-Forwarded-For`, also set:

```yaml
environment:
  - TRUST_PROXY_HEADERS=true
```

(Only enable this if you're actually behind a proxy that sets/overwrites
this header -- otherwise a client can spoof it and dodge the login lockout.)

---

## Option A: Nginx Proxy Manager (NPM)

Assumes NPM is already running (its own container) and both NPM and
ddns-editor are Docker containers.

**1. Put both containers on the same Docker network**, so NPM can reach
`ddns-editor` by container name instead of needing a published host port:

```yaml
services:
  ddns-editor:
    image: mthirteenz/ddns-editor:latest
    container_name: ddns-editor
    restart: unless-stopped
    # no need to publish 5001:5000 externally anymore if NPM is the only
    # thing that needs to reach it -- but keep it if you also want direct
    # LAN access without going through the proxy
    environment:
      - SESSION_COOKIE_SECURE=true
      - TRUST_PROXY_HEADERS=true
    networks:
      - npm_network   # same network NPM is on

networks:
  npm_network:
    external: true    # the network NPM's compose file created
```

**2. In the Nginx Proxy Manager UI:**
- **Proxy Hosts → Add Proxy Host**
- **Domain Names**: `ddns.yourdomain.com` (a subdomain you control, pointed at your public IP/NPM host)
- **Scheme**: `http`
- **Forward Hostname/IP**: `ddns-editor` (the container name -- NPM can resolve it since they share a network)
- **Forward Port**: `5000` (the container's internal port, not the host-published `5001`)
- Enable **Block Common Exploits**
- **Websockets Support**: not needed for this app, leave off

**3. SSL tab** (still in the same Proxy Host dialog):
- **SSL Certificate**: Request a new Let's Encrypt certificate
- Enable **Force SSL** and **HTTP/2 Support**
- Save

**4. Verify**: visit `https://ddns.yourdomain.com` -- you should get a valid
padlock and reach the login page.

---

## Option B: Caddy

If you'd rather run a lightweight standalone proxy, Caddy auto-provisions
Let's Encrypt certs with almost no config:

```
# Caddyfile
ddns.yourdomain.com {
    reverse_proxy ddns-editor:5000
}
```

```yaml
# docker-compose.yml (Caddy service)
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddyfile
      - caddy_data:/data
    networks:
      - npm_network  # same network as ddns-editor

volumes:
  caddy_data:
```

---

## Option C: Traefik (if you're already running it)

Add labels to the `ddns-editor` service instead of a separate proxy config:

```yaml
services:
  ddns-editor:
    image: mthirteenz/ddns-editor:latest
    environment:
      - SESSION_COOKIE_SECURE=true
      - TRUST_PROXY_HEADERS=true
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.ddns-editor.rule=Host(`ddns.yourdomain.com`)"
      - "traefik.http.routers.ddns-editor.entrypoints=websecure"
      - "traefik.http.routers.ddns-editor.tls.certresolver=letsencrypt"
      - "traefik.http.services.ddns-editor.loadbalancer.server.port=5000"
    networks:
      - traefik_network

networks:
  traefik_network:
    external: true
```

---

## Sanity checklist after setting this up

- [ ] `https://your-domain` loads with a valid certificate (no browser warning)
- [ ] Logging in works (if it silently fails / redirects in a loop, `SESSION_COOKIE_SECURE=true` was set before HTTPS was actually working -- unset it, confirm plain login works, then re-enable once HTTPS is confirmed)
- [ ] Plain `http://your-domain` either redirects to HTTPS or is blocked entirely
- [ ] The **Activity** tab shows your own login attempts with the IP you expect (confirms `TRUST_PROXY_HEADERS` is working correctly, not just parroting the proxy's own IP for every request)
