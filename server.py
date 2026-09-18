#!/usr/bin/env python3
"""Egyptian Arabic Annotation Tool — Flask API v5 (PostgreSQL backend).

Sessions, task leases, versioned annotations, history and the completed
page all live in PostgreSQL (see db.py / annotation_repository.py).
Audio files remain on the local filesystem and are streamed by the WSGI
file wrapper (Range/ETag handled by Werkzeug).
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, jsonify, make_response, redirect, request, send_file,
                   session)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import annotation_repository as repo
from db import assert_schema_current, db_conn

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
INDEX_PATH = SCRIPT_DIR / "index.html"
LOGIN_PATH = SCRIPT_DIR / "login.html"
COMPLETED_PATH = SCRIPT_DIR / "completed.html"
ADMIN_PATH = SCRIPT_DIR / "admin.html"
ADMIN_CSS_PATH = SCRIPT_DIR / "admin.css"
ADMIN_JS_PATH = SCRIPT_DIR / "admin.js"

SESSION_TIMEOUT_MINUTES = 30  # idle lifetime; assignments never expire
SESSION_ABSOLUTE_TIMEOUT_HOURS = 20
SESSION_PRESENCE_HEARTBEAT_SECONDS = 30
SESSION_PRESENCE_LEASE_SECONDS = 150
SESSION_TAKEOVER_TOKEN_SECONDS = 60
SESSION_ACTIVITY_THROTTLE_SECONDS = 30
OFFLINE_DRAFT_RETENTION_DAYS = 7
SESSION_IDLE_WARNING_SECONDS = 120
TAKEOVER_SALT = "annotator-session-takeover"
TAKEOVER_PURPOSE = "annotator-session-takeover"
DEFAULT_PUBLIC_DASHBOARD_TIMEZONE = "Asia/Shanghai"

SESSION_STATUS_MESSAGES = {
    "not_authenticated": "Not authenticated",
    "logged_out": "Not authenticated",
    "session_replaced": "Session was replaced by another login.",
    "idle_timeout": "Session expired after a period of inactivity.",
    "absolute_timeout": "Session reached its absolute time limit.",
    "account_deactivated": "This account has been deactivated.",
}

MIME_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".webm": "audio/webm",
    ".opus": "audio/opus", ".wma": "audio/x-ms-wma", ".aac": "audio/aac",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("annotator")

app = Flask(__name__, static_folder=None)

_admin_login_lock = threading.Lock()
_admin_login_attempts: dict[str, list[float]] = {}
LEADERBOARD_CACHE_SECONDS = 60
_leaderboard_cache_lock = threading.Lock()
_leaderboard_cache: dict = {
    "expires_monotonic": 0.0,
    "payload": None,
}


def load_config() -> dict:
    c = {"audio_dir": "./audio", "port": 8080}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            c.update(json.load(f))
    return c


def resolve_public_dashboard_timezone(config: dict | None = None) -> str:
    """Return a validated IANA timezone name. Invalid values fail closed."""
    raw = (config or {}).get(
        "public_dashboard_timezone", DEFAULT_PUBLIC_DASHBOARD_TIMEZONE,
    )
    if raw is None or str(raw).strip() == "":
        name = DEFAULT_PUBLIC_DASHBOARD_TIMEZONE
    else:
        name = str(raw).strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Invalid public_dashboard_timezone {name!r}; "
            "use a valid IANA timezone name such as Asia/Shanghai"
        ) from exc
    return name


# ============================================================
# Auth decorator
# ============================================================
def _session_reason_redirect(code: str) -> str:
    if code in ("not_authenticated", "logged_out"):
        return "/login.html"
    return f"/login.html?reason={code}"


def _cookie_generation():
    value = session.get("generation")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def inspect_request_session() -> repo.SessionState:
    username = session.get("user", "")
    sid = session.get("sid", "")
    generation = _cookie_generation()
    state = repo.inspect_session(username, sid, generation)
    if (
        state.status == "valid"
        and state.fence is not None
        and generation is None
    ):
        session["generation"] = state.fence.generation
    return state


def current_user() -> dict | None:
    state = inspect_request_session()
    if state.status != "valid" or state.fence is None:
        return None
    fence = state.fence
    return {
        "id": fence.user_id,
        "username": fence.username,
        "session_id": fence.session_id,
        "generation": fence.generation,
        "fence": fence,
        "idle_expires_at": state.idle_expires_at,
        "absolute_expires_at": state.absolute_expires_at,
        "last_seen_at": state.last_seen_at,
    }


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        state = inspect_request_session()
        if state.status == "account_deactivated":
            return jsonify({
                "error": SESSION_STATUS_MESSAGES["account_deactivated"],
                "code": "account_deactivated",
                "redirect": _session_reason_redirect("account_deactivated"),
            }), 403
        if state.status != "valid" or state.fence is None:
            code = (
                "not_authenticated"
                if state.status in ("logged_out", "not_authenticated")
                else state.status
            )
            return jsonify({
                "error": SESSION_STATUS_MESSAGES.get(code, "Not authenticated"),
                "code": code,
                "redirect": _session_reason_redirect(code),
            }), 401
        fence = state.fence
        request.annotator = {
            "id": fence.user_id,
            "username": fence.username,
            "session_id": fence.session_id,
            "generation": fence.generation,
            "fence": fence,
            "idle_expires_at": state.idle_expires_at,
            "absolute_expires_at": state.absolute_expires_at,
            "last_seen_at": state.last_seen_at,
        }
        return f(*args, **kwargs)
    return decorated


def annotator_session_policy() -> repo.SessionPolicy:
    idle = int(app.config.get("SESSION_TTL_SECONDS", SESSION_TIMEOUT_MINUTES * 60))
    absolute = int(app.config.get(
        "SESSION_ABSOLUTE_SECONDS", SESSION_ABSOLUTE_TIMEOUT_HOURS * 3600,
    ))
    return repo.SessionPolicy(
        idle_seconds=idle,
        absolute_seconds=max(idle, absolute),
        presence_lease_seconds=int(app.config.get(
            "SESSION_PRESENCE_LEASE_SECONDS", SESSION_PRESENCE_LEASE_SECONDS,
        )),
        heartbeat_seconds=int(app.config.get(
            "SESSION_PRESENCE_HEARTBEAT_SECONDS", SESSION_PRESENCE_HEARTBEAT_SECONDS,
        )),
        takeover_token_seconds=int(app.config.get(
            "SESSION_TAKEOVER_TOKEN_SECONDS", SESSION_TAKEOVER_TOKEN_SECONDS,
        )),
        activity_throttle_seconds=int(app.config.get(
            "SESSION_ACTIVITY_THROTTLE_SECONDS", SESSION_ACTIVITY_THROTTLE_SECONDS,
        )),
        offline_draft_retention_days=int(app.config.get(
            "OFFLINE_DRAFT_RETENTION_DAYS", OFFLINE_DRAFT_RETENTION_DAYS,
        )),
        idle_warning_seconds=int(app.config.get(
            "SESSION_IDLE_WARNING_SECONDS", SESSION_IDLE_WARNING_SECONDS,
        )),
    )


def _takeover_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.secret_key, salt=TAKEOVER_SALT)


def _issue_takeover_token(conflict: repo.ActiveSessionConflict) -> str:
    return _takeover_serializer().dumps({
        "username": conflict.username,
        "observed_generation": conflict.observed_generation,
        "observed_session_fingerprint": conflict.session_fingerprint,
        "purpose": TAKEOVER_PURPOSE,
    })


def _bind_annotator_cookie(result: dict) -> None:
    session.clear()
    session["user"] = result["username"]
    session["sid"] = str(result["session_id"])
    session["generation"] = int(result["generation"])
    session.permanent = True


def _public_session_payload(result: dict) -> dict:
    return {
        "mode": result["mode"],
        "idle_expires_at": repo._iso(result.get("idle_expires_at")),
        "absolute_expires_at": repo._iso(result.get("absolute_expires_at")),
    }


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _admin_cookie_name() -> str:
    return str(app.config.get(
        "ADMIN_SESSION_COOKIE_NAME",
        app.config.get("ADMIN_COOKIE_NAME", "admin_session"),
    ))


def _admin_csrf_cookie_name() -> str:
    return str(app.config.get("ADMIN_CSRF_COOKIE_NAME", "admin_csrf"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_ip() -> str:
    try:
        remote = str(ipaddress.ip_address(request.remote_addr or ""))
    except ValueError:
        return "unknown"
    trusted = app.config.get("ADMIN_TRUSTED_PROXIES", {"127.0.0.1", "::1"})
    if remote in trusted:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            chain = []
            for value in forwarded.split(","):
                try:
                    chain.append(str(ipaddress.ip_address(value.strip())))
                except ValueError:
                    continue
            chain.append(remote)
            # Walk from the application back toward the client, stripping
            # only explicitly trusted hops. The first untrusted address is
            # the rate-limit identity.
            for candidate in reversed(chain):
                if candidate not in trusted:
                    return candidate
    return remote


def _request_ip_hash() -> str:
    material = f"{_request_ip()}\0{app.secret_key}"
    return _digest(material)


def _admin_key_matches(candidate: str) -> bool:
    expected = str(app.config.get("ADMIN_KEY_SHA256", "")).strip().lower()
    if len(expected) != 64 or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    return secrets.compare_digest(_digest(candidate), expected)


def _admin_login_rate_limited(ip: str, *, record_failure: bool = False) -> bool:
    """Small in-process backstop; production also rate-limits at the proxy."""
    now = time.monotonic()
    window = int(app.config.get("ADMIN_LOGIN_WINDOW_SECONDS", 300))
    maximum = int(app.config.get("ADMIN_LOGIN_MAX_FAILURES", 5))
    with _admin_login_lock:
        if record_failure and ip not in _admin_login_attempts \
                and len(_admin_login_attempts) >= 4096:
            expired = [
                address for address, stamps in _admin_login_attempts.items()
                if not stamps or now - stamps[-1] >= window
            ]
            for address in expired:
                _admin_login_attempts.pop(address, None)
            if len(_admin_login_attempts) >= 4096:
                oldest = min(
                    _admin_login_attempts,
                    key=lambda address: _admin_login_attempts[address][-1],
                )
                _admin_login_attempts.pop(oldest, None)
        recent = [stamp for stamp in _admin_login_attempts.get(ip, [])
                  if now - stamp < window]
        if record_failure:
            recent.append(now)
        if recent:
            _admin_login_attempts[ip] = recent
        else:
            _admin_login_attempts.pop(ip, None)
        return len(recent) >= maximum


def _clear_admin_login_failures(ip: str) -> None:
    with _admin_login_lock:
        _admin_login_attempts.pop(ip, None)


def _record_admin_step_up_failure(*, purpose: str,
                                  rate_limited: bool) -> None:
    """Record credential-confirmation failures without persisting the key."""
    try:
        repo.record_admin_auth_event(
            success=False,
            key_id=str(app.config["ADMIN_KEY_ID"]),
            ip_hash=_request_ip_hash(),
            user_agent=request.headers.get("User-Agent", "")[:500],
            details={
                "purpose": purpose,
                "rate_limited": rate_limited,
                "admin_session_id": str(request.admin["id"]),
            },
        )
    except Exception:  # noqa: BLE001 - do not mask the authorization result
        logger.warning("Could not persist failed admin step-up audit")


def _require_admin_key_confirmation(data: dict, *, purpose: str) -> None:
    candidate = data.get("admin_key", data.get("admin_key_confirmation"))
    if not isinstance(candidate, str) or not candidate.strip():
        raise repo.ValidationError("admin_key_confirmation is required")
    candidate = candidate.strip()
    ip = _request_ip()
    if _admin_login_rate_limited(ip):
        _record_admin_step_up_failure(purpose=purpose, rate_limited=True)
        raise repo.RateLimitError("Too many credential attempts. Try again later.")
    if len(candidate) > 512 or not _admin_key_matches(candidate):
        limited = _admin_login_rate_limited(ip, record_failure=True)
        _record_admin_step_up_failure(
            purpose=purpose,
            rate_limited=limited,
        )
        if limited:
            raise repo.RateLimitError(
                "Too many credential attempts. Try again later."
            )
        raise repo.ForbiddenError("Admin credential confirmation failed")
    _clear_admin_login_failures(ip)


def current_admin(*, touch: bool = True) -> dict | None:
    token = request.cookies.get(_admin_cookie_name(), "")
    if not token:
        return None
    return repo.validate_admin_session(
        token_digest=_digest(token),
        idle_seconds=int(app.config.get(
            "ADMIN_SESSION_IDLE_SECONDS",
            app.config["ADMIN_IDLE_TTL_SECONDS"],
        )),
        touch=touch,
    )


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        admin = current_admin()
        if not admin:
            return jsonify({"error": "Admin authentication required",
                            "redirect": "/admin"}), 401
        request.admin = admin
        return f(*args, **kwargs)
    return decorated


def admin_write_required(f):
    @wraps(f)
    @admin_required
    def decorated(*args, **kwargs):
        csrf = request.headers.get("X-CSRF-Token", "")
        csrf_cookie = request.cookies.get(_admin_csrf_cookie_name(), "")
        expected = str(request.admin.get("csrf_digest", ""))
        if not csrf or not csrf_cookie or not secrets.compare_digest(csrf, csrf_cookie) \
                or not expected or not secrets.compare_digest(_digest(csrf), expected):
            raise repo.ForbiddenError("Invalid CSRF token")
        return f(*args, **kwargs)
    return decorated


def _redact_admin_secrets(value):
    if isinstance(value, dict):
        secret_fields = {
            "key", "admin_key", "admin_key_confirmation", "csrf_token",
        }
        return {
            str(key): ("[redacted]" if str(key).lower() in secret_fields
                       else _redact_admin_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_admin_secrets(item) for item in value]
    return value


def audit_admin_write_failures(action_type: str):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except repo.RepositoryError as error:
                raw = request.get_json(silent=True)
                payload = _redact_admin_secrets(raw if isinstance(raw, dict) else {})
                try:
                    repo.record_admin_action_failure(
                        admin_session_id=str(request.admin["id"]),
                        action_type=action_type,
                        reason=(str(payload.get("reason") or "")[:4000]),
                        request_payload=payload,
                        error=f"{type(error).__name__}: {error}",
                    )
                except Exception:  # noqa: BLE001 - do not mask original error
                    logger.warning("Could not persist failed %s audit", action_type)
                raise
        return decorated
    return decorator


def admin_query_filters() -> dict:
    allowed = (
        "from", "to", "timezone", "bucket", "annotator_id", "status",
        "folder", "category", "q", "include_deactivated",
        "annotator_status", "action_type", "lifecycle", "signal",
        "source_scene", "source_confidence", "batch_code", "review_status",
        "prediction_scene", "human_scene",
    )
    return {
        key: value.strip()
        for key in allowed
        if (value := request.args.get(key)) is not None and value.strip()
    }


def admin_items(data: dict) -> list[dict]:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise repo.ValidationError("items must be a non-empty array")
    if len(items) > 100:
        raise repo.ValidationError("A batch can contain at most 100 items")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise repo.ValidationError("Each item must be an object")
        normalized.append({
            "task_id": required_string(item, "task_id"),
            "expected_version_id": required_string(item, "expected_version_id"),
        })
    return normalized


# ============================================================
# Error handling
# ============================================================
@app.errorhandler(repo.RepositoryError)
def handle_repo_error(err: repo.RepositoryError):
    body = {"error": str(err)}
    if isinstance(err, repo.RevisionConflict):
        body["current_revision"] = err.current_revision
        body["conflict"] = "revision"
    if getattr(err, "code", None):
        body["code"] = err.code
    if getattr(err, "field", None):
        body["field"] = err.field
    if isinstance(err, repo.SessionFenceError):
        body["redirect"] = _session_reason_redirect(err.code)
    if isinstance(err, repo.ForbiddenError) and err.code == "account_deactivated":
        body["redirect"] = _session_reason_redirect("account_deactivated")
    return jsonify(body), err.status


def json_object() -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise repo.ValidationError("Request body must be a JSON object")
    return value


def required_string(data: dict, field: str, *, allow_empty: bool = False) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise repo.ValidationError(f"{field} must be a string")
    value = value.strip()
    if not allow_empty and not value:
        raise repo.ValidationError(f"{field} is required")
    return value


def integer_field(data: dict, field: str, default: int | None = None) -> int:
    value = data.get(field, default)
    if isinstance(value, bool):
        raise repo.ValidationError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise repo.ValidationError(f"{field} must be an integer") from exc


def boolean_field(data: dict, field: str, default: bool | None = None) -> bool:
    value = data.get(field, default)
    if not isinstance(value, bool):
        raise repo.ValidationError(f"{field} must be a boolean")
    return value


def string_list(data: dict, field: str) -> list[str]:
    value = data.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise repo.ValidationError(f"{field} must be an array of strings")
    return value


def query_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise repo.ValidationError(f"{name} must be an integer") from exc
    return max(minimum, min(value, maximum))


# ============================================================
# Pages
# ============================================================
@app.route("/")
@app.route("/index.html")
def serve_index():
    state = inspect_request_session()
    if state.status != "valid":
        code = (
            "not_authenticated"
            if state.status in ("logged_out", "not_authenticated")
            else state.status
        )
        return redirect(_session_reason_redirect(code))
    return _no_store(send_file(str(INDEX_PATH), mimetype="text/html"))


@app.route("/login.html")
def serve_login():
    if inspect_request_session().status == "valid":
        return redirect("/")
    return _no_store(send_file(str(LOGIN_PATH), mimetype="text/html"))


@app.route("/completed.html")
def serve_completed():
    state = inspect_request_session()
    if state.status != "valid":
        code = (
            "not_authenticated"
            if state.status in ("logged_out", "not_authenticated")
            else state.status
        )
        return redirect(_session_reason_redirect(code))
    return _no_store(send_file(str(COMPLETED_PATH), mimetype="text/html"))


@app.route("/admin")
@app.route("/admin/")
@app.route("/admin/login")
def serve_admin():
    return send_file(str(ADMIN_PATH), mimetype="text/html")


@app.route("/admin.css")
def serve_admin_css():
    return send_file(str(ADMIN_CSS_PATH), mimetype="text/css")


@app.route("/admin.js")
def serve_admin_js():
    return send_file(str(ADMIN_JS_PATH), mimetype="text/javascript")


@app.after_request
def secure_admin_responses(response):
    if request.path == "/admin" or request.path.startswith("/admin/") \
            or request.path.startswith("/api/admin") \
            or request.path in ("/admin.css", "/admin.js"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'"
        )
    session_paths = {
        "/", "/index.html", "/login.html", "/completed.html",
        "/api/login", "/api/login/takeover", "/api/logout",
        "/api/current-user", "/api/heartbeat", "/api/session/heartbeat",
    }
    if request.path in session_paths or request.path.startswith("/api/login"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


# ============================================================
# Auth API
# ============================================================
@app.route("/api/login", methods=["POST"])
def api_login():
    data = json_object()
    username = required_string(data, "username")
    if len(username) > 50:
        return jsonify({"success": False,
                        "error": "Username too long (max 50 characters)"}), 400
    if not re.match(r"^[\w\s.\-]+$", username):
        return jsonify({"success": False,
                        "error": "Username contains invalid characters"}), 400

    policy = annotator_session_policy()
    cookie_user = session.get("user", "")
    cookie_sid = session.get("sid", "") if cookie_user == username else None
    cookie_generation = _cookie_generation() if cookie_user == username else None
    try:
        result = repo.login_or_resume(
            username,
            requested_session_id=str(uuid.uuid4()),
            cookie_session_id=cookie_sid,
            cookie_generation=cookie_generation,
            policy=policy,
        )
    except repo.ActiveSessionConflict as conflict:
        logger.info("annotator_login rejected reason=session_active")
        token = _issue_takeover_token(conflict)
        body = {
            "success": False,
            "code": "session_active",
            "error": str(conflict),
            "active_session": {
                "last_seen_at": repo._iso(conflict.last_seen_at),
                "login_time": repo._iso(conflict.login_time),
            },
            "takeover_token": token,
            "takeover_token_expires_in": policy.takeover_token_seconds,
        }
        return _no_store(jsonify(body)), 409
    except repo.ForbiddenError as error:
        if error.code == "account_deactivated":
            logger.info("annotator_login rejected reason=account_deactivated")
        raise
    logger.info("annotator_login mode=%s", result["mode"])
    _bind_annotator_cookie(result)
    return _no_store(jsonify({
        "success": True,
        "username": result["username"],
        "session": _public_session_payload(result),
    }))


@app.route("/api/login/takeover", methods=["POST"])
def api_login_takeover():
    data = json_object()
    username = required_string(data, "username")
    token = required_string(data, "takeover_token")
    policy = annotator_session_policy()
    try:
        payload = _takeover_serializer().loads(
            token, max_age=policy.takeover_token_seconds,
        )
    except SignatureExpired as exc:
        raise repo.ValidationError(
            "Takeover confirmation expired. Sign in again.",
            code="takeover_token_expired",
        ) from exc
    except BadSignature as exc:
        raise repo.ValidationError(
            "Takeover confirmation is invalid.",
            code="takeover_token_invalid",
        ) from exc
    if not isinstance(payload, dict) or payload.get("purpose") != TAKEOVER_PURPOSE:
        raise repo.ValidationError(
            "Takeover confirmation is invalid.",
            code="takeover_token_invalid",
        )
    if payload.get("username") != username:
        raise repo.ValidationError(
            "Takeover confirmation does not match this username.",
            code="takeover_token_invalid",
        )
    try:
        observed_generation = int(payload["observed_generation"])
        fingerprint = str(payload["observed_session_fingerprint"])
    except (KeyError, TypeError, ValueError) as exc:
        raise repo.ValidationError(
            "Takeover confirmation is invalid.",
            code="takeover_token_invalid",
        ) from exc
    result = repo.force_takeover(
        username,
        requested_session_id=str(uuid.uuid4()),
        observed_generation=observed_generation,
        observed_session_fingerprint=fingerprint,
        policy=policy,
    )
    logger.info("annotator_login mode=forced_takeover")
    _bind_annotator_cookie(result)
    return _no_store(jsonify({
        "success": True,
        "username": result["username"],
        "session": _public_session_payload(result),
    }))


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Ends the web session only — the user's unfinished assignment is kept."""
    username = session.get("user", "")
    sid = session.get("sid", "")
    generation = _cookie_generation()
    if username and sid:
        repo.logout(username, sid, generation)
    session.clear()
    return _no_store(jsonify({"success": True}))


@app.route("/api/current-user")
def api_current_user():
    user = current_user()
    if not user:
        state = inspect_request_session()
        if state.status == "account_deactivated":
            return jsonify({
                "user": None,
                "error": SESSION_STATUS_MESSAGES["account_deactivated"],
                "code": "account_deactivated",
                "redirect": _session_reason_redirect("account_deactivated"),
            }), 403
        code = (
            "not_authenticated"
            if state.status in ("logged_out", "not_authenticated", "")
            else state.status
        )
        if not session.get("user"):
            code = "not_authenticated"
        return jsonify({
            "user": None,
            "error": SESSION_STATUS_MESSAGES.get(code, "Not authenticated"),
            "code": code,
            "redirect": _session_reason_redirect(code),
        }), 401
    policy = annotator_session_policy()
    state = inspect_request_session()
    return _no_store(jsonify({
        "user": user["username"],
        "session": {
            "server_time": repo._iso(state.server_time),
            "idle_expires_at": repo._iso(user.get("idle_expires_at")),
            "absolute_expires_at": repo._iso(user.get("absolute_expires_at")),
            "heartbeat_seconds": policy.heartbeat_seconds,
            "idle_warning_seconds": policy.idle_warning_seconds,
            "offline_draft_retention_days": policy.offline_draft_retention_days,
        },
    }))


# ============================================================
# Admin authentication API
# ============================================================
@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    configured = str(app.config.get("ADMIN_KEY_SHA256", "")).strip().lower()
    if len(configured) != 64 or not re.fullmatch(r"[0-9a-f]{64}", configured):
        return jsonify({"error": "Admin authentication is not configured"}), 503

    ip = _request_ip()
    if _admin_login_rate_limited(ip):
        return jsonify({"error": "Too many login attempts. Try again later."}), 429

    data = json_object()
    key = data.get("key", data.get("admin_key"))
    if not isinstance(key, str) or not key.strip():
        raise repo.ValidationError("key must be a non-empty string")
    key = key.strip()
    if len(key) > 512 or not _admin_key_matches(key):
        limited = _admin_login_rate_limited(ip, record_failure=True)
        try:
            repo.record_admin_auth_event(
                success=False,
                key_id=str(app.config["ADMIN_KEY_ID"]),
                ip_hash=_request_ip_hash(),
                user_agent=request.headers.get("User-Agent", "")[:500],
                details={"rate_limited": limited},
            )
        except Exception:  # noqa: BLE001 - auth failure must stay generic
            logger.warning("Could not persist failed admin login audit")
        if limited:
            return jsonify({"error": "Too many login attempts. Try again later."}), 429
        return jsonify({"error": "Invalid admin credentials"}), 401

    _clear_admin_login_failures(ip)
    old_token = request.cookies.get(_admin_cookie_name(), "")
    if old_token:
        repo.revoke_admin_session(_digest(old_token))

    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    admin = repo.create_admin_session(
        key_id=str(app.config["ADMIN_KEY_ID"]),
        token_digest=_digest(raw_token),
        csrf_digest=_digest(csrf_token),
        idle_seconds=int(app.config.get(
            "ADMIN_SESSION_IDLE_SECONDS",
            app.config["ADMIN_IDLE_TTL_SECONDS"],
        )),
        absolute_seconds=int(app.config.get(
            "ADMIN_SESSION_ABSOLUTE_SECONDS",
            app.config["ADMIN_ABSOLUTE_TTL_SECONDS"],
        )),
        ip_hash=_request_ip_hash(),
        user_agent=request.headers.get("User-Agent", "")[:500],
    )
    try:
        repo.record_admin_auth_event(
            success=True,
            key_id=str(app.config["ADMIN_KEY_ID"]),
            ip_hash=_request_ip_hash(),
            user_agent=request.headers.get("User-Agent", "")[:500],
            details={"admin_session_id": str(admin["id"])},
        )
    except Exception:  # noqa: BLE001 - a valid login should not fail on audit telemetry
        logger.warning("Could not persist successful admin login audit")

    response = jsonify({
        "authenticated": True,
        "key_id": admin.get("key_id", app.config["ADMIN_KEY_ID"]),
        "csrf_token": csrf_token,
        "idle_expires_at": admin.get("idle_expires_at"),
        "absolute_expires_at": admin.get("absolute_expires_at"),
    })
    response.set_cookie(
        _admin_cookie_name(),
        raw_token,
        max_age=int(app.config.get(
            "ADMIN_SESSION_ABSOLUTE_SECONDS",
            app.config["ADMIN_ABSOLUTE_TTL_SECONDS"],
        )),
        httponly=True,
        secure=bool(app.config.get(
            "ADMIN_SESSION_COOKIE_SECURE",
            app.config["ADMIN_COOKIE_SECURE"],
        )),
        samesite="Strict",
        path="/",
    )
    response.set_cookie(
        _admin_csrf_cookie_name(),
        csrf_token,
        max_age=int(app.config.get(
            "ADMIN_SESSION_ABSOLUTE_SECONDS",
            app.config["ADMIN_ABSOLUTE_TTL_SECONDS"],
        )),
        httponly=False,
        secure=bool(app.config.get(
            "ADMIN_SESSION_COOKIE_SECURE",
            app.config["ADMIN_COOKIE_SECURE"],
        )),
        samesite="Strict",
        path="/",
    )
    return response


@app.route("/api/admin/session")
@admin_required
def api_admin_session():
    csrf_token = request.cookies.get(_admin_csrf_cookie_name(), "")
    csrf_valid = bool(csrf_token) and secrets.compare_digest(
        _digest(csrf_token), str(request.admin.get("csrf_digest", ""))
    )
    return jsonify({
        "authenticated": True,
        "key_id": request.admin.get("key_id"),
        "csrf_token": csrf_token if csrf_valid else None,
        "idle_expires_at": request.admin.get("idle_expires_at"),
        "absolute_expires_at": request.admin.get("absolute_expires_at"),
    })


@app.route("/api/admin/logout", methods=["POST"])
@admin_write_required
def api_admin_logout():
    token = request.cookies.get(_admin_cookie_name(), "")
    if token:
        repo.revoke_admin_session(_digest(token))
    response = jsonify({"success": True})
    response.delete_cookie(
        _admin_cookie_name(),
        secure=bool(app.config.get(
            "ADMIN_SESSION_COOKIE_SECURE",
            app.config["ADMIN_COOKIE_SECURE"],
        )),
        httponly=True,
        samesite="Strict",
        path="/",
    )
    response.delete_cookie(
        _admin_csrf_cookie_name(),
        secure=bool(app.config.get(
            "ADMIN_SESSION_COOKIE_SECURE",
            app.config["ADMIN_COOKIE_SECURE"],
        )),
        httponly=False,
        samesite="Strict",
        path="/",
    )
    return response


def _heartbeat_payload(result: dict) -> dict:
    return {
        "ok": True,
        "server_time": repo._iso(result["server_time"]),
        "idle_expires_at": repo._iso(result["idle_expires_at"]),
        "absolute_expires_at": repo._iso(result["absolute_expires_at"]),
    }


@app.route("/api/heartbeat")
@login_required
def api_heartbeat():
    """Deprecated presence-only heartbeat. Does not extend idle expiry."""
    logger.info("deprecated GET /api/heartbeat used")
    result = repo.session_heartbeat(
        request.annotator["username"],
        str(request.annotator["session_id"]),
        request.annotator["generation"],
        activity=False,
        policy=annotator_session_policy(),
    )
    response = jsonify(_heartbeat_payload(result))
    response.headers["Deprecation"] = "true"
    return _no_store(response)


@app.route("/api/session/heartbeat", methods=["POST"])
@login_required
def api_session_heartbeat():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if data != {} and not isinstance(data, dict):
        raise repo.ValidationError("Request body must be a JSON object")
    activity = False
    if isinstance(data, dict) and "activity" in data:
        activity = boolean_field(data, "activity")
    result = repo.session_heartbeat(
        request.annotator["username"],
        str(request.annotator["session_id"]),
        request.annotator["generation"],
        activity=activity,
        policy=annotator_session_policy(),
    )
    return _no_store(jsonify(_heartbeat_payload(result)))


@app.route("/api/health")
def api_health():
    try:
        with db_conn() as conn:
            versions = assert_schema_current(conn)
        return jsonify({"ok": True, "schema_versions": versions,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception as e:  # noqa: BLE001 — health must never raise
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/api/clientlog", methods=["POST"])
def api_clientlog():
    value = request.get_json(silent=True)
    data = value if isinstance(value, dict) else {}
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": session.get("user", "anonymous"),
        "type": str(data.get("type", "unknown"))[:50],
        "message": str(data.get("message", ""))[:500],
        "url": str(data.get("url", ""))[:300],
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
    }
    try:
        with open(SCRIPT_DIR / "client_errors.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("clientlog write failed: %s", e)
    return jsonify({"success": True})


# ============================================================
# Assignment API
# ============================================================
def _claim_filters_from_request(data: dict | None = None) -> dict:
    from annotation_metadata.contracts import ClaimRequest, parse_strict
    payload = dict(data or {})
    args = request.args
    if "source_scene" in args and "source_scene" not in payload:
        payload["source_scene"] = args.get("source_scene")
    if "batch_code" in args and "batch_code" not in payload:
        payload["batch_code"] = args.get("batch_code")
    if "source_confidence" in args and "source_confidence" not in payload:
        payload["source_confidence"] = args.get("source_confidence")
    parsed = parse_strict(ClaimRequest, payload)
    return {
        "source_scene": parsed.source_scene,
        "batch_code": parsed.batch_code,
        "source_confidence": parsed.source_confidence,
    }


@app.route("/api/assignment")
@login_required
def api_get_assignment():
    """Read-only: resume the current assignment; never claims implicitly."""
    user = request.annotator
    asg = repo.get_assignment(user["id"])
    if asg:
        return jsonify(asg)
    filters = _claim_filters_from_request()
    return jsonify({"assigned": False, "pool": repo.pool_state(user["id"], **filters)})


@app.route("/api/assignment/claim", methods=["POST"])
@login_required
def api_claim():
    user = request.annotator
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if data != {} and not isinstance(data, dict):
        raise repo.ValidationError("Request body must be a JSON object")
    filters = _claim_filters_from_request(data if isinstance(data, dict) else {})
    try:
        asg = repo.claim(
            user["fence"], **filters, policy=annotator_session_policy(),
        )
        return jsonify(asg)
    except repo.TaskPoolBusy as error:
        pool = repo.pool_state(user["id"], **filters)
        pool["available"] = 0
        pool["reason"] = "temporarily_busy"
        return jsonify({"assigned": False, "pool": pool,
                        "error": str(error)}), 409
    except repo.NoTaskAvailable as error:
        return jsonify({"assigned": False, "pool": repo.pool_state(user["id"], **filters),
                        "error": str(error)}), 409


@app.route("/api/assignment/current/abandon", methods=["POST"])
@login_required
def api_abandon():
    data = json_object()
    confirm = data.get("confirm")
    if not isinstance(confirm, bool):
        raise repo.ValidationError("confirm must be a boolean")
    result = repo.abandon(
        request.annotator["fence"],
        lease_token=required_string(data, "lease_token"),
        operation_id=required_string(data, "operation_id"),
        confirm=confirm,
        policy=annotator_session_policy(),
    )
    result["pool"] = repo.pool_state(request.annotator["id"])
    return jsonify(result)


@app.route("/api/assignment/current", methods=["PATCH", "POST"])
@login_required
def api_save_draft():
    data = json_object()
    if not isinstance(data.get("segments", []), list):
        raise repo.ValidationError("segments must be an array")
    request_hash = hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()
    result = repo.save_draft(
        request.annotator["fence"],
        lease_token=required_string(data, "lease_token"),
        expected_revision=integer_field(data, "expected_revision"),
        dirty_segments=data.get("segments", []),
        operation_id=required_string(data, "operation_id"),
        request_hash=request_hash,
        scene_review=data.get("scene_review"),
        policy=annotator_session_policy(),
    )
    return jsonify(result)


@app.route("/api/assignment/current/complete", methods=["POST"])
@login_required
def api_complete():
    data = json_object()
    if not isinstance(data.get("segments", []), list):
        raise repo.ValidationError("segments must be an array")
    request_hash = hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()
    result = repo.complete(
        request.annotator["fence"],
        lease_token=required_string(data, "lease_token"),
        expected_revision=integer_field(data, "expected_revision"),
        target_status=required_string(data, "target_status"),
        skip_reasons=string_list(data, "skip_reasons"),
        dirty_segments=data.get("segments", []),
        operation_id=required_string(data, "operation_id"),
        request_hash=request_hash,
        scene_review=data.get("scene_review"),
        policy=annotator_session_policy(),
    )
    if result.get("idempotent_replay"):
        return jsonify(result["response"])
    return jsonify(result)


# ============================================================
# History / completed / dashboard
# ============================================================
@app.route("/api/history/recent")
@login_required
def api_history():
    before_raw = request.args.get("before")
    before = None
    if before_raw not in (None, ""):
        try:
            before = int(before_raw)
        except (TypeError, ValueError) as exc:
            raise repo.ValidationError("before must be an integer") from exc
    return jsonify(repo.history_recent(
        request.annotator["id"],
        query_int("limit", 10, minimum=1, maximum=50),
        before,
    ))


@app.route("/api/completed")
@login_required
def api_completed():
    return jsonify(repo.completed_list(
        user_id=request.annotator["id"],
        status=request.args.get("status", "all"),
        q=(request.args.get("q") or "").strip(),
        limit=query_int("limit", 20, minimum=1, maximum=100),
        cursor=request.args.get("cursor"),
    ))


@app.route("/api/completed/<task_id>")
@login_required
def api_completed_detail(task_id: str):
    return jsonify(repo.completed_detail(request.annotator["id"], task_id))


@app.route("/api/completed/<task_id>/reopen", methods=["POST"])
@login_required
def api_completed_reopen(task_id: str):
    data = json_object()
    return jsonify(repo.reopen_completed(
        request.annotator["fence"],
        task_id=task_id,
        operation_id=required_string(data, "operation_id"),
        policy=annotator_session_policy(),
    ))


@app.route("/api/dashboard")
@login_required
def api_dashboard():
    dash = repo.dashboard()
    dash["pool"] = repo.pool_state(request.annotator["id"])
    dash["has_assignment"] = repo.has_assignment(request.annotator["id"])
    return jsonify(dash)


def _leaderboard_payload_fresh() -> dict:
    dash = repo.dashboard()
    return {
        "leaderboard": dash["leaderboard"],
        "total_annotated_duration_seconds": float(
            (dash.get("stats") or {}).get("annotated_duration_seconds") or 0.0
        ),
        "scene_options": repo.public_scene_options(),
        "annotation_speed": repo.public_annotation_speed(
            app.config["PUBLIC_DASHBOARD_TIMEZONE"],
        ),
    }


def clear_leaderboard_cache() -> None:
    with _leaderboard_cache_lock:
        _leaderboard_cache["payload"] = None
        _leaderboard_cache["expires_monotonic"] = 0.0


def public_leaderboard_payload() -> dict:
    """Serve a 60s in-process snapshot. Tests always recompute."""
    if app.config.get("TESTING"):
        return _leaderboard_payload_fresh()
    now = time.monotonic()
    with _leaderboard_cache_lock:
        cached = _leaderboard_cache["payload"]
        if cached is not None and _leaderboard_cache["expires_monotonic"] > now:
            return cached
    payload = _leaderboard_payload_fresh()
    with _leaderboard_cache_lock:
        _leaderboard_cache["payload"] = payload
        _leaderboard_cache["expires_monotonic"] = (
            time.monotonic() + LEADERBOARD_CACHE_SECONDS
        )
    return payload


@app.route("/api/leaderboard")
def api_leaderboard():
    """Lightweight public leaderboard and 28-day speed chart for the login page.

    Browsers must revalidate (no max-age). Origin load is bounded by the
    in-process TTL cache rather than HTTP cache, because this path has no
    Nginx proxy_cache.
    """
    response = jsonify(public_leaderboard_payload())
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ============================================================
# Admin dashboard / management API
# ============================================================
@app.route("/api/admin/overview")
@admin_required
def api_admin_overview():
    return jsonify(repo.admin_overview(admin_query_filters()))


@app.route("/api/admin/timeseries")
@admin_required
def api_admin_timeseries():
    return jsonify(repo.admin_timeseries(admin_query_filters()))


@app.route("/api/admin/annotators")
@admin_required
def api_admin_annotators():
    return jsonify(repo.admin_annotators(
        admin_query_filters(),
        limit=query_int("limit", 50, minimum=1, maximum=100),
        cursor=request.args.get("cursor"),
        presence_lease_seconds=int(app.config.get(
            "SESSION_PRESENCE_LEASE_SECONDS", SESSION_PRESENCE_LEASE_SECONDS,
        )),
    ))


@app.route("/api/admin/annotators/<annotator_id>")
@admin_required
def api_admin_annotator_detail(annotator_id: str):
    return jsonify(repo.admin_annotator_detail(
        annotator_id,
        admin_query_filters(),
        presence_lease_seconds=int(app.config.get(
            "SESSION_PRESENCE_LEASE_SECONDS", SESSION_PRESENCE_LEASE_SECONDS,
        )),
    ))


@app.route("/api/admin/annotators/<annotator_id>/annotations")
@admin_required
def api_admin_annotator_annotations(annotator_id: str):
    return jsonify(repo.admin_annotator_annotations(
        annotator_id,
        admin_query_filters(),
        limit=query_int("limit", 50, minimum=1, maximum=100),
        cursor=request.args.get("cursor"),
    ))


@app.route("/api/admin/annotations")
@admin_required
def api_admin_annotations():
    return jsonify(repo.admin_annotations(
        admin_query_filters(),
        limit=query_int("limit", 50, minimum=1, maximum=100),
        cursor=request.args.get("cursor"),
    ))


@app.route("/api/admin/tasks")
@admin_required
def api_admin_tasks():
    return jsonify(repo.admin_tasks(
        admin_query_filters(),
        limit=query_int("limit", 50, minimum=1, maximum=100),
        cursor=request.args.get("cursor"),
    ))


@app.route("/api/admin/quality")
@admin_required
def api_admin_quality():
    return jsonify(repo.admin_quality(
        admin_query_filters(),
        limit=query_int("limit", 50, minimum=1, maximum=100),
    ))


@app.route("/api/admin/annotations/<task_id>")
@admin_required
def api_admin_annotation_detail(task_id: str):
    return jsonify(repo.admin_annotation_detail(task_id))


@app.route("/api/admin/audit")
@admin_required
def api_admin_audit():
    return jsonify(repo.admin_audit(
        admin_query_filters(),
        limit=query_int("limit", 50, minimum=1, maximum=100),
        cursor=request.args.get("cursor"),
    ))


@app.route("/api/admin/annotations/revoke/preview", methods=["POST"])
@admin_write_required
def api_admin_revoke_preview():
    data = json_object()
    return jsonify(repo.admin_revoke_preview(
        annotator_id=required_string(data, "annotator_id"),
        items=admin_items(data),
        block_reclaim=boolean_field(data, "block_reclaim", True),
        release_conflicts=boolean_field(data, "release_conflicts", False),
    ))


@app.route("/api/admin/annotations/revoke", methods=["POST"])
@admin_write_required
@audit_admin_write_failures("revoke_annotations")
def api_admin_revoke():
    data = json_object()
    items = admin_items(data)
    if len(items) > 1:
        _require_admin_key_confirmation(data, purpose="batch_revoke")
    return jsonify(repo.admin_revoke(
        admin_session_id=str(request.admin["id"]),
        operation_id=required_string(data, "operation_id"),
        annotator_id=required_string(data, "annotator_id"),
        items=items,
        reason=required_string(data, "reason"),
        block_reclaim=boolean_field(data, "block_reclaim", True),
        confirm=boolean_field(data, "confirm", False),
        release_conflicts=boolean_field(data, "release_conflicts", False),
    ))


@app.route("/api/admin/annotations/restore", methods=["POST"])
@admin_write_required
@audit_admin_write_failures("restore_annotations")
def api_admin_restore():
    data = json_object()
    task_ids = data.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids or any(
        not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids
    ):
        raise repo.ValidationError("task_ids must be a non-empty array of strings")
    if len(task_ids) > 100:
        raise repo.ValidationError("A restore batch can contain at most 100 tasks")
    return jsonify(repo.admin_restore(
        admin_session_id=str(request.admin["id"]),
        operation_id=required_string(data, "operation_id"),
        admin_action_id=required_string(data, "admin_action_id"),
        task_ids=[task_id.strip() for task_id in task_ids],
        reason=required_string(data, "reason"),
        confirm=boolean_field(data, "confirm", False),
    ))


@app.route("/api/admin/assignments/<task_id>/release", methods=["POST"])
@admin_write_required
@audit_admin_write_failures("release_assignment")
def api_admin_release_assignment(task_id: str):
    data = json_object()
    return jsonify(repo.admin_release_assignment(
        admin_session_id=str(request.admin["id"]),
        operation_id=required_string(data, "operation_id"),
        task_id=task_id,
        reason=required_string(data, "reason"),
        confirm=boolean_field(data, "confirm", False),
    ))


@app.route("/api/admin/annotators/<annotator_id>/deactivate/preview", methods=["POST"])
@admin_write_required
def api_admin_deactivate_preview(annotator_id: str):
    return jsonify(repo.admin_deactivate_preview(annotator_id))


@app.route("/api/admin/annotators/<annotator_id>/deactivate", methods=["POST"])
@admin_write_required
@audit_admin_write_failures("deactivate_annotator")
def api_admin_deactivate(annotator_id: str):
    data = json_object()
    _require_admin_key_confirmation(data, purpose="deactivate_annotator")
    return jsonify(repo.admin_deactivate(
        admin_session_id=str(request.admin["id"]),
        operation_id=required_string(data, "operation_id"),
        annotator_id=annotator_id,
        reason=required_string(data, "reason"),
        confirm=boolean_field(data, "confirm", False),
        confirm_username=required_string(data, "confirm_username"),
    ))


# ============================================================
# Audio / waveform
# ============================================================
def _safe_audio_path(rel_path: str) -> Path | None:
    audio_dir = Path(app.config["AUDIO_DIR"]).resolve()
    candidate = (audio_dir / rel_path).resolve()
    try:
        candidate.relative_to(audio_dir)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


@app.route("/api/audio/<task_id>")
@login_required
def serve_audio(task_id: str):
    media = repo.authorized_media(request.annotator["id"], task_id)
    rel_path = media["rel_path"]
    path = _safe_audio_path(rel_path)
    if path is None:
        return jsonify({"error": "not found"}), 404
    mimetype = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
    accel_prefix = app.config.get("AUDIO_ACCEL_PREFIX", "")
    if accel_prefix and not app.testing:
        from flask import make_response
        resp = make_response("")
        resp.headers["X-Accel-Redirect"] = accel_prefix.rstrip("/") + "/" + quote(rel_path, safe="/")
        resp.headers["Content-Type"] = mimetype
    else:
        resp = send_file(str(path), mimetype=mimetype, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/api/waveform/<task_id>")
@login_required
def api_waveform(task_id: str):
    wf = repo.authorized_media(request.annotator["id"], task_id)["waveform_b64"]
    if wf is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"waveform_b64": wf})


@app.route("/api/admin/audio/<task_id>")
@admin_required
def serve_admin_audio(task_id: str):
    media = repo.admin_authorized_media(task_id)
    rel_path = media["rel_path"]
    path = _safe_audio_path(rel_path)
    if path is None:
        return jsonify({"error": "not found"}), 404
    mimetype = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
    accel_prefix = app.config.get("AUDIO_ACCEL_PREFIX", "")
    if accel_prefix and not app.testing:
        from flask import make_response
        response = make_response("")
        response.headers["X-Accel-Redirect"] = (
            accel_prefix.rstrip("/") + "/" + quote(rel_path, safe="/")
        )
        response.headers["Content-Type"] = mimetype
    else:
        response = send_file(str(path), mimetype=mimetype, conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/api/admin/waveform/<task_id>")
@admin_required
def api_admin_waveform(task_id: str):
    waveform = repo.admin_authorized_media(task_id)["waveform_b64"]
    if waveform is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"waveform_b64": waveform})


# ============================================================
# Startup / main
# ============================================================
def _init_app_config() -> dict:
    config = load_config()
    secret = config.get("secret_key", "")
    if not secret:
        secret = secrets.token_hex(32)
        config["secret_key"] = secret
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    app.secret_key = secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_REFRESH_EACH_REQUEST"] = False
    try:
        timeout_minutes = int(config.get("session_timeout_minutes", SESSION_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        timeout_minutes = SESSION_TIMEOUT_MINUTES
    timeout_minutes = max(5, min(timeout_minutes, 24 * 60))
    try:
        absolute_hours = int(config.get(
            "session_absolute_timeout_hours", SESSION_ABSOLUTE_TIMEOUT_HOURS,
        ))
    except (TypeError, ValueError):
        absolute_hours = SESSION_ABSOLUTE_TIMEOUT_HOURS
    absolute_hours = max(1, min(absolute_hours, 24))
    idle_seconds = timeout_minutes * 60
    absolute_seconds = max(idle_seconds, min(absolute_hours * 3600, 24 * 3600))
    try:
        heartbeat_seconds = int(config.get(
            "session_presence_heartbeat_seconds", SESSION_PRESENCE_HEARTBEAT_SECONDS,
        ))
    except (TypeError, ValueError):
        heartbeat_seconds = SESSION_PRESENCE_HEARTBEAT_SECONDS
    heartbeat_seconds = max(15, min(heartbeat_seconds, 120))
    try:
        presence_lease = int(config.get(
            "session_presence_lease_seconds", SESSION_PRESENCE_LEASE_SECONDS,
        ))
    except (TypeError, ValueError):
        presence_lease = SESSION_PRESENCE_LEASE_SECONDS
    presence_lease = max(heartbeat_seconds * 3, presence_lease)
    try:
        takeover_seconds = int(config.get(
            "session_takeover_token_seconds", SESSION_TAKEOVER_TOKEN_SECONDS,
        ))
    except (TypeError, ValueError):
        takeover_seconds = SESSION_TAKEOVER_TOKEN_SECONDS
    takeover_seconds = max(30, min(takeover_seconds, 300))
    try:
        activity_throttle = int(config.get(
            "session_activity_throttle_seconds", SESSION_ACTIVITY_THROTTLE_SECONDS,
        ))
    except (TypeError, ValueError):
        activity_throttle = SESSION_ACTIVITY_THROTTLE_SECONDS
    try:
        offline_days = int(config.get(
            "offline_draft_retention_days", OFFLINE_DRAFT_RETENTION_DAYS,
        ))
    except (TypeError, ValueError):
        offline_days = OFFLINE_DRAFT_RETENTION_DAYS
    session_secure_raw = os.environ.get(
        "ANNOTATION_SESSION_COOKIE_SECURE",
        str(config.get("session_cookie_secure", False)),
    ).strip().lower()
    app.config["SESSION_COOKIE_SECURE"] = session_secure_raw in {
        "1", "true", "yes", "on",
    }
    app.config["PERMANENT_SESSION_LIFETIME"] = absolute_seconds
    app.config["SESSION_TTL_SECONDS"] = idle_seconds
    app.config["SESSION_ABSOLUTE_SECONDS"] = absolute_seconds
    app.config["SESSION_PRESENCE_HEARTBEAT_SECONDS"] = heartbeat_seconds
    app.config["SESSION_PRESENCE_LEASE_SECONDS"] = presence_lease
    app.config["SESSION_TAKEOVER_TOKEN_SECONDS"] = takeover_seconds
    app.config["SESSION_ACTIVITY_THROTTLE_SECONDS"] = max(1, activity_throttle)
    app.config["OFFLINE_DRAFT_RETENTION_DAYS"] = max(1, offline_days)
    app.config["SESSION_IDLE_WARNING_SECONDS"] = SESSION_IDLE_WARNING_SECONDS
    app.config["AUDIO_DIR"] = str(
        (SCRIPT_DIR / config.get("audio_dir", "./audio")).resolve()
        if not Path(config.get("audio_dir", "./audio")).is_absolute()
        else Path(config["audio_dir"])
    )
    app.config["AUDIO_ACCEL_PREFIX"] = config.get("audio_accel_prefix", "")
    app.config["PUBLIC_DASHBOARD_TIMEZONE"] = resolve_public_dashboard_timezone(config)

    app.config["ADMIN_KEY_SHA256"] = os.environ.get(
        "ANNOTATION_ADMIN_KEY_SHA256",
        str(config.get("admin_key_sha256", "")),
    ).strip().lower()
    app.config["ADMIN_KEY_ID"] = os.environ.get(
        "ANNOTATION_ADMIN_KEY_ID",
        str(config.get("admin_key_id", "primary")),
    ).strip() or "primary"
    app.config["ADMIN_IDLE_TTL_SECONDS"] = max(
        300,
        min(int(os.environ.get("ANNOTATION_ADMIN_IDLE_SECONDS", "1800")), 8 * 3600),
    )
    app.config["ADMIN_ABSOLUTE_TTL_SECONDS"] = max(
        app.config["ADMIN_IDLE_TTL_SECONDS"],
        min(int(os.environ.get("ANNOTATION_ADMIN_ABSOLUTE_SECONDS", "28800")), 24 * 3600),
    )
    secure_raw = os.environ.get(
        "ANNOTATION_ADMIN_COOKIE_SECURE",
        str(config.get("admin_cookie_secure", False)),
    ).strip().lower()
    admin_cookie_secure = secure_raw in {"1", "true", "yes", "on"}
    app.config["ADMIN_COOKIE_SECURE"] = admin_cookie_secure
    app.config["ADMIN_COOKIE_NAME"] = os.environ.get(
        "ANNOTATION_ADMIN_COOKIE_NAME",
        "__Host-admin_session" if admin_cookie_secure else "admin_session",
    ).strip() or ("__Host-admin_session" if admin_cookie_secure else "admin_session")
    app.config["ADMIN_CSRF_COOKIE_NAME"] = os.environ.get(
        "ANNOTATION_ADMIN_CSRF_COOKIE_NAME",
        "__Host-admin_csrf" if admin_cookie_secure else "admin_csrf",
    ).strip() or ("__Host-admin_csrf" if admin_cookie_secure else "admin_csrf")
    if app.config["ADMIN_COOKIE_NAME"].startswith("__Host-") and not admin_cookie_secure:
        raise RuntimeError("__Host- admin cookies require ANNOTATION_ADMIN_COOKIE_SECURE=true")
    if app.config["ADMIN_CSRF_COOKIE_NAME"].startswith("__Host-") and not admin_cookie_secure:
        raise RuntimeError("__Host- admin CSRF cookies require secure cookies")
    app.config["ADMIN_LOGIN_WINDOW_SECONDS"] = 300
    app.config["ADMIN_LOGIN_MAX_FAILURES"] = 5
    trusted_proxy_raw = os.environ.get(
        "ANNOTATION_ADMIN_TRUSTED_PROXIES", "127.0.0.1,::1"
    )
    try:
        app.config["ADMIN_TRUSTED_PROXIES"] = {
            str(ipaddress.ip_address(item.strip()))
            for item in trusted_proxy_raw.split(",") if item.strip()
        }
    except ValueError as exc:
        raise RuntimeError(
            "ANNOTATION_ADMIN_TRUSTED_PROXIES must contain IP addresses"
        ) from exc
    return config


from annotation_metadata.routes import register_metadata_routes
register_metadata_routes(
    app,
    login_required=login_required,
    admin_required=admin_required,
    admin_write_required=admin_write_required,
    json_object=json_object,
    admin_query_filters=admin_query_filters,
)
from annotation_quality.routes import register_cross_check_routes
register_cross_check_routes(
    app,
    login_required=login_required,
    admin_required=admin_required,
    admin_write_required=admin_write_required,
    json_object=json_object,
    admin_query_filters=admin_query_filters,
    audit_admin_write_failures=audit_admin_write_failures,
)

_init_app_config()


def main():
    config = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", "-d", default=config.get("audio_dir", "./audio"))
    parser.add_argument("--port", "-p", type=int, default=config.get("port", 8080))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    audio_dir = Path(args.audio_dir)
    if not audio_dir.is_absolute():
        audio_dir = SCRIPT_DIR / audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)
    app.config["AUDIO_DIR"] = str(audio_dir.resolve())

    # Fail fast on missing DB / schema (dev server only; gunicorn surfaces
    # the same error on first request).
    with db_conn() as conn:
        applied = assert_schema_current(conn)
    print(f"\n{'='*56}\n  Annotation Tool v5 (PostgreSQL)\n{'='*56}")
    print(f"  Audio dir: {app.config['AUDIO_DIR']}")
    print(f"  Schema migrations applied: {applied}")
    print(f"  URL: http://localhost:{args.port}\n{'='*56}\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
