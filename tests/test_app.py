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
    assert auth["username"] == "marwa"
    assert app_module.check_password_hash(auth["password_hash"], "goodpassword1")


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
