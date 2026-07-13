"""
Test suite for ddns-editor. Run with: pytest tests/ -v

These tests exercise the app entirely through Flask's test client (no real
network calls -- provider "test connection" calls are mocked). They're the
same checks that were run manually during development, consolidated here so
CI can catch a regression before it ever reaches Docker Hub.
"""
import importlib
import json
import os
import re
import sys
import time
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.delenv("EDITOR_PASSWORD", raising=False)
    monkeypatch.delenv("EDITOR_USERNAME", raising=False)

    import app as appmod
    importlib.reload(appmod)

    appmod.CONFIG_PATH = str(tmp_path / "config.json")
    appmod.UPDATES_PATH = str(tmp_path / "updates.json")
    appmod.BACKUP_DIR = str(tmp_path / "backups")
    appmod.AUTH_PATH = str(tmp_path / "auth.json")
    appmod.ACTIVITY_PATH = str(tmp_path / "activity.log")
    appmod.API_TOKENS_PATH = str(tmp_path / "api_tokens.json")
    appmod.app.config["TESTING"] = True
    return appmod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def get_csrf(html):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert m, "CSRF token not found on page"
    return m.group(1).decode()


def add_record(client, provider, domain, index="", **fields):
    r = client.get("/")
    token = get_csrf(r.data)
    data = {"csrf_token": token, "index": index, "provider": provider, "domain": domain}
    for k, v in fields.items():
        data[f"{provider}__{k}"] = v
    return client.post("/records/save", data=data, follow_redirects=True)


# ---------------------------------------------------------------------------
# Record CRUD
# ---------------------------------------------------------------------------

def test_add_and_list_record(client):
    r = add_record(client, "duckdns", "a.example.com", token="tok1")
    assert b"a.example.com" in r.data


def test_add_cloudflare_and_edit_preserves_secret_when_blank(app_module, client):
    add_record(client, "cloudflare", "cf.example.com", zone_identifier="zone1", token="secrettoken", ttl="300", proxied="on")
    r = client.get("/")
    token = get_csrf(r.data)
    r = client.post("/records/save", data={
        "csrf_token": token, "index": "0", "provider": "cloudflare", "domain": "cf.example.com",
        "cloudflare__zone_identifier": "zone1", "cloudflare__token": "", "cloudflare__ttl": "600", "cloudflare__proxied": "",
    }, follow_redirects=True)
    cfg = app_module.load_config()
    assert cfg["settings"][0]["token"] == "secrettoken"
    assert cfg["settings"][0]["ttl"] == 600
    assert cfg["settings"][0]["proxied"] is False


def test_required_field_validation(client):
    r = add_record(client, "cloudflare", "bad.example.com", zone_identifier="", token="x")
    assert b"Zone ID is required" in r.data


def test_delete_record(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    r = client.get("/")
    token = get_csrf(r.data)
    r = client.post("/records/0/delete", data={"csrf_token": token}, follow_redirects=True)
    assert b"No DNS records configured yet" in r.data
    assert app_module.load_config()["settings"] == []


def test_field_prefixing_prevents_cross_provider_bleed(app_module, client):
    add_record(client, "noip", "n.example.com", username="bob", password="pw1")
    add_record(client, "cloudflare", "c.example.com", zone_identifier="z1", token="cftok", ttl="300", proxied="on")
    cfg = app_module.load_config()
    assert cfg["settings"][0] == {"provider": "noip", "domain": "n.example.com", "username": "bob",
                                   "password": "pw1", "ip_version": "ipv4 or ipv6", "ipv6_suffix": ""}
    assert cfg["settings"][1]["token"] == "cftok"


def test_unschema_provider_shows_json_fallback(app_module, client):
    cfg = {"settings": [{"provider": "route53", "domain": "r53.example.com", "access_key_id": "x"}]}
    app_module.save_config(cfg)
    r = client.get("/")
    assert b"r53.example.com" in r.data
    assert b"Edit in JSON" in r.data


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def test_backup_created_on_second_save_not_first(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    assert app_module.list_backups() == []
    add_record(client, "duckdns", "b.example.com", token="tok2")
    assert len(app_module.list_backups()) == 1


def test_backup_rotation_caps_at_max(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_BACKUPS", 3)
    for i in range(6):
        add_record(client, "duckdns", f"d{i}.example.com", token="t")
        time.sleep(1.1)  # distinct backup filenames need distinct seconds
    assert len(app_module.list_backups()) == 3


def test_restore_backup(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    add_record(client, "duckdns", "b.example.com", token="tok2")
    backups = app_module.list_backups()
    target = backups[0]["filename"]
    r = client.get("/")
    token = get_csrf(r.data)
    r = client.post(f"/backups/{target}/restore", data={"csrf_token": token}, follow_redirects=True)
    assert b"Restored configuration from backup" in r.data


def test_restore_rejects_invalid_filename(client):
    r = client.get("/")
    token = get_csrf(r.data)
    r = client.post("/backups/not-a-real-backup.json/restore", data={"csrf_token": token}, follow_redirects=True)
    assert b"Invalid backup filename" in r.data


def test_download_backup(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    add_record(client, "duckdns", "b.example.com", token="tok2")
    filename = app_module.list_backups()[0]["filename"]
    r = client.get(f"/backups/{filename}/download")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_post_without_csrf_token_rejected(client):
    r = client.post("/records/save", data={"index": "", "provider": "duckdns", "domain": "x.com", "duckdns__token": "t"})
    assert r.status_code == 400


def test_post_with_wrong_csrf_token_rejected(client):
    client.get("/")  # establish a session
    r = client.post("/records/save", data={
        "csrf_token": "definitely-wrong", "index": "", "provider": "duckdns", "domain": "x.com", "duckdns__token": "t"
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Update status (best-effort parsing of updates.json)
# ---------------------------------------------------------------------------

def test_status_parsing_finds_matching_domain(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    with open(app_module.UPDATES_PATH, "w") as f:
        json.dump({"records": [{"domain": "a.example.com", "ip": "203.0.113.5", "time": "2026-07-13T10:15:00Z"}]}, f)
    r = client.get("/")
    assert b"203.0.113.5" in r.data


def test_status_missing_shows_graceful_fallback(client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    r = client.get("/")
    assert b"No update history yet" in r.data


# ---------------------------------------------------------------------------
# Live connection testing (mocked -- no real network calls)
# ---------------------------------------------------------------------------

def test_connection_success_mocked(client):
    def fake_urlopen(req, timeout=8):
        m = MagicMock()
        m.read.return_value = json.dumps({"success": True, "result": {"name": "example.com"}}).encode()
        m.__enter__ = lambda s: m
        m.__exit__ = lambda s, *a: None
        return m

    r = client.get("/")
    token = get_csrf(r.data)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        r = client.post("/records/test", data={
            "csrf_token": token, "provider": "cloudflare", "domain": "a.example.com",
            "cloudflare__zone_identifier": "z1", "cloudflare__token": "t1",
        })
    data = r.get_json()
    assert data["ok"] is True


def test_connection_failure_mocked(client):
    def fake_fail(req, timeout=8):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    r = client.get("/")
    token = get_csrf(r.data)
    with patch("urllib.request.urlopen", side_effect=fake_fail):
        r = client.post("/records/test", data={
            "csrf_token": token, "provider": "digitalocean", "domain": "a.example.com", "digitalocean__token": "bad",
        })
    data = r.get_json()
    assert data["ok"] is False


def test_untestable_provider_returns_clean_error(client):
    r = client.get("/")
    token = get_csrf(r.data)
    r = client.post("/records/test", data={
        "csrf_token": token, "provider": "noip", "domain": "a.example.com", "noip__username": "u", "noip__password": "p",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth: GUI setup + change password
# ---------------------------------------------------------------------------

def test_setup_creates_gui_password_and_logs_in(app_module, client):
    r = client.get("/setup")
    token = get_csrf(r.data)
    r = client.post("/setup", data={
        "csrf_token": token, "username": "marwa", "password": "goodpassword1", "confirm": "goodpassword1"
    }, follow_redirects=True)
    assert b"DDNS Configuration Editor" in r.data
    auth = app_module.load_auth()
    user = auth["users"][0]
    assert user["username"] == "marwa"
    assert user["role"] == "admin"
    assert app_module.check_password_hash(user["password_hash"], "goodpassword1")


def test_setup_rejects_short_password(client):
    r = client.get("/setup")
    token = get_csrf(r.data)
    r = client.post("/setup", data={"csrf_token": token, "username": "u", "password": "short", "confirm": "short"}, follow_redirects=True)
    assert b"at least 8 characters" in r.data


def test_unauthenticated_redirect_after_setup(app_module, client):
    r = client.get("/setup")
    token = get_csrf(r.data)
    client.post("/setup", data={"csrf_token": token, "username": "marwa", "password": "goodpassword1", "confirm": "goodpassword1"})

    client2 = app_module.app.test_client()
    r = client2.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_change_password_flow(app_module, client):
    r = client.get("/setup")
    token = get_csrf(r.data)
    client.post("/setup", data={"csrf_token": token, "username": "marwa", "password": "goodpassword1", "confirm": "goodpassword1"})

    r = client.get("/")
    token = get_csrf(r.data)
    r = client.post("/account/change-password", data={
        "csrf_token": token, "current_password": "wrong", "new_password": "newpassword2", "confirm_password": "newpassword2"
    }, follow_redirects=True)
    assert b"Current password is incorrect" in r.data

    r = client.post("/account/change-password", data={
        "csrf_token": token, "current_password": "goodpassword1", "new_password": "newpassword2", "confirm_password": "newpassword2"
    }, follow_redirects=True)
    assert b"Password changed successfully" in r.data


def test_env_password_works_as_recovery_alongside_gui_password(app_module, client, monkeypatch):
    r = client.get("/setup")
    token = get_csrf(r.data)
    client.post("/setup", data={"csrf_token": token, "username": "marwa", "password": "guipassword1", "confirm": "guipassword1"})

    monkeypatch.setattr(app_module, "EDITOR_USERNAME", "admin")
    monkeypatch.setattr(app_module, "EDITOR_PASSWORD", "envpassword1")

    client2 = app_module.app.test_client()
    r = client2.get("/login")
    token2 = get_csrf(r.data)
    r = client2.post("/login", data={"csrf_token": token2, "username": "admin", "password": "envpassword1"}, follow_redirects=True)
    assert b"DDNS Configuration Editor" in r.data
    assert b"Change Password" not in r.data  # hidden while env mode active


# ---------------------------------------------------------------------------
# Login lockout
# ---------------------------------------------------------------------------

def test_login_lockout_after_threshold(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "EDITOR_USERNAME", "admin")
    monkeypatch.setattr(app_module, "EDITOR_PASSWORD", "realpassword")
    monkeypatch.setattr(app_module, "LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(app_module, "LOCKOUT_WINDOW_SECONDS", 300)
    app_module._failed_attempts.clear()

    r = client.get("/login")
    token = get_csrf(r.data)
    for _ in range(3):
        r = client.post("/login", data={"csrf_token": token, "username": "admin", "password": "wrong"}, follow_redirects=True)
    assert b"Invalid username or password" in r.data

    r = client.post("/login", data={"csrf_token": token, "username": "admin", "password": "realpassword"}, follow_redirects=True)
    assert b"Too many failed attempts" in r.data


# ---------------------------------------------------------------------------
# Session cookie security flags
# ---------------------------------------------------------------------------

def test_session_cookie_has_secure_flags(client):
    r = client.get("/")
    set_cookie = r.headers.getlist("Set-Cookie")
    assert any("HttpOnly" in h for h in set_cookie)
    assert any("SameSite=Lax" in h for h in set_cookie)


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

def test_activity_log_records_events(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    r = client.get("/")
    token = get_csrf(r.data)
    client.post("/records/0/delete", data={"csrf_token": token}, follow_redirects=True)
    activity = app_module.load_activity()
    actions = [e["action"] for e in activity]
    assert "record_added" in actions
    assert "record_deleted" in actions


# ---------------------------------------------------------------------------
# Health check (must stay open even with auth configured)
# ---------------------------------------------------------------------------

def test_healthz_open_without_auth(client):
    r = client.get("/healthz")
    assert r.get_json() == {"status": "ok"}


def test_healthz_open_with_auth_configured(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "EDITOR_USERNAME", "admin")
    monkeypatch.setattr(app_module, "EDITOR_PASSWORD", "secret123")
    r = client.get("/healthz")
    assert r.get_json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Multi-user auth: old-format migration
# ---------------------------------------------------------------------------

def test_old_single_user_auth_format_migrates_transparently(app_module, client):
    old_format = {"username": "marwa", "password_hash": app_module.generate_password_hash("oldpassword1")}
    with open(app_module.AUTH_PATH, "w") as f:
        json.dump(old_format, f)

    r = client.get("/login")
    token = get_csrf(r.data)
    r = client.post("/login", data={"csrf_token": token, "username": "marwa", "password": "oldpassword1"}, follow_redirects=True)
    assert b"DDNS Configuration Editor" in r.data

    migrated = app_module.load_auth()
    assert migrated["users"][0]["username"] == "marwa"
    assert migrated["users"][0]["role"] == "admin"


# ---------------------------------------------------------------------------
# Multi-user + roles
# ---------------------------------------------------------------------------

def setup_admin(client, username="admin", password="adminpass1"):
    r = client.get("/setup")
    token = get_csrf(r.data)
    client.post("/setup", data={"csrf_token": token, "username": username, "password": password, "confirm": password})


def test_admin_can_add_readonly_user(app_module, client):
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    r = client.post("/users/add", data={"csrf_token": token, "username": "viewer", "password": "viewerpass1", "role": "readonly"}, follow_redirects=True)
    assert b"viewer" in r.data
    auth = app_module.load_auth()
    assert any(u["username"] == "viewer" and u["role"] == "readonly" for u in auth["users"])


def test_readonly_user_cannot_add_record(app_module, client):
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    client.post("/users/add", data={"csrf_token": token, "username": "viewer", "password": "viewerpass1", "role": "readonly"})
    client.post("/logout", data={"csrf_token": token})

    client2 = app_module.app.test_client()
    r = client2.get("/login")
    token2 = get_csrf(r.data)
    client2.post("/login", data={"csrf_token": token2, "username": "viewer", "password": "viewerpass1"})

    r = client2.get("/")
    token3 = get_csrf(r.data)
    r = client2.post("/records/save", data={
        "csrf_token": token3, "index": "", "provider": "duckdns", "domain": "x.example.com", "duckdns__token": "t"
    })
    assert r.status_code == 403


def test_readonly_user_can_still_view_records(app_module, client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    client.post("/users/add", data={"csrf_token": token, "username": "viewer", "password": "viewerpass1", "role": "readonly"})
    client.post("/logout", data={"csrf_token": token})

    client2 = app_module.app.test_client()
    r = client2.get("/login")
    token2 = get_csrf(r.data)
    client2.post("/login", data={"csrf_token": token2, "username": "viewer", "password": "viewerpass1"})
    r = client2.get("/")
    assert b"a.example.com" in r.data


def test_cannot_delete_last_remaining_user(app_module, client):
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    r = client.post("/users/admin/delete", data={"csrf_token": token}, follow_redirects=True)
    assert b"only remaining user" in r.data


def test_cannot_demote_only_admin(app_module, client):
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    r = client.post("/users/admin/role", data={"csrf_token": token, "role": "readonly"}, follow_redirects=True)
    assert b"only remaining admin" in r.data


def test_readonly_user_cannot_access_users_page(app_module, client):
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    client.post("/users/add", data={"csrf_token": token, "username": "viewer", "password": "viewerpass1", "role": "readonly"})
    client.post("/logout", data={"csrf_token": token})

    client2 = app_module.app.test_client()
    r = client2.get("/login")
    token2 = get_csrf(r.data)
    client2.post("/login", data={"csrf_token": token2, "username": "viewer", "password": "viewerpass1"})
    r = client2.get("/users")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# TOTP 2FA
# ---------------------------------------------------------------------------

def test_2fa_enroll_and_login_flow(app_module, client):
    setup_admin(client)
    r = client.get("/account/2fa/setup")
    secret = re.search(rb'value="([A-Z0-9]+)" readonly onclick', r.data).group(1).decode()
    token = get_csrf(r.data)
    code = app_module._totp_code(secret, __import__("time").time())
    r = client.post("/account/2fa/setup", data={"csrf_token": token, "code": code}, follow_redirects=True)
    assert b"Two-factor authentication enabled" in r.data

    client.post("/logout", data={"csrf_token": token})

    client2 = app_module.app.test_client()
    r = client2.get("/login")
    token2 = get_csrf(r.data)
    r = client2.post("/login", data={"csrf_token": token2, "username": "admin", "password": "adminpass1"}, follow_redirects=True)
    assert b"Enter your verification code" in r.data

    token3 = get_csrf(r.data)
    code2 = app_module._totp_code(secret, __import__("time").time())
    r = client2.post("/login/2fa", data={"csrf_token": token3, "code": code2}, follow_redirects=True)
    assert b"DDNS Configuration Editor" in r.data


def test_2fa_setup_rejects_wrong_confirmation_code(app_module, client):
    setup_admin(client)
    r = client.get("/account/2fa/setup")
    token = get_csrf(r.data)
    # An arbitrary 6-digit code essentially never matches the real TOTP
    # value for a freshly generated secret at this instant.
    r = client.post("/account/2fa/setup", data={"csrf_token": token, "code": "123456"})
    assert b"didn" in r.data  # "didn't match" (apostrophe may be HTML-escaped)
    auth = app_module.load_auth()
    assert auth["users"][0]["totp_enabled"] is False


def test_totp_verify_accepts_current_code_rejects_wrong(app_module):
    secret = app_module.generate_totp_secret()
    import time as _time
    good = app_module._totp_code(secret, _time.time())
    assert app_module.verify_totp(secret, good) is True
    assert app_module.verify_totp(secret, "000000") is False or good == "000000"


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------

def test_api_token_create_and_use(app_module, client):
    setup_admin(client)
    r = client.get("/api-tokens")
    token = get_csrf(r.data)
    r = client.post("/api-tokens/create", data={"csrf_token": token, "label": "test-script", "role": "readonly"}, follow_redirects=True)
    m = re.search(rb'value="(ddns_[^"]+)" readonly', r.data)
    assert m, "new token not shown on page"
    raw_token = m.group(1).decode()

    api_client = app_module.app.test_client()
    r = api_client.get("/api/v1/status", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.get_json()["status"] == "ok"


def test_api_readonly_token_cannot_add_record(app_module, client):
    setup_admin(client)
    r = client.get("/api-tokens")
    token = get_csrf(r.data)
    r = client.post("/api-tokens/create", data={"csrf_token": token, "label": "ro-token", "role": "readonly"}, follow_redirects=True)
    raw_token = re.search(rb'value="(ddns_[^"]+)" readonly', r.data).group(1).decode()

    api_client = app_module.app.test_client()
    r = api_client.post("/api/v1/records", headers={"Authorization": f"Bearer {raw_token}"},
                         json={"provider": "duckdns", "domain": "x.example.com", "token": "t"})
    assert r.status_code == 403


def test_api_admin_token_can_add_and_delete_record(app_module, client):
    setup_admin(client)
    r = client.get("/api-tokens")
    token = get_csrf(r.data)
    r = client.post("/api-tokens/create", data={"csrf_token": token, "label": "admin-token", "role": "admin"}, follow_redirects=True)
    raw_token = re.search(rb'value="(ddns_[^"]+)" readonly', r.data).group(1).decode()

    api_client = app_module.app.test_client()
    r = api_client.post("/api/v1/records", headers={"Authorization": f"Bearer {raw_token}"},
                         json={"provider": "duckdns", "domain": "api.example.com", "token": "apitoken"})
    assert r.status_code == 201
    cfg = app_module.load_config()
    assert cfg["settings"][0]["domain"] == "api.example.com"

    r = api_client.get("/api/v1/records", headers={"Authorization": f"Bearer {raw_token}"})
    assert len(r.get_json()["records"]) == 1

    r = api_client.delete("/api/v1/records/0", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.get_json()["status"] == "deleted"
    assert app_module.load_config()["settings"] == []


def test_api_without_token_rejected(app_module, client):
    r = client.get("/api/v1/status")
    assert r.status_code == 401


def test_api_revoked_token_stops_working(app_module, client):
    setup_admin(client)
    r = client.get("/api-tokens")
    token = get_csrf(r.data)
    r = client.post("/api-tokens/create", data={"csrf_token": token, "label": "temp", "role": "readonly"}, follow_redirects=True)
    raw_token = re.search(rb'value="(ddns_[^"]+)" readonly', r.data).group(1).decode()

    r = client.get("/api-tokens")
    token2 = get_csrf(r.data)
    client.post("/api-tokens/temp/revoke", data={"csrf_token": token2})

    api_client = app_module.app.test_client()
    r = api_client.get("/api/v1/status", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.status_code == 401


def test_api_route_exempt_from_browser_csrf_check(app_module, client):
    # /api/* routes must NOT require a csrf_token field -- they're
    # authenticated by bearer token, not session cookie.
    setup_admin(client)
    r = client.get("/api-tokens")
    token = get_csrf(r.data)
    r = client.post("/api-tokens/create", data={"csrf_token": token, "label": "no-csrf-test", "role": "admin"}, follow_redirects=True)
    raw_token = re.search(rb'value="(ddns_[^"]+)" readonly', r.data).group(1).decode()

    api_client = app_module.app.test_client()
    r = api_client.post("/api/v1/records", headers={"Authorization": f"Bearer {raw_token}"},
                         json={"provider": "duckdns", "domain": "nocsrf.example.com", "token": "t"})
    assert r.status_code == 201  # succeeded with NO csrf_token field at all


# ---------------------------------------------------------------------------
# Webhooks (mocked -- no real network calls)
# ---------------------------------------------------------------------------

def test_webhook_fires_on_record_added(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(app_module, "WEBHOOK_FORMAT", "generic")
    calls = []

    def fake_urlopen(req, timeout=5):
        calls.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: m
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        add_record(client, "duckdns", "webhook.example.com", token="t")

    assert calls == ["https://example.com/webhook"]


def test_webhook_failure_does_not_break_the_request(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "WEBHOOK_URL", "https://example.com/webhook")

    def fake_fail(req, timeout=5):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=fake_fail):
        r = add_record(client, "duckdns", "webhook2.example.com", token="t")
    assert b"webhook2.example.com" in r.data  # request still succeeded despite webhook failure


def test_webhook_not_sent_for_non_notify_actions(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "WEBHOOK_URL", "https://example.com/webhook")
    calls = []

    def fake_urlopen(req, timeout=5):
        calls.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: m
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.get("/")  # login_failed / non-notify path: just a GET, no activity at all
    assert calls == []


# ---------------------------------------------------------------------------
# Sidebar shell / tab navigation
# ---------------------------------------------------------------------------

def test_tab_query_param_selects_active_pane(client):
    add_record(client, "duckdns", "a.example.com", token="tok1")
    r = client.get("/?tab=backups")
    assert 'id="backups" role="tabpanel"'.encode() in r.data
    # crude check the backups pane carries the active classes -- look for
    # the pane opening tag with "show active" somewhere near "backups"
    assert b'show active" id="backups"' in r.data


def test_editing_forces_add_tab_regardless_of_tab_param(client):
    add_record(client, "cloudflare", "cf.example.com", zone_identifier="z1", token="t1", ttl="300", proxied="on")
    r = client.get("/?edit=0&tab=backups")
    assert b'show active" id="add"' in r.data


def test_all_main_pages_render_with_sidebar_shell(app_module, client):
    setup_admin(client)
    for path in ["/", "/?tab=add", "/?tab=advanced", "/?tab=backups", "/?tab=activity", "/users", "/api-tokens"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert b'class="app-sidebar"' in r.data, path


def test_standalone_auth_pages_have_no_sidebar(app_module, client):
    setup_admin(client)
    r = client.get("/account/2fa/setup")
    assert b'class="app-sidebar"' not in r.data


def test_readonly_user_does_not_see_admin_sidebar_links(app_module, client):
    setup_admin(client)
    r = client.get("/users")
    token = get_csrf(r.data)
    client.post("/users/add", data={"csrf_token": token, "username": "viewer", "password": "viewerpass1", "role": "readonly"})
    client.post("/logout", data={"csrf_token": token})

    client2 = app_module.app.test_client()
    r = client2.get("/login")
    token2 = get_csrf(r.data)
    client2.post("/login", data={"csrf_token": token2, "username": "viewer", "password": "viewerpass1"})
    r = client2.get("/")
    assert b'href="/users"' not in r.data
    assert b'href="/api-tokens"' not in r.data
