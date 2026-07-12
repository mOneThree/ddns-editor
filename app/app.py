import json
import os

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
# Required for flash messages. Falls back to a random value each restart if
# the operator hasn't set one -- fine for a homelab tool with no sessions
# that need to persist across restarts.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

CONFIG_PATH = "/updater/data/config.json"

# Providers with a friendly form. Anything else falls back to the raw JSON
# editor -- ddns-updater supports ~50 providers total (see
# https://github.com/qdm12/ddns-updater), and hand-wizarding every one of
# their distinct config shapes isn't worth the fragile surface area. These
# two cover the large majority of home use.
SUPPORTED_PROVIDERS = ["cloudflare", "duckdns"]


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


def summarize(entry):
    """One-line human summary of a record for the list view. Never
    includes the token."""
    provider = entry.get("provider", "unknown")
    if provider == "cloudflare":
        zone = entry.get("zone_identifier", "")
        zone_short = (zone[:10] + "…") if len(zone) > 10 else zone
        ttl = entry.get("ttl", 300)
        proxied = "Proxied" if entry.get("proxied") else "DNS only"
        return f"Zone {zone_short} • TTL {ttl}s • {proxied}"
    if provider == "duckdns":
        return "Duck DNS"
    # Unrecognized provider (any of the ~50 others ddns-updater supports).
    # Show a compact key list so operators can still tell records apart.
    keys = [k for k in entry.keys() if k not in ("provider", "domain", "token")]
    return ("Custom provider (" + ", ".join(keys) + ")") if keys else "Custom provider"


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

    return render_template(
        "index.html",
        config=config,
        records=records,
        edit_index=edit_index if is_editing else "",
        edit_entry=edit_entry,
        edit_provider=edit_entry.get("provider", ""),
        is_editing=is_editing,
        is_supported_provider=edit_entry.get("provider", "") in SUPPORTED_PROVIDERS if is_editing else True,
    )


@app.route("/records/save", methods=["POST"])
def save_record():
    # Blank "index" means adding a new record; a valid integer means
    # editing an existing one in place.
    index_raw = request.form.get("index", "").strip()
    index = int(index_raw) if index_raw.isdigit() else None

    provider = request.form.get("provider", "").strip()
    domain = request.form.get("domain", "").strip()

    if not provider or not domain:
        flash("Provider and domain are required.", "danger")
        return redirect(url_for("index"))

    config = load_config()
    settings = config.get("settings", [])
    editing_existing = index is not None and 0 <= index < len(settings)

    existing = settings[index] if editing_existing else {}
    existing_token = existing.get("token", "") if existing.get("provider") == provider else ""

    entry = {"provider": provider, "domain": domain}

    if provider == "cloudflare":
        zone_identifier = request.form.get("zone_identifier", "").strip()
        token = request.form.get("token", "").strip() or existing_token
        proxied = request.form.get("proxied") == "on"
        ttl_raw = request.form.get("ttl", "300").strip() or "300"

        if not zone_identifier or not token:
            flash("Zone ID and API Token are required for Cloudflare.", "danger")
            return redirect(url_for("index"))
        try:
            ttl = int(ttl_raw)
        except ValueError:
            flash("TTL must be a whole number of seconds.", "danger")
            return redirect(url_for("index"))

        entry.update({
            "zone_identifier": zone_identifier,
            "token": token,
            "proxied": proxied,
            "ttl": ttl,
        })
    elif provider == "duckdns":
        token = request.form.get("token", "").strip() or existing_token
        if not token:
            flash("Token is required for Duck DNS.", "danger")
            return redirect(url_for("index"))
        entry["token"] = token
    else:
        flash(f"Unknown provider '{provider}'.", "danger")
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
    # Advanced / raw JSON editor.
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
