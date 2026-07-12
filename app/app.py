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


def get_first_setting():
    settings = load_config().get("settings", [])
    return settings[0] if settings else {}


@app.route("/")
def index():
    config = load_config()
    first = get_first_setting()
    provider = first.get("provider", "")
    return render_template(
        "index.html",
        config=config,
        first=first,
        provider=provider,
        is_supported_provider=provider in SUPPORTED_PROVIDERS,
    )


@app.route("/update_simple", methods=["POST"])
def update_simple():
    provider = request.form.get("provider", "").strip()
    domain = request.form.get("domain", "").strip()

    if not provider or not domain:
        flash("Provider and domain are required.", "danger")
        return redirect(url_for("index"))

    entry = {"provider": provider, "domain": domain}
    existing = get_first_setting()
    existing_token = existing.get("token", "") if existing.get("provider") == provider else ""

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

    config = load_config()
    # Simple mode manages a single entry. Anyone needing multiple domains/
    # providers at once should use the Advanced (raw JSON) tab instead.
    config["settings"] = [entry]
    save_config(config)
    flash("Configuration saved. Restart ddns-updater for changes to take effect.", "success")
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
