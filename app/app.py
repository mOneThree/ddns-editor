import base64
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, Response

from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
# Required for flash messages and login sessions. Falls back to a random
# value each restart if the operator hasn't set one -- fine as long as you
# don't mind being logged out on restart.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

# Secure session cookie flags. SESSION_COOKIE_SECURE defaults to False
# because it *breaks login entirely* if the browser is talking to this app
# over plain HTTP (the browser silently refuses to send a Secure cookie
# over an insecure connection) -- only flip it on once you've put this
# behind HTTPS (see docs/REVERSE_PROXY.md), via SESSION_COOKIE_SECURE=true.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
)

CONFIG_PATH = "/updater/data/config.json"
UPDATES_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "updates.json")
BACKUP_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "backups")
AUTH_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "auth.json")
ACTIVITY_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "activity.log")
API_TOKENS_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "api_tokens.json")
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "10"))
MAX_ACTIVITY_ENTRIES = 500
_BACKUP_NAME_RE = re.compile(r"^config-\d{8}T\d{6}Z\.json$")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_FORMAT = os.environ.get("WEBHOOK_FORMAT", "generic").lower()  # generic | discord | slack | ntfy


# ---------------------------------------------------------------------------
# Activity log: a simple append-only record of who changed what and when.
# Stored as newline-delimited JSON so it can't be corrupted by a partial
# write the way a single big JSON array could be, and so it's trivially
# appendable without reading the whole file back in.
# ---------------------------------------------------------------------------

def log_activity(action, detail=""):
    try:
        entry = {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "ip": _client_ip(),
            "action": action,
            "detail": detail,
        }
        with open(ACTIVITY_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _trim_activity_log()
    except OSError:
        pass  # never let logging break the actual operation

    if action in _WEBHOOK_NOTIFY_ACTIONS:
        _send_webhook(action, detail)


_WEBHOOK_NOTIFY_ACTIONS = {
    "record_added", "record_updated", "record_deleted", "backup_restored",
    "password_changed", "password_setup", "user_added", "user_deleted",
    "user_role_changed", "2fa_enabled", "2fa_disabled", "api_token_created",
    "api_token_revoked",
}


def _send_webhook(action, detail):
    if not WEBHOOK_URL:
        return
    message = f"ddns-editor: {action.replace('_', ' ')}" + (f" ({detail})" if detail else "")
    try:
        if WEBHOOK_FORMAT == "discord":
            body = {"content": message}
        elif WEBHOOK_FORMAT == "slack":
            body = {"text": message}
        elif WEBHOOK_FORMAT == "ntfy":
            # ntfy accepts a raw text body, not JSON.
            req = urllib.request.Request(WEBHOOK_URL, data=message.encode("utf-8"), method="POST")
            urllib.request.urlopen(req, timeout=5)
            return
        else:
            body = {"event": action, "detail": detail, "time": datetime.now(timezone.utc).isoformat()}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # a notification failure must never break the actual operation


def _trim_activity_log():
    if not os.path.exists(ACTIVITY_PATH):
        return
    try:
        with open(ACTIVITY_PATH, "r") as f:
            lines = f.readlines()
        if len(lines) > MAX_ACTIVITY_ENTRIES:
            with open(ACTIVITY_PATH, "w") as f:
                f.writelines(lines[-MAX_ACTIVITY_ENTRIES:])
    except OSError:
        pass


def load_activity(limit=100):
    if not os.path.exists(ACTIVITY_PATH):
        return []
    try:
        with open(ACTIVITY_PATH, "r") as f:
            lines = f.readlines()
    except OSError:
        return []
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()  # most recent first
    return entries


# ---------------------------------------------------------------------------
# Client IP helper. Only trusts X-Forwarded-For if TRUST_PROXY_HEADERS is
# explicitly enabled -- if this app is reachable directly (no reverse
# proxy in front stripping/setting that header), trusting it blindly would
# let an attacker spoof a different IP on every request and bypass the
# login lockout below entirely.
# ---------------------------------------------------------------------------

TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").lower() == "true"


def _client_ip():
    if TRUST_PROXY_HEADERS:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Login lockout. In-memory only (resets on container restart) -- this is a
# single-container homelab tool, not a fleet, so a shared store like Redis
# would be overkill. Keyed by client IP (see _client_ip above).
# ---------------------------------------------------------------------------

LOCKOUT_THRESHOLD = int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "5"))
LOCKOUT_WINDOW_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_WINDOW_SECONDS", "300"))
_failed_attempts = defaultdict(list)


def _is_locked_out(ip):
    now = time.time()
    recent = [t for t in _failed_attempts[ip] if now - t < LOCKOUT_WINDOW_SECONDS]
    _failed_attempts[ip] = recent
    return len(recent) >= LOCKOUT_THRESHOLD


def _record_failed_login(ip):
    _failed_attempts[ip].append(time.time())


def _clear_failed_logins(ip):
    _failed_attempts.pop(ip, None)


def _lockout_seconds_remaining(ip):
    if not _failed_attempts[ip]:
        return 0
    oldest_relevant = min(_failed_attempts[ip])
    remaining = LOCKOUT_WINDOW_SECONDS - (time.time() - oldest_relevant)
    return max(0, int(remaining))


# ---------------------------------------------------------------------------
# CSRF protection. Deliberately hand-rolled (a session-bound random token,
# required on every state-changing request) rather than pulling in
# Flask-WTF, to keep the image's dependency footprint at just Flask.
# ---------------------------------------------------------------------------

def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def ensure_csrf_token():
    get_csrf_token()  # establishes session['csrf_token'] if not already present


@app.before_request
def csrf_protect():
    if (request.path or "").startswith("/api/"):
        return  # token-authenticated, not cookie/session-based -- not CSRF-vulnerable
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        token = session.get("csrf_token")
        submitted = request.form.get("csrf_token", "")
        if not token or not submitted or not hmac.compare_digest(token, submitted):
            abort(400, description="Your session token expired or is invalid. Please refresh the page and try again.")

# ---------------------------------------------------------------------------
# Authentication.
#
# Two independent ways to set a password, checked in this priority order:
#
#   1. EDITOR_PASSWORD environment variable ("env mode"). Set by the
#      operator in docker-compose.yml. Always checked first -- it keeps
#      working even after a GUI password is set, as a recovery credential
#      in case the GUI password is forgotten.
#   2. A password set through the app itself at /setup, stored as a salted
#      hash in auth.json on the data volume ("gui mode"). This is the
#      day-to-day password once set.
#
# If neither is set, the editor stays fully open (matches all previous
# versions, so existing deployments never suddenly lock themselves out) --
# the UI shows a banner nudging the operator to secure it either way.
# ---------------------------------------------------------------------------

EDITOR_USERNAME = os.environ.get("EDITOR_USERNAME", "admin")
EDITOR_PASSWORD = os.environ.get("EDITOR_PASSWORD", "")


def load_auth():
    """Returns {"users": [...]}. Transparently migrates the old
    single-user format ({"username":..., "password_hash":...}) used by
    versions before multi-user support existed, so upgrading never locks
    out an existing GUI-configured user."""
    if not os.path.exists(AUTH_PATH):
        return None
    try:
        with open(AUTH_PATH, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if "users" not in data and "password_hash" in data:
        migrated = {
            "users": [{
                "username": data.get("username", "admin"),
                "password_hash": data["password_hash"],
                "role": "admin",
                "totp_secret": None,
                "totp_enabled": False,
            }]
        }
        save_auth(migrated)
        return migrated

    return data


def save_auth(data):
    os.makedirs(os.path.dirname(AUTH_PATH), exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, AUTH_PATH)


def find_user(username):
    auth = load_auth()
    if not auth:
        return None
    for u in auth.get("users", []):
        if hmac.compare_digest(u["username"], username):
            return u
    return None


def update_user(username, **changes):
    auth = load_auth() or {"users": []}
    for u in auth["users"]:
        if u["username"] == username:
            u.update(changes)
            break
    save_auth(auth)


def env_auth_configured():
    return bool(EDITOR_PASSWORD)


def gui_auth_configured():
    auth = load_auth()
    return bool(auth and auth.get("users"))


def auth_configured():
    return env_auth_configured() or gui_auth_configured()


def current_user():
    """The logged-in user's record, or a synthetic admin record for the
    EDITOR_PASSWORD env-var login (which always carries admin rights --
    it's a single shared operator credential, not a per-person account)."""
    if session.get("auth_via") == "env":
        return {"username": EDITOR_USERNAME, "role": "admin"}
    username = session.get("username")
    return find_user(username) if username else None


def current_role():
    user = current_user()
    return user["role"] if user else None


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if auth_configured() and current_role() != "admin":
            abort(403, description="Admin role required.")
        return view(*args, **kwargs)
    return wrapped


def require_write(view):
    """Blocks readonly-role users from state-changing routes. Read-only
    actions (viewing records, running a live connection test, downloading
    a backup) stay available to them."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if auth_configured() and current_role() == "readonly":
            abort(403, description="Read-only accounts can't make changes.")
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) two-factor auth, implemented directly with hashlib/hmac/
# struct rather than pulling in a dependency -- the algorithm is short and
# stable, and this keeps the image's dependency footprint at just Flask.
# ---------------------------------------------------------------------------

def generate_totp_secret():
    return base64.b32encode(os.urandom(20)).decode("utf-8").rstrip("=")


def _totp_code(secret, for_time, step=30, digits=6):
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded)
    counter = int(for_time // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)


def verify_totp(secret, code, step=30, digits=6, window=1):
    if not code or not code.isdigit():
        return False
    now = time.time()
    for offset in range(-window, window + 1):
        expected = _totp_code(secret, now + offset * step, step, digits)
        if hmac.compare_digest(expected, code):
            return True
    return False


def totp_uri(secret, username, issuer="DDNS Editor"):
    return f"otpauth://totp/{issuer}:{username}?secret={secret}&issuer={issuer}"


# ---------------------------------------------------------------------------
# Provider schema registry (see previous version's comments for the design
# rationale). "test" optionally names a live-credential-check function from
# PROVIDER_TEST_FUNCS below -- only wired up for providers with a read-only
# API endpoint, so testing never triggers a real DNS update as a side effect.
# ---------------------------------------------------------------------------

IP_VERSION_FIELD = {
    "name": "ip_version", "label": "IP version", "type": "select",
    "options": ["ipv4", "ipv6", "ipv4 or ipv6"], "default": "ipv4 or ipv6",
    "summary": False,
}
IPV6_SUFFIX_FIELD = {
    "name": "ipv6_suffix", "label": "IPv6 interface suffix", "type": "text",
    "default": "", "summary": False,
    "help": "Optional, e.g. 0:0:0:0:72ad:8fbb:a54e:bedd/64. Leave blank to use the raw temporary IPv6 address.",
}

PROVIDER_SCHEMAS = {
    "cloudflare": {
        "label": "Cloudflare", "test": "cloudflare",
        "fields": [
            {"name": "zone_identifier", "label": "Zone ID", "type": "text", "required": True, "short": "Zone"},
            {"name": "token", "label": "API Token (Zone:DNS:Edit scope)", "type": "secret", "required": True},
            {"name": "ttl", "label": "TTL (seconds)", "type": "number", "default": 300, "short": "TTL"},
            {"name": "proxied", "label": "Proxy through Cloudflare (orange cloud)", "type": "checkbox", "short": "Proxied"},
        ],
    },
    "duckdns": {
        "label": "Duck DNS",
        "fields": [
            {"name": "token", "label": "Token", "type": "secret", "required": True},
        ],
    },
    "noip": {
        "label": "NoIP",
        "fields": [
            {"name": "username", "label": "Username", "type": "text", "required": True, "short": "User"},
            {"name": "password", "label": "Password", "type": "secret", "required": True},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "name.com": {
        "label": "Name.com",
        "fields": [
            {"name": "username", "label": "Username", "type": "text", "required": True, "short": "User"},
            {"name": "token", "label": "API Token", "type": "secret", "required": True},
            {"name": "ttl", "label": "TTL (seconds, min 300)", "type": "number", "default": 300, "short": "TTL"},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "digitalocean": {
        "label": "DigitalOcean", "test": "digitalocean",
        "fields": [
            {"name": "token", "label": "API Token", "type": "secret", "required": True,
             "help": "Create one at cloud.digitalocean.com/settings/applications"},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "godaddy": {
        "label": "GoDaddy", "test": "godaddy",
        "fields": [
            {"name": "key", "label": "API Key", "type": "secret", "required": True,
             "help": "Create at developer.godaddy.com/keys"},
            {"name": "secret", "label": "API Secret", "type": "secret", "required": True},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "dynu": {
        "label": "Dynu",
        "fields": [
            {"name": "group", "label": "Group (optional)", "type": "text", "default": "", "short": "Group"},
            {"name": "username", "label": "Username", "type": "text", "required": True, "short": "User"},
            {"name": "password", "label": "Password", "type": "secret", "required": True,
             "help": "Plain text, MD5, or SHA256 -- or a dedicated IP-update password from Dynu."},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "porkbun": {
        "label": "Porkbun", "test": "porkbun",
        "fields": [
            {"name": "api_key", "label": "API Key", "type": "secret", "required": True},
            {"name": "secret_api_key", "label": "Secret API Key", "type": "secret", "required": True},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "namecheap": {
        "label": "Namecheap",
        "fields": [
            {"name": "password", "label": "Dynamic DNS Password", "type": "secret", "required": True,
             "help": "Found in Namecheap's Advanced DNS tab for the domain, not your account password."},
        ],
    },
    "ovh": {
        "label": "OVH (DynHost)",
        "fields": [
            {"name": "username", "label": "Username", "type": "text", "required": True, "short": "User"},
            {"name": "password", "label": "Password", "type": "secret", "required": True},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
        "note": "This form covers OVH's DynHost (dynamic) mode only. OVH's API mode "
                "(app_key/app_secret/consumer_key) isn't supported here -- use the Advanced tab for that.",
    },
    "custom": {
        "label": "Custom URL",
        "fields": [
            {"name": "url", "label": "Update URL", "type": "text", "required": True, "short": "URL",
             "help": "The URL to call, without the IP address -- ddns-updater appends it using the query param names below."},
            {"name": "ipv4key", "label": "IPv4 query param name", "type": "text", "default": "ipv4"},
            {"name": "ipv6key", "label": "IPv6 query param name", "type": "text", "default": "ipv6"},
            {"name": "success_regex", "label": "Success response pattern", "type": "text", "default": ""},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
}


# ---------------------------------------------------------------------------
# Live connection testing. Only wired up for providers with a read-only,
# side-effect-free API call -- NoIP/DuckDNS/Dynu/OVH/Namecheap/etc. don't
# have one without actually performing a real DNS update, so they're left
# untested rather than risk changing a live record just to "check" it.
# ---------------------------------------------------------------------------

def _http(url, method="GET", headers=None, body=None, timeout=8):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _error_message(exc, fallback):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            data = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            return f"{fallback}: HTTP {exc.code} {exc.reason}"
        for key in ("message", "error", "errors"):
            if key in data:
                val = data[key]
                if isinstance(val, list) and val:
                    val = val[0].get("message", val[0]) if isinstance(val[0], dict) else val[0]
                return f"{fallback}: {val}"
        return f"{fallback}: HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"{fallback}: could not reach the API ({exc.reason})"
    return f"{fallback}: unexpected error ({exc})"


def test_cloudflare(fields, domain):
    token, zone = fields.get("token", ""), fields.get("zone_identifier", "")
    if not token or not zone:
        return False, "Zone ID and API Token are both required to test."
    try:
        _, body = _http(f"https://api.cloudflare.com/client/v4/zones/{zone}",
                         headers={"Authorization": f"Bearer {token}"})
        data = json.loads(body)
        if data.get("success"):
            name = data.get("result", {}).get("name", zone)
            return True, f"Connected -- zone found: {name}"
        errors = data.get("errors") or [{"message": "unknown error"}]
        return False, f"Cloudflare rejected the request: {errors[0].get('message', 'unknown error')}"
    except Exception as e:
        return False, _error_message(e, "Cloudflare test failed")


def test_digitalocean(fields, domain):
    token = fields.get("token", "")
    if not token:
        return False, "API Token is required to test."
    try:
        _, body = _http("https://api.digitalocean.com/v2/account",
                         headers={"Authorization": f"Bearer {token}"})
        data = json.loads(body)
        email = data.get("account", {}).get("email", "")
        return True, f"Connected -- token valid{' for ' + email if email else ''}."
    except Exception as e:
        return False, _error_message(e, "DigitalOcean test failed")


def test_godaddy(fields, domain):
    key, secret = fields.get("key", ""), fields.get("secret", "")
    if not key or not secret:
        return False, "API Key and Secret are both required to test."
    if not domain:
        return False, "Enter the domain above before testing."
    try:
        _, body = _http(f"https://api.godaddy.com/v1/domains/{domain}",
                         headers={"Authorization": f"sso-key {key}:{secret}"})
        data = json.loads(body)
        status = data.get("status", "unknown")
        return True, f"Connected -- domain found, status: {status}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "Credentials look valid, but this domain wasn't found in this GoDaddy account."
        return False, _error_message(e, "GoDaddy test failed")
    except Exception as e:
        return False, _error_message(e, "GoDaddy test failed")


def test_porkbun(fields, domain):
    api_key, secret_key = fields.get("api_key", ""), fields.get("secret_api_key", "")
    if not api_key or not secret_key:
        return False, "Both API keys are required to test."
    try:
        _, body = _http("https://api.porkbun.com/api/json/v3/ping", method="POST",
                         body={"apikey": api_key, "secretapikey": secret_key})
        data = json.loads(body)
        if data.get("status") == "SUCCESS":
            return True, f"Connected -- Porkbun sees your IP as {data.get('yourIp', 'unknown')}."
        return False, f"Porkbun rejected the request: {data.get('message', 'unknown error')}"
    except Exception as e:
        return False, _error_message(e, "Porkbun test failed")


PROVIDER_TEST_FUNCS = {
    "cloudflare": test_cloudflare,
    "digitalocean": test_digitalocean,
    "godaddy": test_godaddy,
    "porkbun": test_porkbun,
}


# ---------------------------------------------------------------------------
# Config load/save + automatic backups
# ---------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {"settings": []}


def _backup_current_config():
    if not os.path.exists(CONFIG_PATH):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(CONFIG_PATH, os.path.join(BACKUP_DIR, f"config-{ts}.json"))
        _prune_backups()
    except OSError:
        pass  # backups are a safety net, not critical -- never block a save on this


def _prune_backups():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "config-*.json")))
    for f in files[: max(len(files) - MAX_BACKUPS, 0)]:
        try:
            os.remove(f)
        except OSError:
            pass


def save_config(config):
    _backup_current_config()
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=4)
    os.replace(tmp_path, CONFIG_PATH)


def list_backups():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "config-*.json")), reverse=True)
    backups = []
    for path in files:
        name = os.path.basename(path)
        m = re.match(r"^config-(\d{8})T(\d{6})Z\.json$", name)
        when = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]} UTC" if m else name
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        backups.append({"filename": name, "when": when, "size": size})
    return backups


# ---------------------------------------------------------------------------
# Best-effort update-status reader.
#
# ddns-updater's updates.json schema isn't officially documented, so rather
# than assume exact key names (and risk silently showing wrong data), this
# walks the JSON looking for any dict that mentions the domain alongside
# something IP-shaped or time-shaped. Worst case it finds nothing and the
# UI shows "no update history yet" -- it never blocks or breaks the page.
# A raw-JSON viewer is included in the UI so this can be verified/refined
# against a real updates.json if the display looks wrong.
# ---------------------------------------------------------------------------

_DOMAIN_KEYS = ("domain", "owner", "host", "hostname")
_IP_KEYS = ("ip", "ipv4", "ipv6", "current_ip", "currentip")
_TIME_KEYS = ("time", "updated", "updated_at", "last_update", "success_time", "when")


def load_updates_raw():
    if not os.path.exists(UPDATES_PATH):
        return None
    try:
        with open(UPDATES_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _parse_time_value(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _format_time(value):
    dt = _parse_time_value(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else str(value)


def _relative_time(dt):
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def find_status_for_domain(updates_data, domain):
    if not updates_data or not domain:
        return None
    found = {}

    def walk(node):
        if found:
            return
        if isinstance(node, dict):
            domain_val = next((node[k] for k in _DOMAIN_KEYS if isinstance(node.get(k), str)), None)
            if domain_val and (domain_val == domain or domain_val in domain or domain in domain_val):
                ip_val = next((node[k] for k in _IP_KEYS if k in node), None)
                time_val = next((node[k] for k in _TIME_KEYS if k in node), None)
                if ip_val or time_val:
                    found["ip"] = ip_val
                    found["time"] = _format_time(time_val) if time_val else None
                    found["time_dt"] = _parse_time_value(time_val) if time_val else None
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    try:
        walk(updates_data)
    except Exception:
        return None
    return found or None


# ---------------------------------------------------------------------------
# Provider form helpers (unchanged design from previous version)
# ---------------------------------------------------------------------------

def _truncate(text, length=16):
    text = str(text)
    return text if len(text) <= length else text[: length - 1] + "…"


def summarize(entry):
    provider = entry.get("provider", "unknown")
    schema = PROVIDER_SCHEMAS.get(provider)
    if not schema:
        keys = [k for k in entry.keys() if k not in ("provider", "domain", "token", "password", "secret", "key")]
        return ("Custom provider (" + ", ".join(keys) + ")") if keys else "Custom provider"
    parts = []
    for field in schema["fields"]:
        if field["type"] == "secret" or field.get("summary") is False:
            continue
        value = entry.get(field["name"])
        if value in (None, ""):
            continue
        label = field.get("short", field["label"])
        if field["type"] == "checkbox":
            parts.append(f"{label}: {'Yes' if value else 'No'}")
        else:
            parts.append(f"{label}: {_truncate(value)}")
    return " • ".join(parts) if parts else schema["label"]


def build_entry(provider, domain, form, existing):
    schema = PROVIDER_SCHEMAS[provider]
    entry = {"provider": provider, "domain": domain}
    preserve_secrets = existing.get("provider") == provider

    for field in schema["fields"]:
        name = field["name"]
        ftype = field["type"]
        form_key = f"{provider}__{name}"

        if ftype == "checkbox":
            entry[name] = form.get(form_key) == "on"
            continue

        raw = form.get(form_key, "").strip()

        if ftype == "secret":
            value = raw or (existing.get(name, "") if preserve_secrets else "")
        elif ftype == "number":
            raw = raw or str(field.get("default", 0))
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{field['label']} must be a whole number.")
        else:
            value = raw or field.get("default", "")

        if field.get("required") and not value:
            raise ValueError(f"{field['label']} is required for {schema['label']}.")

        entry[name] = value

    return entry


def _extract_test_fields(provider, form, existing):
    """Same field-collection logic as build_entry, but for the live-test
    endpoint -- doesn't validate/require, just gathers what's there and
    fills in existing secrets for blank fields so testing a saved record
    (without retyping tokens) works."""
    schema = PROVIDER_SCHEMAS[provider]
    preserve_secrets = existing.get("provider") == provider
    fields = {}
    for field in schema["fields"]:
        name = field["name"]
        form_key = f"{provider}__{name}"
        if field["type"] == "checkbox":
            fields[name] = form.get(form_key) == "on"
            continue
        raw = form.get(form_key, "").strip()
        if field["type"] == "secret" and not raw and preserve_secrets:
            raw = existing.get(name, "")
        fields[name] = raw
    return fields


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.before_request
def require_login():
    if request.endpoint in ("healthz", "static") or (request.path or "").startswith("/api/"):
        return  # API routes have their own token-based gate, see below

    if not auth_configured():
        return  # nothing set anywhere -- stay fully open (backward compatible)

    if request.endpoint in ("login", "login_2fa"):
        return

    if request.endpoint == "setup":
        return  # view itself redirects once something's configured

    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = _client_ip()
    if request.method == "POST":
        if _is_locked_out(ip):
            wait = _lockout_seconds_remaining(ip)
            error = f"Too many failed attempts. Try again in about {max(wait // 60, 1)} minute(s)."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            valid, role, auth_via, needs_2fa = False, None, None, False

            if EDITOR_PASSWORD and hmac.compare_digest(username, EDITOR_USERNAME) and hmac.compare_digest(password, EDITOR_PASSWORD):
                valid, role, auth_via = True, "admin", "env"

            if not valid:
                user = find_user(username)
                if user and check_password_hash(user.get("password_hash", ""), password):
                    valid, role, auth_via = True, user["role"], "gui"
                    needs_2fa = bool(user.get("totp_enabled"))

            if valid:
                _clear_failed_logins(ip)
                if needs_2fa:
                    # Password correct, but a second factor is required --
                    # don't grant a session yet, stash a pending state.
                    session["pending_2fa_user"] = username
                    session["pending_2fa_next"] = request.form.get("next") or url_for("index")
                    return redirect(url_for("login_2fa"))
                session["logged_in"] = True
                session["username"] = username
                session["auth_via"] = auth_via
                log_activity("login_success", f"user={username}")
                return redirect(request.form.get("next") or url_for("index"))

            _record_failed_login(ip)
            log_activity("login_failed", f"user={username}")
            time.sleep(1)  # basic throttle in addition to the lockout above
            error = "Invalid username or password."

    return render_template(
        "login.html",
        error=error,
        next=request.args.get("next", ""),
        setup_available=(not env_auth_configured() and not gui_auth_configured()),
    )


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    username = session.get("pending_2fa_user")
    if not username:
        return redirect(url_for("login"))

    error = None
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        user = find_user(username)
        if user and verify_totp(user.get("totp_secret", ""), code):
            session.pop("pending_2fa_user", None)
            next_url = session.pop("pending_2fa_next", url_for("index"))
            session["logged_in"] = True
            session["username"] = username
            session["auth_via"] = "gui"
            log_activity("login_success", f"user={username} (2fa)")
            return redirect(next_url)
        log_activity("login_failed", f"user={username} (bad 2fa code)")
        time.sleep(1)
        error = "Invalid code."

    return render_template("login_2fa.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if env_auth_configured():
        flash("A password is already set via the EDITOR_PASSWORD environment variable.", "info")
        return redirect(url_for("login"))
    if gui_auth_configured():
        return redirect(url_for("login"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip() or "admin"
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            save_auth({"users": [{
                "username": username, "password_hash": generate_password_hash(password),
                "role": "admin", "totp_secret": None, "totp_enabled": False,
            }]})
            session["logged_in"] = True
            session["username"] = username
            session["auth_via"] = "gui"
            log_activity("password_setup", f"user={username}")
            flash("Password set -- you're now logged in as the admin user.", "success")
            return redirect(url_for("index"))

    return render_template("setup.html", error=error)


@app.route("/account/change-password", methods=["POST"])
def change_password():
    if session.get("auth_via") == "env":
        flash("Password is managed via the EDITOR_PASSWORD environment variable; unset it to manage passwords from here instead.", "danger")
        return redirect(url_for("index"))

    username = session.get("username")
    user = find_user(username)
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if not user or not check_password_hash(user.get("password_hash", ""), current):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("index"))
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "danger")
        return redirect(url_for("index"))
    if new != confirm:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("index"))

    update_user(username, password_hash=generate_password_hash(new))
    log_activity("password_changed", f"user={username}")
    flash("Password changed successfully.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Two-factor auth setup (per-user, self-service)
# ---------------------------------------------------------------------------

@app.route("/account/2fa/setup", methods=["GET", "POST"])
def setup_2fa():
    if session.get("auth_via") == "env":
        abort(400, description="2FA isn't available for the environment-variable login.")
    username = session.get("username")
    user = find_user(username)
    if not user:
        abort(400)

    if request.method == "GET":
        secret = generate_totp_secret()
        session["pending_totp_secret"] = secret
        return render_template("setup_2fa.html", secret=secret, uri=totp_uri(secret, username), error=None)

    secret = session.get("pending_totp_secret", "")
    code = request.form.get("code", "").strip()
    if not secret or not verify_totp(secret, code):
        return render_template(
            "setup_2fa.html", secret=secret, uri=totp_uri(secret, username),
            error="That code didn't match. Scan the QR/enter the secret again and try the current code.",
        )

    update_user(username, totp_secret=secret, totp_enabled=True)
    session.pop("pending_totp_secret", None)
    log_activity("2fa_enabled", f"user={username}")
    flash("Two-factor authentication enabled.", "success")
    return redirect(url_for("index"))


@app.route("/account/2fa/disable", methods=["POST"])
def disable_2fa():
    if session.get("auth_via") == "env":
        abort(400)
    username = session.get("username")
    user = find_user(username)
    password = request.form.get("current_password", "")
    if not user or not check_password_hash(user.get("password_hash", ""), password):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("index"))
    update_user(username, totp_secret=None, totp_enabled=False)
    log_activity("2fa_disabled", f"user={username}")
    flash("Two-factor authentication disabled.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@app.route("/users", methods=["GET"])
@require_admin
def users_page():
    auth = load_auth() or {"users": []}
    return render_template(
        "users.html", users=auth.get("users", []), current_username=session.get("username"),
        active_tab="users", auth_configured=auth_configured(), auth_is_gui=(session.get("auth_via") == "gui"),
        current_role=current_role(), is_admin=True,
    )


@app.route("/users/add", methods=["POST"])
@require_admin
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "readonly")
    if role not in ("admin", "readonly"):
        role = "readonly"

    if not username or len(password) < 8:
        flash("Username is required and password must be at least 8 characters.", "danger")
        return redirect(url_for("users_page"))
    if find_user(username):
        flash(f"A user named '{username}' already exists.", "danger")
        return redirect(url_for("users_page"))

    auth = load_auth() or {"users": []}
    auth["users"].append({
        "username": username, "password_hash": generate_password_hash(password),
        "role": role, "totp_secret": None, "totp_enabled": False,
    })
    save_auth(auth)
    log_activity("user_added", f"user={username} role={role}")
    flash(f"User '{username}' added.", "success")
    return redirect(url_for("users_page"))


@app.route("/users/<username>/delete", methods=["POST"])
@require_admin
def delete_user(username):
    auth = load_auth() or {"users": []}
    users = auth.get("users", [])
    if len(users) <= 1:
        flash("Can't delete the only remaining user.", "danger")
        return redirect(url_for("users_page"))
    if username == session.get("username"):
        flash("You can't delete your own account while logged in as it.", "danger")
        return redirect(url_for("users_page"))
    auth["users"] = [u for u in users if u["username"] != username]
    save_auth(auth)
    log_activity("user_deleted", f"user={username}")
    flash(f"User '{username}' deleted.", "success")
    return redirect(url_for("users_page"))


@app.route("/users/<username>/role", methods=["POST"])
@require_admin
def change_user_role(username):
    new_role = request.form.get("role", "readonly")
    if new_role not in ("admin", "readonly"):
        new_role = "readonly"
    auth = load_auth() or {"users": []}
    admins = [u for u in auth.get("users", []) if u["role"] == "admin"]
    if len(admins) == 1 and admins[0]["username"] == username and new_role != "admin":
        flash("Can't demote the only remaining admin.", "danger")
        return redirect(url_for("users_page"))
    update_user(username, role=new_role)
    log_activity("user_role_changed", f"user={username} role={new_role}")
    flash(f"'{username}' is now {new_role}.", "success")
    return redirect(url_for("users_page"))


# ---------------------------------------------------------------------------
# API tokens: a separate, opt-in mechanism from the browser session/CSRF
# system above. Tokens are only usable against /api/v1/* routes, are shown
# once at creation (only the hash is stored), and are exempt from the
# session-login redirect and CSRF check since header-based bearer auth
# isn't vulnerable to CSRF the way cookies are.
# ---------------------------------------------------------------------------

def load_api_tokens():
    if not os.path.exists(API_TOKENS_PATH):
        return {"tokens": []}
    try:
        with open(API_TOKENS_PATH, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"tokens": []}


def save_api_tokens(data):
    os.makedirs(os.path.dirname(API_TOKENS_PATH), exist_ok=True)
    tmp = API_TOKENS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, API_TOKENS_PATH)


def list_api_tokens():
    return load_api_tokens().get("tokens", [])


def create_api_token(label, role="readonly"):
    raw_token = "ddns_" + secrets.token_urlsafe(32)
    data = load_api_tokens()
    data.setdefault("tokens", []).append({
        "label": label or "unnamed",
        "token_hash": hashlib.sha256(raw_token.encode()).hexdigest(),
        "role": role if role in ("admin", "readonly") else "readonly",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
    save_api_tokens(data)
    return raw_token  # only time the raw value is ever available


def revoke_api_token(label):
    data = load_api_tokens()
    data["tokens"] = [t for t in data.get("tokens", []) if t["label"] != label]
    save_api_tokens(data)


def verify_api_token(raw_token):
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    for t in list_api_tokens():
        if hmac.compare_digest(t["token_hash"], token_hash):
            return t["role"]
    return None


def require_api_token(min_role="readonly"):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.startswith("Bearer ") else request.headers.get("X-API-Key", "")
            role = verify_api_token(token)
            if role is None:
                return {"error": "Missing or invalid API token."}, 401
            if min_role == "admin" and role != "admin":
                return {"error": "This action requires an admin-level API token."}, 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route("/api/v1/status")
@require_api_token()
def api_status():
    config = load_config()
    return {"status": "ok", "record_count": len(config.get("settings", []))}


@app.route("/api/v1/records", methods=["GET"])
@require_api_token()
def api_list_records():
    config = load_config()
    settings = config.get("settings", [])
    updates_data = load_updates_raw()
    out = []
    for i, entry in enumerate(settings):
        out.append({
            "index": i,
            "domain": entry.get("domain", ""),
            "provider": entry.get("provider", ""),
            "summary": summarize(entry),
            "status": find_status_for_domain(updates_data, entry.get("domain", "")),
        })
    return {"records": out}


@app.route("/api/v1/records", methods=["POST"])
@require_api_token(min_role="admin")
def api_add_record():
    body = request.get_json(silent=True) or {}
    provider = body.get("provider", "")
    domain = body.get("domain", "")
    if not provider or not domain:
        return {"error": "'provider' and 'domain' are required."}, 400
    if provider not in PROVIDER_SCHEMAS:
        return {"error": f"Unknown provider '{provider}'. Use the Advanced tab / raw config for unsupported providers."}, 400

    # Reuse build_entry's validation by adapting the JSON body into the same
    # "<provider>__<field>" form-shaped dict build_entry expects.
    fake_form = {}
    for field in PROVIDER_SCHEMAS[provider]["fields"]:
        key = f"{provider}__{field['name']}"
        value = body.get(field["name"], "")
        fake_form[key] = "on" if (field["type"] == "checkbox" and value) else str(value)

    try:
        entry = build_entry(provider, domain, fake_form, {})
    except ValueError as e:
        return {"error": str(e)}, 400

    config = load_config()
    config.setdefault("settings", []).append(entry)
    save_config(config)
    log_activity("record_added", f"domain={domain} provider={provider} (via API)")
    return {"status": "created", "index": len(config["settings"]) - 1}, 201


@app.route("/api/v1/records/<int:index>", methods=["DELETE"])
@require_api_token(min_role="admin")
def api_delete_record(index):
    config = load_config()
    settings = config.get("settings", [])
    if not (0 <= index < len(settings)):
        return {"error": "Record not found."}, 404
    removed = settings.pop(index)
    config["settings"] = settings
    save_config(config)
    log_activity("record_deleted", f"domain={removed.get('domain', 'unknown')} (via API)")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# API token management UI (admin only)
# ---------------------------------------------------------------------------

@app.route("/api-tokens", methods=["GET"])
@require_admin
def api_tokens_page():
    return render_template(
        "api_tokens.html", tokens=list_api_tokens(), new_token=session.pop("new_api_token", None),
        active_tab="apitokens", auth_configured=auth_configured(), auth_is_gui=(session.get("auth_via") == "gui"),
        current_role=current_role(), current_username=session.get("username"), is_admin=True,
    )


@app.route("/api-tokens/create", methods=["POST"])
@require_admin
def create_api_token_route():
    label = request.form.get("label", "").strip()
    role = request.form.get("role", "readonly")
    if not label:
        flash("A label is required so you can tell tokens apart later.", "danger")
        return redirect(url_for("api_tokens_page"))
    if any(t["label"] == label for t in list_api_tokens()):
        flash(f"A token labeled '{label}' already exists.", "danger")
        return redirect(url_for("api_tokens_page"))
    raw_token = create_api_token(label, role)
    session["new_api_token"] = raw_token
    log_activity("api_token_created", f"label={label} role={role}")
    flash("Token created -- copy it now, it won't be shown again.", "success")
    return redirect(url_for("api_tokens_page"))


@app.route("/api-tokens/<label>/revoke", methods=["POST"])
@require_admin
def revoke_api_token_route(label):
    revoke_api_token(label)
    log_activity("api_token_revoked", f"label={label}")
    flash(f"Token '{label}' revoked.", "success")
    return redirect(url_for("api_tokens_page"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    config = load_config()
    settings = config.get("settings", [])
    updates_data = load_updates_raw()

    records = []
    for i, entry in enumerate(settings):
        records.append({
            "index": i,
            "entry": entry,
            "summary": summarize(entry),
            "status": find_status_for_domain(updates_data, entry.get("domain", "")),
        })

    edit_index = request.args.get("edit", type=int)
    edit_entry, is_editing = {}, False
    if edit_index is not None and 0 <= edit_index < len(settings):
        edit_entry, is_editing = settings[edit_index], True
    edit_provider = edit_entry.get("provider", "")

    providers_count = len({r["entry"].get("provider") for r in records if r["entry"].get("provider")})
    most_recent_dt = None
    for r in records:
        dt = r["status"].get("time_dt") if r["status"] else None
        if dt and (most_recent_dt is None or dt > most_recent_dt):
            most_recent_dt = dt
    updated_summary = _relative_time(most_recent_dt) if most_recent_dt else "Never"

    active_tab = request.args.get("tab", "records")
    if active_tab not in ("records", "add", "advanced", "backups", "activity"):
        active_tab = "records"
    if is_editing:
        active_tab = "add"  # editing always wins, regardless of ?tab=

    return render_template(
        "index.html",
        config=config,
        records=records,
        provider_schemas=PROVIDER_SCHEMAS,
        testable_providers=list(PROVIDER_TEST_FUNCS.keys()),
        edit_index=edit_index if is_editing else "",
        edit_entry=edit_entry,
        edit_provider=edit_provider,
        is_editing=is_editing,
        active_tab=active_tab,
        is_supported_provider=(edit_provider in PROVIDER_SCHEMAS) if is_editing else True,
        backups=list_backups(),
        updates_raw=json.dumps(updates_data, indent=2) if updates_data else None,
        auth_configured=auth_configured(),
        auth_is_gui=(session.get("auth_via") == "gui"),
        current_role=current_role(),
        current_username=session.get("username"),
        is_admin=(current_role() == "admin" or not auth_configured()),
        activity=load_activity(),
        api_tokens=list_api_tokens(),
        providers_count=providers_count,
        updated_summary=updated_summary,
    )


@app.route("/records/save", methods=["POST"])
@require_write
def save_record():
    index_raw = request.form.get("index", "").strip()
    index = int(index_raw) if index_raw.isdigit() else None
    provider = request.form.get("provider", "").strip()
    domain = request.form.get("domain", "").strip()

    if not provider or not domain:
        flash("Provider and domain are required.", "danger")
        return redirect(url_for("index"))
    if provider not in PROVIDER_SCHEMAS:
        flash(f"Unknown provider '{provider}'.", "danger")
        return redirect(url_for("index"))

    config = load_config()
    settings = config.get("settings", [])
    editing_existing = index is not None and 0 <= index < len(settings)
    existing = settings[index] if editing_existing else {}

    try:
        entry = build_entry(provider, domain, request.form, existing)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("index"))

    if editing_existing:
        settings[index] = entry
        log_activity("record_updated", f"domain={domain} provider={provider}")
        flash(f"Record for {domain} updated. Restart ddns-updater for changes to take effect.", "success")
    else:
        settings.append(entry)
        log_activity("record_added", f"domain={domain} provider={provider}")
        flash(f"Record for {domain} added. Restart ddns-updater for changes to take effect.", "success")

    config["settings"] = settings
    save_config(config)
    return redirect(url_for("index"))


@app.route("/records/test", methods=["POST"])
def test_record():
    provider = request.form.get("provider", "").strip()
    domain = request.form.get("domain", "").strip()
    index_raw = request.form.get("index", "").strip()
    index = int(index_raw) if index_raw.isdigit() else None

    if provider not in PROVIDER_TEST_FUNCS:
        return {"ok": False, "message": "Live testing isn't available for this provider."}, 400

    config = load_config()
    settings = config.get("settings", [])
    existing = settings[index] if index is not None and 0 <= index < len(settings) else {}

    fields = _extract_test_fields(provider, request.form, existing)
    ok, message = PROVIDER_TEST_FUNCS[provider](fields, domain)
    return {"ok": ok, "message": message}


@app.route("/records/<int:index>/delete", methods=["POST"])
@require_write
def delete_record(index):
    config = load_config()
    settings = config.get("settings", [])
    if 0 <= index < len(settings):
        removed = settings.pop(index)
        config["settings"] = settings
        save_config(config)
        log_activity("record_deleted", f"domain={removed.get('domain', 'unknown')} provider={removed.get('provider', 'unknown')}")
        flash(f"Record for {removed.get('domain', 'unknown')} deleted. Restart ddns-updater for changes to take effect.", "success")
    else:
        flash("Record not found (it may have already been deleted).", "danger")
    return redirect(url_for("index"))


@app.route("/update", methods=["POST"])
@require_write
def update():
    raw = request.form.get("config", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        flash(f"Not saved: invalid JSON ({e.msg} at line {e.lineno}, col {e.colno})", "danger")
        return redirect(url_for("index"))
    if not isinstance(parsed, dict):
        flash("Not saved: config must be a JSON object.", "danger")
        return redirect(url_for("index"))
    save_config(parsed)
    log_activity("advanced_json_saved", f"{len(parsed.get('settings', []))} record(s)")
    flash("Configuration saved. Restart ddns-updater for changes to take effect.", "success")
    return redirect(url_for("index"))


@app.route("/backups/<filename>/restore", methods=["POST"])
@require_write
def restore_backup(filename):
    if not _BACKUP_NAME_RE.match(filename):
        flash("Invalid backup filename.", "danger")
        return redirect(url_for("index"))
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        flash("Backup not found.", "danger")
        return redirect(url_for("index"))
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        flash(f"Could not read backup: {e}", "danger")
        return redirect(url_for("index"))
    save_config(data)  # backs up the pre-restore state first, same as any save
    log_activity("backup_restored", filename)
    flash(f"Restored configuration from backup {filename}. Restart ddns-updater for changes to take effect.", "success")
    return redirect(url_for("index"))


@app.route("/backups/<filename>/download")
def download_backup(filename):
    if not _BACKUP_NAME_RE.match(filename):
        abort(400)
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        abort(404)
    with open(path, "r") as f:
        contents = f.read()
    return Response(
        contents,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
