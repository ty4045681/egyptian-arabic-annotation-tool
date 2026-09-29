"""P1 frontend delivery tests (work packages A/B).

Covers section 9.1 `tests/test_frontend_delivery.py`:
switch default off, role auth, reason redirects, exact routing, missing
builds, manifest/chunk integrity, MIME/cache, traversal/unknown 404s, and
"API never returns HTML".
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import frontend_delivery as delivery
import server


ADMIN_KEY = "test-admin-key-0123456789abcdef-0123456789abcdef"
ADMIN_KEY_DIGEST = hashlib.sha256(ADMIN_KEY.encode()).hexdigest()


@pytest.fixture
def admin_client(client, monkeypatch):
    monkeypatch.setenv("ANNOTATION_ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_ID", "primary")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_IDLE_SECONDS", 1800)
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_ABSOLUTE_SECONDS", 28800)
    return client


@pytest.fixture
def preview_off(monkeypatch):
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", False)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")


@pytest.fixture
def fake_builds(tmp_path, monkeypatch):
    """Isolated dist/ + dist-p1/ trees; patched into delivery module."""
    standard = tmp_path / "dist"
    p1 = tmp_path / "dist-p1"
    for root in (standard, p1):
        (root / "assets").mkdir(parents=True)
        (root / ".vite").mkdir(parents=True)
        (root / "assets" / "index-abc123.js").write_text(
            "console.log('preview');", encoding="utf-8"
        )
        (root / "assets" / "style-abc123.css").write_text(
            "body{margin:0}", encoding="utf-8"
        )
        (root / "index.html").write_text(
            '<!doctype html><html><head>'
            '<meta name="csp-nonce" content="%FRONTEND_CSP_NONCE%">'
            '<link rel="stylesheet" href="/frontend/assets/style-abc123.css">'
            "</head><body><div id=\"root\"></div>"
            '<script type="module" src="/frontend/assets/index-abc123.js"></script>'
            "</body></html>",
            encoding="utf-8",
        )
        (root / ".vite" / "manifest.json").write_text(
            json.dumps(
                {
                    "index.html": {
                        "file": "assets/index-abc123.js",
                        "css": ["assets/style-abc123.css"],
                        "isEntry": True,
                    }
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(delivery, "DIST_STANDARD", standard)
    monkeypatch.setattr(delivery, "DIST_P1", p1)
    return standard, p1


@pytest.fixture
def preview_standard(admin_client, preview_off, fake_builds, monkeypatch):
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    return admin_client


def _admin_login(client):
    response = client.post("/api/admin/login", json={"key": ADMIN_KEY})
    assert response.status_code == 200, response.json
    return response


def _annotator_login(client, username="alice"):
    response = client.post("/api/login", json={"username": username})
    assert response.status_code == 200, response.json
    return response


# --- switch default ----------------------------------------------------------

def test_preview_switch_defaults_off(client):
    assert server.app.config.get("FRONTEND_PREVIEW_ENABLED") in (False, None)
    assert client.get("/admin/preview?view=corpus").status_code == 404
    assert client.get("/frontend-preview/workspace").status_code == 404
    assert client.get("/frontend-preview/components").status_code == 404


def test_preview_off_returns_404_even_with_auth(admin_client, preview_off, fake_builds):
    _admin_login(admin_client)
    assert admin_client.get("/admin/preview?view=corpus").status_code == 404
    assert admin_client.get("/frontend-preview/components").status_code == 404
    # The primary admin now uses these assets without enabling preview routes.
    assert admin_client.get("/frontend/assets/index-abc123.js").status_code == 200


def test_illegal_preview_config_fails_closed():
    with pytest.raises(RuntimeError):
        delivery.parse_preview_enabled("maybe")
    with pytest.raises(RuntimeError):
        delivery.parse_frontend_build("canary")
    with pytest.raises(RuntimeError):
        delivery.resolve_frontend_config(
            {"frontend_preview": False, "frontend_build": "standard"},
            {"ANNOTATION_FRONTEND_PREVIEW": "false", "ANNOTATION_FRONTEND_BUILD": "p1"},
        )


def test_startup_preflight_fails_when_preview_on_but_build_missing(tmp_path, monkeypatch):
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    monkeypatch.setattr(delivery, "DIST_STANDARD", empty)
    monkeypatch.setenv("ANNOTATION_FRONTEND_PREVIEW", "true")
    monkeypatch.setenv("ANNOTATION_FRONTEND_BUILD", "standard")
    old_preview = server.app.config.get("FRONTEND_PREVIEW_ENABLED")
    old_build = server.app.config.get("FRONTEND_BUILD")
    try:
        with pytest.raises(RuntimeError, match="build incomplete"):
            delivery.init_frontend_config(server.app, {})
    finally:
        server.app.config["FRONTEND_PREVIEW_ENABLED"] = old_preview
        server.app.config["FRONTEND_BUILD"] = old_build


# --- auth + reason ------------------------------------------------------------

def test_admin_preview_requires_admin_session(preview_standard):
    response = preview_standard.get("/admin/preview?view=corpus")
    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/login"


def test_admin_preview_serves_html_with_auth(preview_standard):
    _admin_login(preview_standard)
    response = preview_standard.get("/admin/preview?view=corpus")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("Content-Type", "")
    assert response.headers.get("Cache-Control") == "no-store"
    assert "nosniff" in response.headers.get("X-Content-Type-Options", "")
    assert "DENY" in response.headers.get("X-Frame-Options", "")
    assert "script-src 'self'" in response.headers.get("Content-Security-Policy", "")


def test_workspace_preview_uses_legacy_reason_redirect(client, fake_builds, monkeypatch):
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    response = client.get("/frontend-preview/workspace")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login.html")


def test_workspace_preview_serves_html_with_session(client, fake_builds, monkeypatch):
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    _annotator_login(client)
    response = client.get("/frontend-preview/workspace")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("Content-Type", "")
    assert response.headers.get("Cache-Control") == "no-store"
    assert "script-src 'self'" in response.headers.get("Content-Security-Policy", "")


def test_components_requires_p1_build_and_admin(admin_client, fake_builds, monkeypatch):
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    _admin_login(admin_client)
    assert admin_client.get("/frontend-preview/components").status_code == 404
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "p1")
    response = admin_client.get("/frontend-preview/components")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("Content-Type", "")


def test_components_requires_admin_session(client, fake_builds, monkeypatch):
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "p1")
    response = client.get("/frontend-preview/components")
    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/login"


# --- exact routing -------------------------------------------------------------

def test_admin_preview_view_routing(preview_standard):
    _admin_login(preview_standard)
    missing_view = preview_standard.get("/admin/preview")
    assert missing_view.status_code == 302
    assert missing_view.headers["Location"] == "/admin/preview?view=corpus"
    other = preview_standard.get("/admin/preview?view=review")
    assert other.status_code == 302
    assert other.headers["Location"].startswith("/admin?view=review")


def test_unknown_preview_paths_are_not_html_shell(preview_standard):
    _admin_login(preview_standard)
    response = preview_standard.get("/frontend-preview/unknown-page")
    assert response.status_code == 404
    body = response.get_data(as_text=True)
    assert "/frontend/assets/" not in body


def test_admin_entries_use_react_without_preview(admin_client, preview_off, fake_builds):
    for path in ("/admin", "/admin/", "/admin/login"):
        response = admin_client.get(path)
        assert response.status_code == 200
        assert "nonce-" in response.headers["Content-Security-Policy"]
        assert "%FRONTEND_CSP_NONCE%" not in response.text
        assert "/admin.js" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
    session_response = admin_client.get("/api/admin/session")
    assert session_response.status_code == 401
    assert "text/html" not in session_response.headers.get("Content-Type", "")


def test_admin_missing_build_is_explicit_without_legacy_fallback(client, preview_off, tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "DIST_STANDARD", tmp_path / "missing")
    response = client.get("/admin")
    assert response.status_code == 503
    assert "npm --prefix frontend run build" in response.json["error"]


# --- missing build ---------------------------------------------------------------

def test_preview_on_missing_build_fails_loudly(admin_client, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(delivery, "DIST_STANDARD", empty)
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    _admin_login(admin_client)
    response = admin_client.get("/admin/preview?view=corpus")
    assert response.status_code == 500
    assert "text/html" not in response.headers.get("Content-Type", "")
    assert response.json["error"] == "Frontend build incomplete"


def test_manifest_integrity_detects_missing_chunk(fake_builds):
    standard, _ = fake_builds
    manifest_path = standard / ".vite" / "manifest.json"
    manifest_path.write_text(
        json.dumps({"index.html": {"file": "assets/gone-zzz.js", "isEntry": True}}),
        encoding="utf-8",
    )
    problems = delivery.verify_build_complete(standard)
    assert any("gone-zzz.js" in problem for problem in problems)


# --- MIME / cache ------------------------------------------------------------------

def test_asset_mime_and_immutable_cache(preview_standard):
    _admin_login(preview_standard)
    js = preview_standard.get("/frontend/assets/index-abc123.js")
    assert js.status_code == 200
    assert "javascript" in js.headers.get("Content-Type", "")
    assert js.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
    assert js.headers.get("X-Content-Type-Options") == "nosniff"
    css = preview_standard.get("/frontend/assets/style-abc123.css")
    assert css.status_code == 200
    assert "text/css" in css.headers.get("Content-Type", "")
    assert css.headers.get("Cache-Control") == "public, max-age=31536000, immutable"


def test_asset_404_is_not_immutable_and_not_html(preview_standard):
    missing = preview_standard.get("/frontend/assets/does-not-exist.js")
    assert missing.status_code == 404
    assert missing.headers.get("Cache-Control") != "public, max-age=31536000, immutable"
    assert "text/html" not in missing.headers.get("Content-Type", "")


def test_asset_rejects_traversal_hidden_map_and_html(preview_standard):
    for bad in (
        "../index.html",
        "..%2Findex.html",
        ".hidden.js",
        "index-abc123.js.map",
        "page.html",
        "manifest.json",
    ):
        response = preview_standard.get(f"/frontend/assets/{bad}")
        assert response.status_code == 404, bad
        assert "text/html" not in response.headers.get("Content-Type", ""), bad


def test_unknown_api_never_returns_html(preview_standard):
    response = preview_standard.get("/api/does-not-exist-xyz")
    assert response.status_code == 404
    content_type = response.headers.get("Content-Type", "")
    body = response.get_data(as_text=True)
    assert "text/html" not in content_type or "/frontend/assets/" not in body
    assert '<div id="root">' not in body


def test_preview_html_carries_unified_security_headers(preview_standard):
    _admin_login(preview_standard)
    for path in ("/admin/preview?view=corpus", "/frontend-preview/components"):
        # components needs p1; switch for that iteration only
        if path.endswith("/components"):
            preview_standard.application.config["FRONTEND_BUILD"] = "p1"
        response = preview_standard.get(path)
        assert response.status_code == 200, path
        headers = response.headers
        assert headers.get("Cache-Control") == "no-store"
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "no-referrer"
        assert "default-src 'self'" in headers.get("Content-Security-Policy", "")
    preview_standard.application.config["FRONTEND_BUILD"] = "standard"


def test_nonce_csp_is_not_overwritten_by_after_request():
    from flask import Flask, Response

    app = Flask(__name__)
    nonce_csp = (
        "default-src 'self'; script-src 'self' 'nonce-abc123'; "
        "style-src 'self' 'nonce-abc123'"
    )
    with app.test_request_context("/admin/preview?view=corpus"):
        response = Response("ok")
        response.headers["Content-Security-Policy"] = nonce_csp
        delivery.apply_preview_html_headers(response)
        assert response.headers["Content-Security-Policy"] == nonce_csp


# --- per-response CSP nonce ------------------------------------------------------

def _csp_nonce_from_headers(headers) -> str | None:
    csp = headers.get("Content-Security-Policy", "")
    for token in csp.replace(";", " ").split():
        if token.startswith("'nonce-") and token.endswith("'"):
            return token[len("'nonce-"):-1]
    return None


def test_preview_html_carries_fresh_nonce_matching_meta(preview_standard):
    _admin_login(preview_standard)
    first = preview_standard.get("/admin/preview?view=corpus")
    second = preview_standard.get("/admin/preview?view=corpus")
    assert first.status_code == 200 and second.status_code == 200
    nonces = []
    for response in (first, second):
        csp = response.headers.get("Content-Security-Policy", "")
        assert "style-src 'self'" in csp
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp
        # Preview entries load external chunks only; inline scripts stay banned.
        assert "'nonce-" not in csp.split("style-src")[0]
        nonce = _csp_nonce_from_headers(response.headers)
        assert nonce, csp
        body = response.get_data(as_text=True)
        assert f'<meta name="csp-nonce" content="{nonce}"' in body
        assert "%FRONTEND_CSP_NONCE%" not in body
        assert response.headers.get("Cache-Control") == "no-store"
        nonces.append(nonce)
    assert nonces[0] != nonces[1]


def test_preview_html_without_placeholder_fails_loudly(
    admin_client, tmp_path, monkeypatch
):
    broken = tmp_path / "broken"
    (broken / "assets").mkdir(parents=True)
    (broken / ".vite").mkdir(parents=True)
    (broken / "assets" / "index-abc123.js").write_text("console.log(1);", encoding="utf-8")
    (broken / "index.html").write_text(
        "<!doctype html><html><head></head><body></body></html>", encoding="utf-8"
    )
    (broken / ".vite" / "manifest.json").write_text(
        json.dumps({"index.html": {"file": "assets/index-abc123.js", "isEntry": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(delivery, "DIST_STANDARD", broken)
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    _admin_login(admin_client)
    response = admin_client.get("/admin/preview?view=corpus")
    assert response.status_code == 500
    assert "text/html" not in response.headers.get("Content-Type", "")
