import json
import os

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
# Required for flash messages. Falls back to a random value each restart if
# the operator hasn't set one -- fine for a homelab tool with no sessions
# that need to persist across restarts.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

CONFIG_PATH = "/updater/data/config.json"

# ---------------------------------------------------------------------------
# Provider schema registry.
#
# Field specs are taken directly from ddns-updater's own docs/<provider>.md
# pages (https://github.com/qdm12/ddns-updater/tree/master/docs) -- not
# guessed -- since a wrong field name here means the editor "saves
# successfully" but ddns-updater silently fails to update the record.
#
# Adding a new provider is just adding an entry here; the form and
# validation are generated from this data, no template/route changes
# needed.
#
# Field dict keys:
#   name       form field name (also the JSON key written to config.json)
#   label      shown on the Add/Edit form
#   type       "text" | "secret" | "number" | "checkbox" | "select"
#   required   bool, defaults to False
#   default    default value (also used for select options)
#   options    list of choices, required for type "select"
#   help       optional help text under the field
#   summary    include in the Configured Records table summary (default True,
#              secret fields are never shown regardless)
#   short      short label used in the summary column (falls back to label)
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
        "label": "Cloudflare",
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
        "label": "DigitalOcean",
        "fields": [
            {"name": "token", "label": "API Token", "type": "secret", "required": True,
             "help": "Create one at cloud.digitalocean.com/settings/applications"},
            IP_VERSION_FIELD, IPV6_SUFFIX_FIELD,
        ],
    },
    "godaddy": {
        "label": "GoDaddy",
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
        "label": "Porkbun",
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


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {"settings": []}


def save_config(config):
    # Write to a temp file first and rename, so a crash mid-write can't
    # corrupt the DDNS updater's live config.
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=4)
    os.replace(tmp_path, CONFIG_PATH)


def _truncate(text, length=16):
    text = str(text)
    return text if len(text) <= length else text[: length - 1] + "…"


def summarize(entry):
    """One-line human summary of a record for the list view. Secret fields
    are never included."""
    provider = entry.get("provider", "unknown")
    schema = PROVIDER_SCHEMAS.get(provider)
    if not schema:
        # Any of the ~35 other providers ddns-updater supports that don't
        # have a schema here yet. Show a compact key list so operators can
        # still tell records apart; edit via the Advanced tab.
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
    """Build a settings entry from submitted form data using the provider's
    schema. Raises ValueError with a user-facing message if validation
    fails. Form field names are prefixed with "<provider>__" so that
    same-named fields across different providers' (hidden) form sections
    never collide on submit."""
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
        else:  # text, select
            value = raw or field.get("default", "")

        if field.get("required") and not value:
            raise ValueError(f"{field['label']} is required for {schema['label']}.")

        entry[name] = value

    return entry


@app.route("/")
def index():
    config = load_config()
    settings = config.get("settings", [])
    records = [
        {"index": i, "entry": entry, "summary": summarize(entry)}
        for i, entry in enumerate(settings)
    ]

    edit_index = request.args.get("edit", type=int)
    edit_entry = {}
    is_editing = False
    if edit_index is not None and 0 <= edit_index < len(settings):
        edit_entry = settings[edit_index]
        is_editing = True
    edit_provider = edit_entry.get("provider", "")

    return render_template(
        "index.html",
        config=config,
        records=records,
        provider_schemas=PROVIDER_SCHEMAS,
        edit_index=edit_index if is_editing else "",
        edit_entry=edit_entry,
        edit_provider=edit_provider,
        is_editing=is_editing,
        is_supported_provider=(edit_provider in PROVIDER_SCHEMAS) if is_editing else True,
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


@app.route("/records/<int:index>/delete", methods=["POST"])
def delete_record(index):
    config = load_config()
    settings = config.get("settings", [])
    if 0 <= index < len(settings):
        removed = settings.pop(index)
        config["settings"] = settings
        save_config(config)
        flash(
            f"Record for {removed.get('domain', 'unknown')} deleted. "
            "Restart ddns-updater for changes to take effect.",
            "success",
        )
    else:
        flash("Record not found (it may have already been deleted).", "danger")
    return redirect(url_for("index"))


@app.route("/update", methods=["POST"])
def update():
    # Advanced / raw JSON editor -- the fallback for any of the ~35
    # providers that don't have a schema above yet.
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


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
