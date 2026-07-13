import glob
import hmac
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
# Required for flash messages and login sessions. Falls back to a random
# value each restart if the operator hasn't set one -- fine as long as you
# don't mind being logged out on restart.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

CONFIG_PATH = "/updater/data/config.json"
UPDATES_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "updates.json")
BACKUP_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "backups")
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "10"))
_BACKUP_NAME_RE = re.compile(r"^config-\d{8}T\d{6}Z\.json$")

# Optional login. If EDITOR_PASSWORD isn't set, the editor stays open
# (matches previous behavior, so existing deployments don't suddenly lock
# themselves out) but shows a warning banner nudging the operator to set one.
EDITOR_USERNAME = os.environ.get("EDITOR_USERNAME", "admin")
EDITOR_PASSWORD = os.environ.get("EDITOR_PASSWORD", "")
AUTH_ENABLED = bool(EDITOR_PASSWORD)


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


def _format_time(value):
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


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
    if not AUTH_ENABLED:
        return
    if request.endpoint in ("login", "healthz", "static"):
        return
    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid = hmac.compare_digest(username, EDITOR_USERNAME) and hmac.compare_digest(password, EDITOR_PASSWORD)
        if valid:
            session["logged_in"] = True
            return redirect(request.form.get("next") or url_for("index"))
        time.sleep(1)  # basic throttle against brute-force attempts
        error = "Invalid username or password."
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


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
        is_supported_provider=(edit_provider in PROVIDER_SCHEMAS) if is_editing else True,
        backups=list_backups(),
        updates_raw=json.dumps(updates_data, indent=2) if updates_data else None,
        auth_enabled=AUTH_ENABLED,
    )


@app.route("/records/save", methods=["POST"])
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
        flash(f"Record for {domain} updated. Restart ddns-updater for changes to take effect.", "success")
    else:
        settings.append(entry)
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
def delete_record(index):
    config = load_config()
    settings = config.get("settings", [])
    if 0 <= index < len(settings):
        removed = settings.pop(index)
        config["settings"] = settings
        save_config(config)
        flash(f"Record for {removed.get('domain', 'unknown')} deleted. Restart ddns-updater for changes to take effect.", "success")
    else:
        flash("Record not found (it may have already been deleted).", "danger")
    return redirect(url_for("index"))


@app.route("/update", methods=["POST"])
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
    flash("Configuration saved. Restart ddns-updater for changes to take effect.", "success")
    return redirect(url_for("index"))


@app.route("/backups/<filename>/restore", methods=["POST"])
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
    flash(f"Restored configuration from backup {filename}. Restart ddns-updater for changes to take effect.", "success")
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
