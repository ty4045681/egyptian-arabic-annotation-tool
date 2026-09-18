"""Transactional data-access layer for the annotation platform.

All state transitions happen inside single PostgreSQL transactions here;
server.py contains no SQL. Raising a subclass of RepositoryError maps to
an HTTP error response in the API layer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Json

from annotation_metadata.taxonomy import (
    SCENE_DEFS,
    SCENE_ORDER,
    effective_source_scene,
    effective_source_scene_sql,
)
from db import db_tx

ALLOWED_SKIP_REASONS = {"noisy", "not_egyptian", "poor_quality"}
HEARTBEAT_MIN_INTERVAL_SECONDS = 30
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_ABSOLUTE_SECONDS = 20 * 3600
DEFAULT_PRESENCE_LEASE_SECONDS = 150
DEFAULT_HEARTBEAT_SECONDS = 30
DEFAULT_TAKEOVER_TOKEN_SECONDS = 60
DEFAULT_ACTIVITY_THROTTLE_SECONDS = 30
DEFAULT_OFFLINE_DRAFT_RETENTION_DAYS = 7
DEFAULT_IDLE_WARNING_SECONDS = 120
SESSION_EVENT_TYPES = frozenset({
    "login", "resume", "stale_takeover", "forced_takeover", "logout",
})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# Errors
# ============================================================
class RepositoryError(Exception):
    status = 500


class NotFoundError(RepositoryError):
    status = 404


class ConflictError(RepositoryError):
    status = 409

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class NoTaskAvailable(ConflictError):
    """No task currently satisfies the caller's claim conditions."""


class TaskPoolBusy(ConflictError):
    """A claimable task exists but is temporarily locked by another transaction."""


class ValidationError(RepositoryError):
    status = 400

    def __init__(self, message, *, code=None, field=None, details=None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.details = details


class ForbiddenError(RepositoryError):
    status = 403

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class RateLimitError(RepositoryError):
    status = 429


class RevisionConflict(ConflictError):
    """Optimistic-concurrency failure; carries the server's current revision."""

    def __init__(self, current_revision: int):
        super().__init__(f"revision conflict, server revision is {current_revision}")
        self.current_revision = current_revision


class ActiveSessionConflict(ConflictError):
    """A live same-name session is still within the presence lease."""

    def __init__(self, username: str, observed_generation: int,
                 last_seen_at: datetime, login_time: datetime,
                 session_fingerprint: str):
        super().__init__(
            "This name is currently active on another device."
        )
        self.code = "session_active"
        self.username = username
        self.observed_generation = observed_generation
        self.last_seen_at = last_seen_at
        self.login_time = login_time
        self.session_fingerprint = session_fingerprint


class SessionChangedConflict(ConflictError):
    """The takeover token no longer matches the live session."""

    def __init__(self, message="The other session changed. Sign in again to continue."):
        super().__init__(message)
        self.code = "session_changed"


class SessionFenceError(RepositoryError):
    """A write was attempted with a session that is no longer authoritative."""

    status = 401
    _messages = {
        "not_authenticated": "Not authenticated",
        "session_replaced": "Session was replaced by another login.",
        "idle_timeout": "Session expired after a period of inactivity.",
        "absolute_timeout": "Session reached its absolute time limit.",
    }

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or self._messages.get(code, "Not authenticated"))


@dataclass(frozen=True)
class SessionFence:
    user_id: uuid.UUID
    username: str
    session_id: uuid.UUID
    generation: int


@dataclass(frozen=True)
class SessionState:
    status: str
    fence: SessionFence | None
    idle_expires_at: datetime | None = None
    absolute_expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_activity_at: datetime | None = None
    login_time: datetime | None = None
    server_time: datetime | None = None


@dataclass(frozen=True)
class SessionPolicy:
    idle_seconds: int = DEFAULT_IDLE_SECONDS
    absolute_seconds: int = DEFAULT_ABSOLUTE_SECONDS
    presence_lease_seconds: int = DEFAULT_PRESENCE_LEASE_SECONDS
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    takeover_token_seconds: int = DEFAULT_TAKEOVER_TOKEN_SECONDS
    activity_throttle_seconds: int = DEFAULT_ACTIVITY_THROTTLE_SECONDS
    offline_draft_retention_days: int = DEFAULT_OFFLINE_DRAFT_RETENTION_DAYS
    idle_warning_seconds: int = DEFAULT_IDLE_WARNING_SECONDS


def default_session_policy() -> SessionPolicy:
    return SessionPolicy()


def session_fingerprint(session_id) -> str:
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()


def _iso(value) -> str | None:
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _as_fence(fence: SessionFence) -> SessionFence:
    if not isinstance(fence, SessionFence):
        raise ValidationError("session fence is required")
    return fence


def _interval_seconds(seconds: int):
    return timedelta(seconds=int(seconds))


def _operation_replay(cur, operation_id, user_id, route: str,
                      request_hash: str) -> dict | None:
    row = cur.execute(
        """SELECT user_id, route, request_hash, response_status, response
           FROM operations WHERE operation_id = %s""",
        (operation_id,),
    ).fetchone()
    if not row:
        return None
    if row[0] != user_id or row[1] != route or row[2] != request_hash:
        raise ConflictError("operation_id was already used for a different request")
    return {"status_code": row[3], "response": row[4]}


def _store_operation(cur, operation_id, user_id, route: str,
                     request_hash: str, response: dict,
                     status_code: int = 200) -> None:
    cur.execute(
        """INSERT INTO operations
               (operation_id, user_id, route, request_hash,
                response_status, response)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (operation_id, user_id, route, request_hash, status_code, Json(response)),
    )


# ============================================================
# Users / sessions
# ============================================================
def ensure_user(cur: psycopg.Cursor, username: str) -> dict:
    uid = uuid.uuid4()
    row = cur.execute(
        """INSERT INTO annotators (id, username) VALUES (%s, %s)
           ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username
           RETURNING id, username, status""",
        (uid, username),
    ).fetchone()
    from annotation_metadata.repository import ensure_default_scope
    ensure_default_scope(cur, row[0])
    return {"id": row[0], "username": row[1], "status": row[2]}


def _lock_active_annotator(cur, user_id):
    row = cur.execute(
        "SELECT id, username, status FROM annotators WHERE id = %s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if not row:
        raise NotFoundError("Annotator not found")
    if row[2] != "active":
        raise ForbiddenError(
            "Annotator account is deactivated",
            code="account_deactivated",
        )
    return row


def _policy(policy: SessionPolicy | None) -> SessionPolicy:
    return policy if policy is not None else default_session_policy()


def _record_session_event(cur, user_id, event_type: str, generation: int,
                          previous_generation=None, details=None) -> None:
    if event_type not in SESSION_EVENT_TYPES:
        raise ValidationError(f"unsupported session event_type: {event_type}")
    cur.execute(
        """INSERT INTO annotator_session_events
               (user_id, event_type, generation, previous_generation, details)
           VALUES (%s, %s, %s, %s, %s)""",
        (user_id, event_type, generation, previous_generation, Json(details or {})),
    )


def _replace_active_session(cur, user_id, session_id, policy: SessionPolicy,
                            previous_generation: int | None):
    generation = int(previous_generation or 0) + 1
    idle = _interval_seconds(policy.idle_seconds)
    absolute = _interval_seconds(policy.absolute_seconds)
    row = cur.execute(
        """INSERT INTO active_sessions (
               user_id, session_id, login_time, last_seen_at, last_activity_at,
               expires_at, absolute_expires_at, generation)
           VALUES (
               %s, %s, now(), now(), now(),
               LEAST(now() + %s, now() + %s),
               now() + %s,
               %s)
           ON CONFLICT (user_id) DO UPDATE SET
               session_id = EXCLUDED.session_id,
               login_time = now(),
               last_seen_at = now(),
               last_activity_at = now(),
               absolute_expires_at = EXCLUDED.absolute_expires_at,
               expires_at = EXCLUDED.expires_at,
               generation = EXCLUDED.generation
           RETURNING session_id, generation, expires_at, absolute_expires_at,
                     last_seen_at, login_time, last_activity_at""",
        (user_id, session_id, idle, absolute, absolute, generation),
    ).fetchone()
    return {
        "session_id": row[0],
        "generation": int(row[1]),
        "expires_at": row[2],
        "absolute_expires_at": row[3],
        "last_seen_at": row[4],
        "login_time": row[5],
        "last_activity_at": row[6],
    }


def _session_payload(user: dict, session_row: dict, mode: str, *,
                     reason: str | None = None) -> dict:
    fence = SessionFence(
        user_id=user["id"],
        username=user["username"],
        session_id=session_row["session_id"],
        generation=int(session_row["generation"]),
    )
    payload = {
        "id": user["id"],
        "username": user["username"],
        "session_id": session_row["session_id"],
        "generation": fence.generation,
        "fence": fence,
        "mode": mode,
        "idle_expires_at": session_row["expires_at"],
        "absolute_expires_at": session_row["absolute_expires_at"],
        "last_seen_at": session_row["last_seen_at"],
        "login_time": session_row.get("login_time"),
        "last_activity_at": session_row.get("last_activity_at"),
    }
    if reason:
        payload["reason"] = reason
    return payload


def _require_session_fence(cur, fence: SessionFence) -> None:
    """Hold SHARE on the session row, then classify failure reasons."""
    fence = _as_fence(fence)
    row = cur.execute(
        """SELECT session_id, generation,
                  expires_at > now() AS idle_valid,
                  absolute_expires_at > now() AS absolute_valid
             FROM active_sessions
            WHERE user_id = %s
            FOR SHARE""",
        (fence.user_id,),
    ).fetchone()
    if not row:
        raise SessionFenceError(code="not_authenticated")
    if str(row[0]) != str(fence.session_id) or int(row[1]) != int(fence.generation):
        raise SessionFenceError(code="session_replaced")
    if not row[3]:
        raise SessionFenceError(code="absolute_timeout")
    if not row[2]:
        raise SessionFenceError(code="idle_timeout")


def _touch_real_activity(cur, fence: SessionFence, *,
                         policy: SessionPolicy | None = None,
                         assignment: bool = True) -> None:
    """Refresh idle deadline from a genuine user write. Never extends absolute."""
    policy = _policy(policy)
    idle = _interval_seconds(policy.idle_seconds)
    cur.execute(
        """UPDATE active_sessions
              SET last_seen_at = now(),
                  last_activity_at = now(),
                  expires_at = LEAST(now() + %s, absolute_expires_at)
            WHERE user_id = %s
              AND session_id = %s
              AND generation = %s""",
        (idle, fence.user_id, fence.session_id, fence.generation),
    )
    if assignment:
        cur.execute(
            """UPDATE assignments
                  SET last_activity_at = now()
                WHERE user_id = %s""",
            (fence.user_id,),
        )


def _online_sql(alias: str = "ses") -> str:
    return (
        f"{alias}.expires_at > now() "
        f"AND {alias}.absolute_expires_at > now() "
        f"AND {alias}.last_seen_at > now() - %s"
    )


def active_session_exists(username: str, *,
                          presence_lease_seconds: int = DEFAULT_PRESENCE_LEASE_SECONDS
                          ) -> bool:
    lease = _interval_seconds(presence_lease_seconds)
    with db_tx() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT 1 FROM active_sessions s
               JOIN annotators u ON u.id = s.user_id
               WHERE u.username = %s AND u.status = 'active'
                 AND {_online_sql("s")}""",
            (username, lease),
        )
        return cur.fetchone() is not None


def inspect_session(username: str, session_id: str,
                    generation: int | None = None) -> SessionState:
    """Read-only classification. Does not refresh any timestamps."""
    if not username or not session_id:
        return SessionState(status="not_authenticated", fence=None)
    with db_tx() as conn, conn.cursor() as cur:
        now_row = cur.execute("SELECT now()").fetchone()[0]
        user = cur.execute(
            "SELECT id, username, status FROM annotators WHERE username = %s",
            (username,),
        ).fetchone()
        if not user:
            return SessionState(
                status="not_authenticated", fence=None, server_time=now_row,
            )
        if user[2] != "active":
            return SessionState(
                status="account_deactivated", fence=None, server_time=now_row,
            )
        row = cur.execute(
            """SELECT session_id, generation, expires_at, absolute_expires_at,
                      last_seen_at, last_activity_at, login_time
                 FROM active_sessions
                WHERE user_id = %s""",
            (user[0],),
        ).fetchone()
        if not row:
            return SessionState(
                status="logged_out", fence=None, server_time=now_row,
            )
        sid_match = str(row[0]) == str(session_id)
        if not sid_match:
            return SessionState(
                status="session_replaced",
                fence=None,
                idle_expires_at=row[2],
                absolute_expires_at=row[3],
                last_seen_at=row[4],
                last_activity_at=row[5],
                login_time=row[6],
                server_time=now_row,
            )
        if generation is not None and int(row[1]) != int(generation):
            return SessionState(
                status="session_replaced",
                fence=None,
                idle_expires_at=row[2],
                absolute_expires_at=row[3],
                last_seen_at=row[4],
                last_activity_at=row[5],
                login_time=row[6],
                server_time=now_row,
            )
        fence = SessionFence(
            user_id=user[0], username=user[1],
            session_id=row[0], generation=int(row[1]),
        )
        if row[3] <= now_row:
            return SessionState(
                status="absolute_timeout", fence=fence,
                idle_expires_at=row[2], absolute_expires_at=row[3],
                last_seen_at=row[4], last_activity_at=row[5],
                login_time=row[6], server_time=now_row,
            )
        if row[2] <= now_row:
            return SessionState(
                status="idle_timeout", fence=fence,
                idle_expires_at=row[2], absolute_expires_at=row[3],
                last_seen_at=row[4], last_activity_at=row[5],
                login_time=row[6], server_time=now_row,
            )
        return SessionState(
            status="valid", fence=fence,
            idle_expires_at=row[2], absolute_expires_at=row[3],
            last_seen_at=row[4], last_activity_at=row[5],
            login_time=row[6], server_time=now_row,
        )


def load_session_fence(user_id) -> SessionFence:
    """Current valid fence for a user id. Used by tests that only stored user_id."""
    uid = _validate_uuid(user_id, "user_id")
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """SELECT u.id, u.username, s.session_id, s.generation,
                      u.status, s.expires_at > now(), s.absolute_expires_at > now()
                 FROM annotators u
                 JOIN active_sessions s ON s.user_id = u.id
                WHERE u.id = %s""",
            (uid,),
        ).fetchone()
        if not row:
            raise SessionFenceError(code="not_authenticated")
        if row[4] != "active":
            raise ForbiddenError(
                "Annotator account is deactivated",
                code="account_deactivated",
            )
        if not row[6]:
            raise SessionFenceError(code="absolute_timeout")
        if not row[5]:
            raise SessionFenceError(code="idle_timeout")
        return SessionFence(
            user_id=row[0], username=row[1],
            session_id=row[2], generation=int(row[3]),
        )


def login_or_resume(username: str, requested_session_id: str, *,
                    cookie_session_id: str | None = None,
                    cookie_generation: int | None = None,
                    policy: SessionPolicy | None = None) -> dict:
    """Atomically resume, replace a stale/expired session, or reject a live one."""
    policy = _policy(policy)
    requested = _validate_uuid(requested_session_id, "session_id")
    with db_tx() as conn, conn.cursor() as cur:
        user = ensure_user(cur, username)
        locked = _lock_active_annotator(cur, user["id"])
        user = {"id": locked[0], "username": locked[1], "status": locked[2]}
        current = cur.execute(
            """SELECT session_id, generation, expires_at, absolute_expires_at,
                      last_seen_at, last_activity_at, login_time
                 FROM active_sessions
                WHERE user_id = %s
                FOR UPDATE""",
            (user["id"],),
        ).fetchone()
        now_row = cur.execute("SELECT now()").fetchone()[0]
        if current is not None:
            cookie_sid = cookie_session_id
            cookie_ok = (
                cookie_sid
                and str(current[0]) == str(cookie_sid)
                and (
                    cookie_generation is None
                    or int(current[1]) == int(cookie_generation)
                )
                and current[2] > now_row
                and current[3] > now_row
            )
            if cookie_ok:
                cur.execute(
                    """UPDATE active_sessions
                          SET last_seen_at = now()
                        WHERE user_id = %s
                    RETURNING session_id, generation, expires_at, absolute_expires_at,
                              last_seen_at, login_time, last_activity_at""",
                    (user["id"],),
                )
                updated = cur.fetchone()
                session_row = {
                    "session_id": updated[0], "generation": int(updated[1]),
                    "expires_at": updated[2], "absolute_expires_at": updated[3],
                    "last_seen_at": updated[4], "login_time": updated[5],
                    "last_activity_at": updated[6],
                }
                _record_session_event(
                    cur, user["id"], "resume", session_row["generation"],
                    previous_generation=session_row["generation"],
                )
                return _session_payload(user, session_row, "resume")

            idle_expired = current[2] <= now_row
            absolute_expired = current[3] <= now_row
            if idle_expired or absolute_expired:
                reason = (
                    "absolute_timeout" if absolute_expired
                    else "idle_timeout" if idle_expired
                    else "missing"
                )
                previous = int(current[1])
                replaced = _replace_active_session(
                    cur, user["id"], requested, policy, previous,
                )
                _record_session_event(
                    cur, user["id"], "login", replaced["generation"],
                    previous_generation=previous,
                    details={"reason": reason},
                )
                return _session_payload(user, replaced, "login", reason=reason)

            stale_cutoff = now_row - _interval_seconds(policy.presence_lease_seconds)
            if current[4] <= stale_cutoff:
                previous = int(current[1])
                replaced = _replace_active_session(
                    cur, user["id"], requested, policy, previous,
                )
                _record_session_event(
                    cur, user["id"], "stale_takeover", replaced["generation"],
                    previous_generation=previous,
                    details={"reason": "presence_lease_expired"},
                )
                return _session_payload(user, replaced, "stale_takeover")

            raise ActiveSessionConflict(
                username=user["username"],
                observed_generation=int(current[1]),
                last_seen_at=current[4],
                login_time=current[6],
                session_fingerprint=session_fingerprint(current[0]),
            )

        replaced = _replace_active_session(cur, user["id"], requested, policy, 0)
        _record_session_event(
            cur, user["id"], "login", replaced["generation"],
            previous_generation=None,
            details={"reason": "missing"},
        )
        return _session_payload(user, replaced, "login", reason="missing")


def login(username: str, session_id: str, ttl_seconds: int, **kwargs) -> dict:
    """Compatibility wrapper used by tests and smoke scripts."""
    absolute = kwargs.pop("absolute_seconds", None)
    if absolute is None:
        absolute = max(int(ttl_seconds), DEFAULT_ABSOLUTE_SECONDS)
    policy = SessionPolicy(
        idle_seconds=int(ttl_seconds),
        absolute_seconds=int(absolute),
        presence_lease_seconds=int(kwargs.pop(
            "presence_lease_seconds", DEFAULT_PRESENCE_LEASE_SECONDS,
        )),
        heartbeat_seconds=int(kwargs.pop(
            "heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS,
        )),
        takeover_token_seconds=int(kwargs.pop(
            "takeover_token_seconds", DEFAULT_TAKEOVER_TOKEN_SECONDS,
        )),
        activity_throttle_seconds=int(kwargs.pop(
            "activity_throttle_seconds", DEFAULT_ACTIVITY_THROTTLE_SECONDS,
        )),
        offline_draft_retention_days=int(kwargs.pop(
            "offline_draft_retention_days", DEFAULT_OFFLINE_DRAFT_RETENTION_DAYS,
        )),
    )
    return login_or_resume(
        username,
        requested_session_id=session_id,
        cookie_session_id=kwargs.pop("cookie_session_id", None),
        cookie_generation=kwargs.pop("cookie_generation", None),
        policy=policy,
    )


def login_actor(name: str, ttl_seconds: int = DEFAULT_IDLE_SECONDS, **kwargs) -> dict:
    """Create a session and return user, SID, generation, and fence."""
    return login(name, str(uuid.uuid4()), ttl_seconds, **kwargs)


def force_takeover(username: str, requested_session_id: str, *,
                   observed_generation: int,
                   observed_session_fingerprint: str,
                   policy: SessionPolicy | None = None) -> dict:
    """Replace a live session only if generation and SID fingerprint still match."""
    policy = _policy(policy)
    requested = _validate_uuid(requested_session_id, "session_id")
    with db_tx() as conn, conn.cursor() as cur:
        user = ensure_user(cur, username)
        locked = _lock_active_annotator(cur, user["id"])
        user = {"id": locked[0], "username": locked[1], "status": locked[2]}
        current = cur.execute(
            """SELECT session_id, generation
                 FROM active_sessions
                WHERE user_id = %s
                FOR UPDATE""",
            (user["id"],),
        ).fetchone()
        if (
            current is None
            or int(current[1]) != int(observed_generation)
            or session_fingerprint(current[0]) != observed_session_fingerprint
        ):
            raise SessionChangedConflict()
        previous = int(current[1])
        replaced = _replace_active_session(
            cur, user["id"], requested, policy, previous,
        )
        _record_session_event(
            cur, user["id"], "forced_takeover", replaced["generation"],
            previous_generation=previous,
        )
        return _session_payload(user, replaced, "forced_takeover")


def validate_session(username: str, session_id: str,
                     generation: int | None = None) -> dict | None:
    """Return the annotator if the cookie session is currently valid."""
    state = inspect_session(username, session_id, generation)
    if state.status != "valid" or state.fence is None:
        return None
    return {
        "id": state.fence.user_id,
        "username": state.fence.username,
        "session_id": state.fence.session_id,
        "generation": state.fence.generation,
        "fence": state.fence,
        "idle_expires_at": state.idle_expires_at,
        "absolute_expires_at": state.absolute_expires_at,
        "last_seen_at": state.last_seen_at,
    }


def logout(username: str, session_id: str, generation: int | None = None) -> None:
    """Drop only the matching web session. Assignments are intentionally kept."""
    with db_tx() as conn, conn.cursor() as cur:
        if generation is None:
            deleted = cur.execute(
                """DELETE FROM active_sessions s
                   USING annotators u
                   WHERE s.user_id = u.id
                     AND u.username = %s
                     AND s.session_id = %s
                   RETURNING s.user_id, s.generation""",
                (username, session_id),
            ).fetchone()
        else:
            deleted = cur.execute(
                """DELETE FROM active_sessions s
                   USING annotators u
                   WHERE s.user_id = u.id
                     AND u.username = %s
                     AND s.session_id = %s
                     AND s.generation = %s
                   RETURNING s.user_id, s.generation""",
                (username, session_id, int(generation)),
            ).fetchone()
        if deleted:
            _record_session_event(
                cur, deleted[0], "logout", int(deleted[1]),
                previous_generation=int(deleted[1]),
            )


def session_heartbeat(username: str, session_id: str,
                      generation: int | None = None, *,
                      activity: bool = False,
                      policy: SessionPolicy | None = None) -> dict:
    """Update presence; extend idle only for throttled genuine activity."""
    policy = _policy(policy)
    state = inspect_session(username, session_id, generation)
    if state.status != "valid" or state.fence is None:
        if state.status == "account_deactivated":
            raise ForbiddenError(
                "Annotator account is deactivated",
                code="account_deactivated",
            )
        code = state.status if state.status != "logged_out" else "not_authenticated"
        raise SessionFenceError(code=code)
    fence = state.fence
    idle = _interval_seconds(policy.idle_seconds)
    throttle = _interval_seconds(policy.activity_throttle_seconds)
    with db_tx() as conn, conn.cursor() as cur:
        if activity:
            row = cur.execute(
                """UPDATE active_sessions
                      SET last_seen_at = now(),
                          last_activity_at = CASE
                              WHEN last_activity_at < now() - %s THEN now()
                              ELSE last_activity_at END,
                          expires_at = CASE
                              WHEN last_activity_at < now() - %s
                              THEN LEAST(now() + %s, absolute_expires_at)
                              ELSE expires_at END
                    WHERE user_id = %s
                      AND session_id = %s
                      AND generation = %s
                      AND expires_at > now()
                      AND absolute_expires_at > now()
                    RETURNING now(), expires_at, absolute_expires_at, last_activity_at""",
                (throttle, throttle, idle,
                 fence.user_id, fence.session_id, fence.generation),
            ).fetchone()
            if row and row[3] is not None:
                cur.execute(
                    """UPDATE assignments
                          SET last_activity_at = now()
                        WHERE user_id = %s
                          AND last_activity_at < now() - %s""",
                    (fence.user_id, throttle),
                )
        else:
            row = cur.execute(
                """UPDATE active_sessions
                      SET last_seen_at = now()
                    WHERE user_id = %s
                      AND session_id = %s
                      AND generation = %s
                      AND expires_at > now()
                      AND absolute_expires_at > now()
                    RETURNING now(), expires_at, absolute_expires_at, last_activity_at""",
                (fence.user_id, fence.session_id, fence.generation),
            ).fetchone()
        if not row:
            # Re-classify rather than resurrect.
            again = inspect_session(username, session_id, generation)
            code = again.status if again.status != "logged_out" else "not_authenticated"
            if code == "valid":
                code = "not_authenticated"
            if code == "account_deactivated":
                raise ForbiddenError(
                    "Annotator account is deactivated",
                    code="account_deactivated",
                )
            raise SessionFenceError(code=code)
        return {
            "ok": True,
            "server_time": row[0],
            "idle_expires_at": row[1],
            "absolute_expires_at": row[2],
        }


def heartbeat(username: str, session_id: str, ttl_seconds: int,
              generation: int | None = None) -> dict:
    """Deprecated presence-only heartbeat. Does not extend idle expiry."""
    policy = SessionPolicy(
        idle_seconds=int(ttl_seconds),
        absolute_seconds=max(int(ttl_seconds), DEFAULT_ABSOLUTE_SECONDS),
    )
    return session_heartbeat(
        username, session_id, generation, activity=False, policy=policy,
    )


# ============================================================
# Task pool state
# ============================================================
_CLAIMABLE_TASKS_FROM = """
    FROM annotation_tasks t
    JOIN annotation_versions v
      ON v.task_id = t.id AND v.lifecycle = 'draft'
    WHERE t.status = 'pending' AND t.eligible
      AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)
"""


def _claimable_tasks_from(*, user_scoped: bool) -> str:
    """Return the shared SQL source used by pool counts and atomic claims."""
    if not user_scoped:
        return _CLAIMABLE_TASKS_FROM
    return (
        _CLAIMABLE_TASKS_FROM
        + """ AND NOT EXISTS (
                 SELECT 1 FROM task_annotator_blocks b
                 WHERE b.task_id = t.id AND b.user_id = %s
               )
               AND (t.reserved_for_user_id = %s OR t.reserved_for_user_id IS NULL)"""
    )


def pool_state(user_id: str | None = None, *, source_scene: str | None = None,
               batch_code: str | None = None,
               source_confidence: str | None = None) -> dict:
    """Return global totals and, when supplied, availability for one user."""
    uid = _validate_uuid(user_id, "user_id") if user_id is not None else None
    if uid is not None:
        from annotation_metadata.claiming import parse_claim_filters, pool_snapshot
        filters = parse_claim_filters(
            source_scene=source_scene, batch_code=batch_code,
            source_confidence=source_confidence,
        )
        with db_tx() as conn, conn.cursor() as cur:
            return pool_snapshot(cur, uid, filters)

    claimable_from = _claimable_tasks_from(user_scoped=False)
    with db_tx() as conn, conn.cursor() as cur:
        counts = dict(
            cur.execute(
                "SELECT status, count(*) FROM annotation_tasks GROUP BY status"
            ).fetchall()
        )
        total = sum(counts.values())
        annotated = counts.get("annotated", 0)
        skipped = counts.get("skipped", 0)
        pending = counts.get("pending", 0)
        from annotation_quality.queries import assignment_queue_counts
        assigned, cross_check_in_progress = assignment_queue_counts(cur)
        eligible_pending = cur.execute(
            "SELECT count(*) FROM annotation_tasks WHERE status = 'pending' AND eligible"
        ).fetchone()[0]
        available = cur.execute(
            "SELECT count(*) " + claimable_from,
        ).fetchone()[0]

    if available > 0:
        reason = "available"
    elif total == 0 or (pending > 0 and eligible_pending == 0):
        reason = "no_preprocessed"
    elif eligible_pending > 0:
        reason = "temporarily_all_assigned"
    else:
        reason = "all_completed"

    return {
        "total": total,
        "annotated": annotated,
        "skipped": skipped,
        "pending": pending,
        "assigned": assigned,
        "available": available,
        "cross_check_in_progress": cross_check_in_progress,
        "reason": reason,
    }


# ============================================================
# Assignment read / claim / abandon
# ============================================================
_ASSIGNMENT_QUERY = """
    SELECT a.task_id, a.mode, a.lease_token, a.assigned_at,
           t.rel_path, t.filename, t.folder, t.duration, t.status,
           v.id AS version_id, v.revision, v.skip_reasons,
           a.cross_check_round_id, r.state
    FROM assignments a
    JOIN annotation_tasks t ON t.id = a.task_id
    JOIN annotation_versions v ON v.id = a.working_version_id
    LEFT JOIN cross_check_rounds r ON r.id = a.cross_check_round_id
    WHERE a.user_id = %s
"""

_SEGMENT_PROTECTED_EXTRA = frozenset({
    "id", "start", "end", "duration", "asr_text", "text",
    "exclude_from_training", "segment_id", "start_s", "end_s",
    "annotator", "annotator_id", "username", "user_id",
    "submitted_by", "author", "original_text", "original_annotator",
    "original_annotator_id", "original_version_id",
    "credited_annotator_id",
})


def _row_to_assignment(row, segments: list, waveform_b64: str | None) -> dict:
    payload = {
        "assigned": True,
        "task_id": str(row[0]),
        "mode": row[1],
        "lease_token": str(row[2]),
        "assigned_at": row[3].isoformat() if row[3] else None,
        "rel_path": row[4],
        "filename": row[5],
        "folder": row[6],
        "duration": row[7],
        "status": row[8],
        "version_id": str(row[9]),
        "revision": row[10],
        "skip_reasons": list(row[11] or []),
        "segments": segments,
        "waveform_b64": waveform_b64,
        "resumed": True,
    }
    if row[1] == "cross_check" and row[12]:
        payload["cross_check"] = {
            "round_id": str(row[12]),
            "state": row[13] or "in_progress",
        }
    return payload


def _attach_assignment_metadata(cur, payload: dict, user_id) -> dict:
    from annotation_metadata.serializers import attach_metadata
    from annotation_quality.serializers import apply_assignment_visibility
    published = cur.execute(
        "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
        (payload["task_id"],),
    ).fetchone()
    attach_metadata(
        cur, payload, payload["task_id"],
        version_id=payload.get("version_id"),
        published_version_id=published[0] if published else None,
        assignment_user_id=user_id,
        include_draft_review=True,
        blind=payload.get("mode") == "cross_check",
    )
    return apply_assignment_visibility(payload)


def get_assignment(user_id: str) -> dict | None:
    """Read-only: the user's active assignment (never implicitly claims)."""
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(_ASSIGNMENT_QUERY, (user_id,)).fetchone()
        if not row:
            return None
        segments = _load_segments(cur, row[9])
        wf = _load_waveform(cur, row[0])
        payload = _row_to_assignment(row, segments, wf)
        return _attach_assignment_metadata(cur, payload, user_id)


def has_assignment(user_id: str) -> bool:
    uid = _validate_uuid(user_id, "user_id")
    with db_tx() as conn, conn.cursor() as cur:
        return cur.execute(
            "SELECT EXISTS (SELECT 1 FROM assignments WHERE user_id = %s)",
            (uid,),
        ).fetchone()[0]


def _raise_claim_pool_outcome(*statuses: str) -> None:
    if any(status == "busy" for status in statuses):
        raise TaskPoolBusy("Task pool is busy; retry claim")
    raise NoTaskAvailable("No task available to claim")


def _commit_normal_assignment(
    cur, fence: SessionFence, session_policy: SessionPolicy | None, uid,
    claim_policy_name: str, task_id, rel_path, filename, folder, duration,
    status, version_id, revision, best_id, best_scene, best_confidence,
) -> dict | None:
    from annotation_quality.repository import ensure_participant
    candidate_token = uuid.uuid4()
    inserted = cur.execute(
        """INSERT INTO assignments
               (user_id, task_id, working_version_id, mode, lease_token,
                base_revision, assigned_at, last_activity_at,
                claim_scene_code, claim_source_id, claim_policy,
                claim_confidence)
           VALUES (%s, %s, %s, 'annotation', %s, %s, now(), now(),
                   %s, %s, %s, %s)
           ON CONFLICT DO NOTHING
           RETURNING lease_token""",
        (uid, task_id, version_id, candidate_token, revision,
         best_scene, best_id, claim_policy_name, best_confidence),
    ).fetchone()
    if not inserted:
        return None
    cur.execute(
        "UPDATE annotation_tasks SET reserved_for_user_id = NULL, updated_at = now() WHERE id = %s",
        (task_id,),
    )
    cur.execute(
        """INSERT INTO annotation_events (user_id, task_id, version_id,
                                          event_type, to_status, details)
           VALUES (%s, %s, %s, 'claimed', 'pending', %s)""",
        (uid, task_id, version_id, Json({
            "mode": "annotation",
            "claim_scene_code": best_scene,
            "claim_confidence": best_confidence,
            "claim_policy": claim_policy_name,
            "claim_source_id": str(best_id) if best_id else None,
        })),
    )
    ensure_participant(cur, task_id, uid)
    segments = _load_segments(cur, version_id)
    wf = _load_waveform(cur, task_id)
    payload = {
        "assigned": True,
        "task_id": str(task_id),
        "mode": "annotation",
        "lease_token": str(inserted[0]),
        "assigned_at": utcnow().isoformat(),
        "rel_path": rel_path,
        "filename": filename,
        "folder": folder,
        "duration": duration,
        "status": status,
        "version_id": str(version_id),
        "revision": revision,
        "skip_reasons": [],
        "segments": segments,
        "waveform_b64": wf,
        "resumed": False,
    }
    _touch_real_activity(cur, fence, policy=session_policy)
    return _attach_assignment_metadata(cur, payload, uid)


def _try_claim_normal_pool(cur, fence, session_policy, uid, scope, filters,
                           claim_policy_name: str) -> tuple[dict | None, str]:
    from annotation_metadata.claiming import (
        candidate_exists_sql, fetch_claim_candidate, task_still_matches,
    )
    exists_sql, exists_params = candidate_exists_sql(scope, filters, uid)
    for _attempt in range(64):
        task_row = fetch_claim_candidate(cur, scope, filters, uid, claim_policy_name)
        if not task_row:
            candidate_exists = cur.execute(exists_sql, exists_params).fetchone()[0]
            if candidate_exists:
                return None, "busy"
            return None, "empty"
        (task_id, rel_path, filename, folder, duration, status, version_id,
         revision, _reserved, best_id, best_scene, best_confidence) = task_row
        still = task_still_matches(cur, task_id, scope, filters, uid)
        if still is None:
            continue
        _task_id, best_id, best_scene, best_confidence = still
        payload = _commit_normal_assignment(
            cur, fence, session_policy, uid, claim_policy_name,
            task_id, rel_path, filename, folder, duration, status,
            version_id, revision, best_id, best_scene, best_confidence,
        )
        if payload:
            return payload, "claimed"
    return None, "busy"


def _try_claim_reserved(cur, fence, session_policy, uid, scope, filters,
                        claim_policy_name: str) -> dict | None:
    from annotation_metadata.claiming import claim_lock_query, task_still_matches
    if not scope.can_claim:
        return None
    sql, params = claim_lock_query(
        scope, filters, uid, claim_policy_name, reserved_only=True,
    )
    reserved = cur.execute(sql, params).fetchone()
    if not reserved:
        return None
    (task_id, rel_path, filename, folder, duration, status, version_id,
     revision, _reserved, best_id, best_scene, best_confidence) = reserved
    still = task_still_matches(cur, task_id, scope, filters, uid)
    if still is None:
        return None
    _task_id, best_id, best_scene, best_confidence = still
    return _commit_normal_assignment(
        cur, fence, session_policy, uid, claim_policy_name,
        task_id, rel_path, filename, folder, duration, status,
        version_id, revision, best_id, best_scene, best_confidence,
    )


def _finish_cross_check_claim(cur, fence, session_policy, uid, created: dict) -> dict:
    payload = {
        "assigned": True,
        "task_id": str(created["task_id"]),
        "mode": "cross_check",
        "lease_token": str(created["lease_token"]),
        "assigned_at": utcnow().isoformat(),
        "status": created["status"],
        "version_id": str(created["draft_id"]),
        "revision": created["revision"],
        "rel_path": created["rel_path"],
        "filename": created["filename"],
        "folder": created["folder"],
        "duration": created["duration"],
        "skip_reasons": [],
        "resumed": False,
        "cross_check": {
            "round_id": str(created["round_id"]),
            "state": "in_progress",
        },
        "segments": _load_segments(cur, created["draft_id"]),
        "waveform_b64": _load_waveform(cur, created["task_id"]),
    }
    _touch_real_activity(cur, fence, policy=session_policy)
    return _attach_assignment_metadata(cur, payload, uid)


def claim(fence: SessionFence, *, source_scene: str | None = None,
          batch_code: str | None = None,
          source_confidence: str | None = None,
          policy: SessionPolicy | None = None,
          rng=None) -> dict:
    """Claim one task for the user, preferring migration-reserved drafts.

    An existing assignment is always resumed, ignoring the new scene selector.
    Raises NoTaskAvailable when no task matches the user's claim conditions,
    or TaskPoolBusy when matching rows are temporarily locked.
    """
    from annotation_metadata.claim_policy import get_claim_policy
    from annotation_metadata.claiming import assert_scope_allows, parse_claim_filters
    from annotation_metadata.queries import load_scope
    from annotation_quality.claiming import try_claim_cross_check
    from annotation_quality.repository import cross_check_allowed, load_settings

    fence = _as_fence(fence)
    uid = fence.user_id
    session_policy = _policy(policy)
    filters = parse_claim_filters(
        source_scene=source_scene, batch_code=batch_code,
        source_confidence=source_confidence,
    )
    claim_policy = get_claim_policy()
    with db_tx() as conn, conn.cursor() as cur:
        # Serialize claims for the same user. Different users still proceed
        # concurrently and SKIP LOCKED prevents task contention.
        _lock_active_annotator(cur, uid)
        _require_session_fence(cur, fence)
        existing = cur.execute(
            _ASSIGNMENT_QUERY, (uid,)
        ).fetchone()
        if existing:
            _touch_real_activity(cur, fence, policy=session_policy)
            segments = _load_segments(cur, existing[9])
            wf = _load_waveform(cur, existing[0])
            payload = _row_to_assignment(existing, segments, wf)
            return _attach_assignment_metadata(cur, payload, uid)

        scope = load_scope(cur, uid)
        assert_scope_allows(scope, filters)

        reserved = _try_claim_reserved(
            cur, fence, session_policy, uid, scope, filters, claim_policy.name,
        )
        if reserved:
            return reserved

        settings = load_settings(cur)
        allowed = cross_check_allowed(settings)
        claim_rng = rng if rng is not None else secrets.SystemRandom()
        prefer_cross = False
        if allowed:
            prefer_cross = (
                int(claim_rng.randrange(10000)) < int(settings["sampling_rate_bps"])
            )

        if prefer_cross:
            created, cc_status = try_claim_cross_check(
                cur, user_id=uid, scope=scope, filters=filters,
                claim_policy=claim_policy.name, settings=settings,
                rng=claim_rng,
            )
            if created:
                return _finish_cross_check_claim(
                    cur, fence, session_policy, uid, created,
                )
            payload, normal_status = _try_claim_normal_pool(
                cur, fence, session_policy, uid, scope, filters,
                claim_policy.name,
            )
            if payload:
                return payload
            _raise_claim_pool_outcome(cc_status, normal_status)

        payload, normal_status = _try_claim_normal_pool(
            cur, fence, session_policy, uid, scope, filters, claim_policy.name,
        )
        if payload:
            return payload
        if allowed:
            created, cc_status = try_claim_cross_check(
                cur, user_id=uid, scope=scope, filters=filters,
                claim_policy=claim_policy.name, settings=settings,
                rng=claim_rng,
            )
            if created:
                return _finish_cross_check_claim(
                    cur, fence, session_policy, uid, created,
                )
            _raise_claim_pool_outcome(normal_status, cc_status)
        _raise_claim_pool_outcome(normal_status)


def _lock_assignment(cur, user_id, lease_token: str | None):
    """Lock and validate the caller's assignment row + working version."""
    row = cur.execute(
        """SELECT a.task_id, a.working_version_id, a.mode, a.lease_token,
                  t.status, t.rel_path, a.cross_check_round_id
           FROM assignments a
           JOIN annotation_tasks t ON t.id = a.task_id
           WHERE a.user_id = %s
           FOR UPDATE OF a""",
        (user_id,),
    ).fetchone()
    if not row:
        raise ConflictError("No active assignment")
    if lease_token is not None and str(row[3]) != lease_token:
        raise ForbiddenError("Lease token does not match the current assignment")
    return {
        "task_id": row[0],
        "version_id": row[1],
        "mode": row[2],
        "lease_token": str(row[3]),
        "task_status": row[4],
        "rel_path": row[5],
        "cross_check_round_id": row[6],
    }


def _require_open_cross_check_round(cur, asg) -> None:
    round_id = asg.get("cross_check_round_id")
    if not round_id:
        raise ConflictError("Cross-check assignment is missing its round")
    row = cur.execute(
        """SELECT state FROM cross_check_rounds
           WHERE id = %s AND task_id = %s
           FOR UPDATE""",
        (round_id, asg["task_id"]),
    ).fetchone()
    if not row or row[0] != "in_progress":
        raise ConflictError("Cross-check round is no longer in progress")


def _cancel_in_progress_cross_check(
    cur, *, round_id, task_id, version_id, reason: str,
) -> None:
    """Cancel an in-progress round and abandon its draft. Caller deletes assignment."""
    if not round_id:
        raise ConflictError("Cross-check assignment is missing its round")
    row = cur.execute(
        """SELECT state FROM cross_check_rounds
           WHERE id = %s AND task_id = %s
           FOR UPDATE""",
        (round_id, task_id),
    ).fetchone()
    if not row or row[0] != "in_progress":
        raise ConflictError("Cross-check round is no longer in progress")
    cur.execute(
        """SELECT id FROM annotation_versions WHERE id = %s FOR UPDATE""",
        (version_id,),
    )
    cur.execute(
        """UPDATE cross_check_rounds
           SET revision = revision + 1,
               state = 'cancelled',
               termination_reason = %s,
               resolved_at = now(),
               updated_at = now()
           WHERE id = %s AND state = 'in_progress'""",
        (reason, round_id),
    )
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'abandoned', updated_at = now()
           WHERE id = %s AND lifecycle = 'draft'""",
        (version_id,),
    )


def abandon(fence: SessionFence, lease_token: str, operation_id: str, confirm: bool,
            policy: SessionPolicy | None = None) -> dict:
    if not confirm:
        raise ValidationError("Abandon requires explicit confirmation")
    fence = _as_fence(fence)
    uid = fence.user_id
    op_uuid = _validate_uuid(operation_id, "operation_id")
    request_hash = hashlib.sha256(json.dumps(
        {"lease_token": lease_token, "confirm": confirm}, sort_keys=True
    ).encode()).hexdigest()
    with db_tx() as conn, conn.cursor() as cur:
        _lock_active_annotator(cur, uid)
        _require_session_fence(cur, fence)
        prior = _operation_replay(cur, op_uuid, uid, "abandon", request_hash)
        if prior:
            return {**prior["response"], "idempotent_replay": True}
        asg = _lock_assignment(cur, uid, lease_token)
        if asg["mode"] == "cross_check":
            _cancel_in_progress_cross_check(
                cur, round_id=asg["cross_check_round_id"],
                task_id=asg["task_id"], version_id=asg["version_id"],
                reason="abandoned",
            )
        elif asg["mode"] == "revision":
            # Discard the revision draft; published data is untouched.
            cur.execute(
                """UPDATE annotation_versions
                   SET lifecycle = 'abandoned', updated_at = now()
                   WHERE id = %s AND lifecycle = 'draft'""",
                (asg["version_id"],),
            )
        # annotation mode: draft content is kept on the task, which simply
        # returns to the pool for the next claimer.
        cur.execute("DELETE FROM assignments WHERE user_id = %s", (uid,))
        response = {"success": True, "abandoned": True}
        _store_operation(cur, op_uuid, uid, "abandon", request_hash, response)
        if asg["mode"] == "cross_check":
            cur.execute(
                """INSERT INTO annotation_events
                       (operation_id, user_id, task_id, version_id, event_type,
                        from_status, to_status, details)
                   VALUES (%s, %s, %s, %s, 'cross_check_cancelled', %s, %s, %s)""",
                (
                    op_uuid, uid, asg["task_id"], asg["version_id"],
                    asg["task_status"], asg["task_status"],
                    Json({
                        "mode": "cross_check",
                        "round_id": str(asg["cross_check_round_id"]),
                        "termination_reason": "abandoned",
                    }),
                ),
            )
        else:
            cur.execute(
                """INSERT INTO annotation_events
                       (operation_id, user_id, task_id, version_id, event_type,
                        from_status, details)
                   VALUES (%s, %s, %s, %s, 'abandoned', %s, %s)""",
                (
                    op_uuid,
                    uid,
                    asg["task_id"],
                    asg["version_id"],
                    asg["task_status"],
                    Json({"mode": asg["mode"]}),
                ),
            )
        _touch_real_activity(cur, fence, policy=policy, assignment=False)
        return response


# ============================================================
# Segments / waveform helpers
# ============================================================
def _load_segments(cur, version_id) -> list[dict]:
    rows = cur.execute(
        """SELECT segment_id, start_s, end_s, duration, asr_text, text,
                  exclude_from_training, extra
           FROM segments WHERE version_id = %s ORDER BY segment_id""",
        (version_id,),
    ).fetchall()
    segs = []
    for r in rows:
        seg = {
            "id": r[0],
            "start": r[1],
            "end": r[2],
            "duration": r[3],
            "asr_text": r[4] or "",
            "text": r[5] or "",
            "exclude_from_training": bool(r[6]),
        }
        extra = r[7] if isinstance(r[7], dict) else None
        if extra:
            for key, value in extra.items():
                if key not in _SEGMENT_PROTECTED_EXTRA:
                    seg[key] = value
        segs.append(seg)
    return segs


def _load_waveform(cur, task_id) -> str | None:
    row = cur.execute(
        "SELECT payload FROM waveforms WHERE task_id = %s", (task_id,)
    ).fetchone()
    if not row:
        return None
    return base64.b64encode(bytes(row[0])).decode("ascii")


def _parse_dirty_segment(seg: dict) -> tuple[int, float, float, float, str, bool]:
    try:
        sid = int(seg["id"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError(f"segment entry missing valid id: {seg!r}")
    start, end, duration = _seg_times(seg)
    text = seg.get("text", "")
    if not isinstance(text, str):
        raise ValidationError(f"segment {sid}: text must be a string")
    exclude = bool(seg.get("exclude_from_training", False))
    return sid, start, end, duration, text, exclude


def _apply_dirty_segments(cur, version_id, dirty: list[dict]) -> None:
    """Update existing segments only; clients may not invent segment IDs."""
    if not dirty:
        return
    seen: set[int] = set()
    for seg in dirty:
        sid, start, end, duration, text, exclude = _parse_dirty_segment(seg)
        if sid in seen:
            raise ValidationError(f"segment {sid} appears more than once")
        seen.add(sid)
        cur.execute(
            """UPDATE segments
               SET start_s = %s, end_s = %s, duration = %s,
                   text = %s, exclude_from_training = %s
               WHERE version_id = %s AND segment_id = %s""",
            (start, end, duration, text, exclude, version_id, sid),
        )
        if cur.rowcount != 1:
            raise ValidationError(f"segment {sid} does not exist in this task")


def _merge_dirty_segments(segments: list[dict], dirty: list[dict]) -> list[dict]:
    """Apply dirty fields in memory using the same rules as save."""
    merged = {int(seg["id"]): dict(seg) for seg in segments}
    if not dirty:
        return [merged[int(seg["id"])] for seg in segments]
    seen: set[int] = set()
    for seg in dirty:
        sid, start, end, duration, text, exclude = _parse_dirty_segment(seg)
        if sid in seen:
            raise ValidationError(f"segment {sid} appears more than once")
        seen.add(sid)
        if sid not in merged:
            raise ValidationError(f"segment {sid} does not exist in this task")
        merged[sid].update({
            "id": sid,
            "start": start,
            "end": end,
            "duration": duration,
            "text": text,
            "exclude_from_training": exclude,
        })
    return [merged[int(seg["id"])] for seg in segments]


def _seg_times(seg: dict) -> tuple[float, float, float]:
    try:
        start = float(seg["start"])
        end = float(seg["end"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError(f"segment {seg.get('id')!r}: invalid time values")
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValidationError(
            f"segment {seg.get('id')!r}: requires 0 <= start < end"
        )
    return start, end, round(end - start, 3)


def _validate_segment_timeline(segments: list[dict], duration: float) -> None:
    previous_end = 0.0
    ordered = sorted(
        segments, key=lambda seg: (float(seg["start"]), int(seg["id"])),
    )
    for seg in ordered:
        start, end = float(seg["start"]), float(seg["end"])
        segment_id = seg["id"]
        if start < previous_end - 0.001:
            raise ValidationError(f"segment {segment_id} overlaps the previous segment")
        if end > duration + 0.001:
            raise ValidationError(f"segment {segment_id} ends after the audio duration")
        previous_end = end


def _validate_version_segments(cur, version_id, task_id) -> None:
    duration = float(cur.execute(
        "SELECT duration FROM annotation_tasks WHERE id = %s", (task_id,)
    ).fetchone()[0])
    rows = cur.execute(
        "SELECT segment_id, start_s, end_s FROM segments WHERE version_id = %s ORDER BY start_s, segment_id",
        (version_id,),
    ).fetchall()
    _validate_segment_timeline(
        [{"id": row[0], "start": row[1], "end": row[2]} for row in rows],
        duration,
    )


# ============================================================
# Draft save (autosave) and complete
# ============================================================
def save_draft(fence: SessionFence, lease_token: str, expected_revision: int,
               dirty_segments: list[dict], operation_id: str,
               request_hash: str, scene_review=None,
               policy: SessionPolicy | None = None) -> dict:
    fence = _as_fence(fence)
    uid = fence.user_id
    op_uuid = _validate_uuid(operation_id, "operation_id")
    with db_tx() as conn, conn.cursor() as cur:
        _lock_active_annotator(cur, uid)
        _require_session_fence(cur, fence)
        prior = _operation_replay(cur, op_uuid, uid, "save_draft", request_hash)
        if prior:
            return prior["response"]
        asg = _lock_assignment(cur, uid, lease_token)
        if asg["mode"] == "cross_check":
            _require_open_cross_check_round(cur, asg)
        row = cur.execute(
            "SELECT revision FROM annotation_versions WHERE id = %s FOR UPDATE",
            (asg["version_id"],),
        ).fetchone()
        if not row:
            raise ConflictError("Working version no longer exists")
        current = row[0]
        if current != int(expected_revision):
            raise RevisionConflict(current)
        _apply_dirty_segments(cur, asg["version_id"], dirty_segments)
        from annotation_metadata.reviews import apply_optional_review
        review, review_changed = apply_optional_review(
            cur, version_id=asg["version_id"], payload=scene_review,
            actor_user_id=uid, operation_id=op_uuid,
        )
        new_rev = current + 1
        cur.execute(
            """UPDATE annotation_versions
               SET revision = %s, human_modified = true,
                   modified_by_user_id = %s, updated_at = now()
               WHERE id = %s""",
            (new_rev, uid, asg["version_id"]),
        )
        response = {
            "success": True, "revision": new_rev,
            "scene_review": review,
            "scene_review_changed": review_changed,
        }
        _store_operation(cur, op_uuid, uid, "save_draft", request_hash, response)
        _touch_real_activity(cur, fence, policy=policy)
        return response


def complete(fence: SessionFence, lease_token: str, expected_revision: int,
             target_status: str, skip_reasons: list[str],
             dirty_segments: list[dict], operation_id: str,
             request_hash: str, scene_review=None,
             policy: SessionPolicy | None = None) -> dict:
    if target_status not in ("annotated", "skipped"):
        raise ValidationError("target_status must be 'annotated' or 'skipped'")
    if not isinstance(skip_reasons, list) or any(
        not isinstance(reason, str) for reason in skip_reasons
    ):
        raise ValidationError("skip_reasons must be an array of strings")
    if not isinstance(dirty_segments, list):
        raise ValidationError("segments must be an array")
    if target_status == "skipped":
        reasons = list(skip_reasons)
        if not reasons:
            raise ValidationError("Skip requires at least one skip reason")
        bad = set(reasons) - ALLOWED_SKIP_REASONS
        if bad:
            raise ValidationError(f"Invalid skip reasons: {sorted(bad)}")
    else:
        reasons = []

    fence = _as_fence(fence)
    uid = fence.user_id
    op_uuid = _validate_uuid(operation_id, "operation_id")

    with db_tx() as conn, conn.cursor() as cur:
        _lock_active_annotator(cur, uid)
        _require_session_fence(cur, fence)
        prior = _operation_replay(cur, op_uuid, uid, "complete", request_hash)
        if prior:
            return {"idempotent_replay": True,
                    "status_code": prior["status_code"], "response": prior["response"]}

        asg = _lock_assignment(cur, uid, lease_token)
        if asg["mode"] != "cross_check":
            return _complete_published_assignment(
                cur, fence, uid, asg, expected_revision, target_status,
                reasons, dirty_segments, op_uuid, request_hash, scene_review,
                policy,
            )
        # Cross-check compare runs after this txn releases user/assignment
        # write locks (plan 6.2).

    from annotation_quality.service import submit_cross_check
    return submit_cross_check(
        fence, lease_token, expected_revision, target_status, reasons,
        dirty_segments, op_uuid, request_hash, scene_review, policy,
    )


def _complete_published_assignment(
    cur, fence: SessionFence, uid, asg: dict, expected_revision: int,
    target_status: str, reasons: list[str], dirty_segments: list[dict],
    op_uuid, request_hash: str, scene_review, policy: SessionPolicy | None,
) -> dict:
    cur.execute(
        "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
        (asg["task_id"],),
    )
    row = cur.execute(
        "SELECT revision FROM annotation_versions WHERE id = %s FOR UPDATE",
        (asg["version_id"],),
    ).fetchone()
    if not row:
        raise ConflictError("Working version no longer exists")
    current = row[0]
    if current != int(expected_revision):
        raise RevisionConflict(current)

    _apply_dirty_segments(cur, asg["version_id"], dirty_segments)
    _validate_version_segments(cur, asg["version_id"], asg["task_id"])
    from annotation_metadata.reviews import apply_optional_review
    review, _review_changed = apply_optional_review(
        cur, version_id=asg["version_id"], payload=scene_review,
        actor_user_id=uid, operation_id=op_uuid,
    )
    if review is None:
        from annotation_metadata.repository import latest_review
        review = latest_review(cur, asg["version_id"])

    if target_status == "annotated":
        empty = cur.execute(
            """SELECT segment_id FROM segments
               WHERE version_id = %s AND exclude_from_training = false
                 AND btrim(text) = '' ORDER BY segment_id""",
            (asg["version_id"],),
        ).fetchall()
        if empty:
            ids = [str(r[0]) for r in empty]
            raise ValidationError(
                "Segments without text must be annotated or marked Bad Quality: "
                + ", ".join(ids)
            )

    # Publish: supersede any previous published version, flip task state.
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'superseded', updated_at = now()
           WHERE task_id = %s AND lifecycle = 'published' AND id <> %s""",
        (asg["task_id"], asg["version_id"]),
    )
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'published', target_status = %s, skip_reasons = %s,
               revision = revision + 1,
               human_modified = true, modified_by_user_id = %s,
               submitted_by_user_id = %s, submitted_at = now(), updated_at = now()
           WHERE id = %s""",
        (target_status, reasons, uid, uid, asg["version_id"]),
    )
    cur.execute(
        """UPDATE annotation_tasks
           SET status = %s, current_published_version_id = %s,
               reserved_for_user_id = NULL, updated_at = now()
           WHERE id = %s""",
        (target_status, asg["version_id"], asg["task_id"]),
    )
    response = {
        "success": True,
        "task_id": str(asg["task_id"]),
        "status": target_status,
        "skip_reasons": reasons,
        "scene_review": review,
    }
    _store_operation(cur, op_uuid, uid, "complete", request_hash, response)
    cur.execute(
        """INSERT INTO annotation_events
               (operation_id, user_id, task_id, version_id, event_type,
                from_status, to_status, details)
           VALUES (%s, %s, %s, %s, 'completed', %s, %s, %s)""",
        (op_uuid, uid, asg["task_id"], asg["version_id"],
         asg["task_status"], target_status,
         Json({
             "mode": asg["mode"], "skip_reasons": reasons,
             "scene_review_id": (review or {}).get("id"),
         })),
    )
    cur.execute("DELETE FROM assignments WHERE user_id = %s", (uid,))
    _touch_real_activity(cur, fence, policy=policy, assignment=False)
    return response


# ============================================================
# History (Back/Next)
# ============================================================
def history_recent(user_id: str, limit: int = 10, before_event_id: int | None = None) -> dict:
    uid = _validate_uuid(user_id, "user_id")
    limit = max(1, min(int(limit), 50))
    with db_tx() as conn, conn.cursor() as cur:
        params: list = [uid]
        where = "e.user_id = %s AND e.event_type = 'completed'"
        if before_event_id is not None:
            where += " AND e.id < %s"
            params.append(int(before_event_id))
        rows = cur.execute(
            f"""SELECT e.id, e.created_at, e.to_status, t.id, t.filename,
                       t.folder, t.duration, t.rel_path
                FROM annotation_events e
                JOIN annotation_tasks t ON t.id = e.task_id
                WHERE {where}
                ORDER BY e.id DESC
                LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "event_id": r[0],
                "completed_at": r[1].isoformat(),
                "status": r[2],
                "task_id": str(r[3]),
                "filename": r[4],
                "folder": r[5],
                "duration": r[6],
                "rel_path": r[7],
            }
            for r in rows
        ]
    return {
        "items": items,
        "next_cursor": items[-1]["event_id"] if has_more and items else None,
    }


# ============================================================
# Completed page (own records only)
# ============================================================
def completed_list(user_id: str, status: str = "all", q: str = "",
                   limit: int = 20, cursor: str | None = None) -> dict:
    uid = _validate_uuid(user_id, "user_id")
    if status not in ("all", "annotated", "skipped"):
        raise ValidationError("status must be all|annotated|skipped")
    limit = max(1, min(int(limit), 100))

    where = ["t.current_published_version_id IS NOT NULL",
             "v.submitted_by_user_id = %s"]
    params: list = [uid]
    if status != "all":
        where.append("t.status = %s")
        params.append(status)
    if q:
        where.append("(t.filename ILIKE %s OR t.folder ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like])
    if cursor:
        try:
            ts, vid = cursor.split("|", 1)
            uuid.UUID(vid)
            datetime.fromisoformat(ts)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("Invalid cursor") from exc
        where.append("(v.submitted_at, v.id) < (%s::timestamptz, %s::uuid)")
        params.extend([ts, vid])

    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""SELECT v.id, v.submitted_at, t.id, t.filename, t.folder,
                       t.status, t.duration, t.rel_path,
                       (SELECT count(*) FROM segments s WHERE s.version_id = v.id)
                FROM annotation_tasks t
                JOIN annotation_versions v ON v.id = t.current_published_version_id
                WHERE {' AND '.join(where)}
                ORDER BY v.submitted_at DESC, v.id DESC
                LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "task_id": str(r[2]),
                "version_id": str(r[0]),
                "submitted_at": r[1].isoformat() if r[1] else None,
                "filename": r[3],
                "folder": r[4],
                "status": r[5],
                "duration": r[6],
                "rel_path": r[7],
                "segment_count": r[8],
                "skip_reasons": [],
            }
            for r in rows
        ]
        # skip reasons + summary
        if items:
            vids = [i["version_id"] for i in items]
            cur.execute(
                "SELECT id, skip_reasons FROM annotation_versions WHERE id = ANY(%s)",
                (vids,),
            )
            skips = {str(r[0]): list(r[1] or []) for r in cur.fetchall()}
            for i in items:
                i["skip_reasons"] = skips.get(i["version_id"], [])

        summary_row = cur.execute(
            """SELECT
                 count(*) FILTER (WHERE t.status = 'annotated'),
                 count(*) FILTER (WHERE t.status = 'skipped'),
                 COALESCE(sum(t.duration) FILTER (WHERE t.status = 'annotated'), 0)
               FROM annotation_tasks t
               JOIN annotation_versions v ON v.id = t.current_published_version_id
               WHERE v.submitted_by_user_id = %s""",
            (uid,),
        ).fetchone()
        has_assignment = cur.execute(
            "SELECT EXISTS (SELECT 1 FROM assignments WHERE user_id = %s)", (uid,)
        ).fetchone()[0]

    next_cursor = None
    if has_more and items:
        last = rows[-1]
        next_cursor = f"{last[1].isoformat()}|{last[0]}"
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_assignment": has_assignment,
        "summary": {
            "annotated": summary_row[0],
            "skipped": summary_row[1],
            "duration_seconds": float(summary_row[2]),
        },
    }


def completed_detail(user_id: str, task_id: str) -> dict:
    uid = _validate_uuid(user_id, "user_id")
    tid = _validate_uuid(task_id, "task_id")
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """SELECT t.id, t.rel_path, t.filename, t.folder, t.duration,
                      t.status, t.category, t.preprocessed_at,
                      v.id, v.submitted_at, v.skip_reasons
               FROM annotation_tasks t
               JOIN annotation_versions v ON v.id = t.current_published_version_id
               WHERE t.id = %s""",
            (tid,),
        ).fetchone()
        if not row or not row[8]:
            raise NotFoundError("Task not found")
        detail = cur.execute(
            "SELECT submitted_by_user_id FROM annotation_versions WHERE id = %s",
            (row[8],),
        ).fetchone()
        if not detail or detail[0] != uid:
            raise ForbiddenError("You can only view your own submissions")
        segments = _load_segments(cur, row[8])
        wf = _load_waveform(cur, tid)
        payload = {
            "task_id": str(row[0]),
            "rel_path": row[1],
            "filename": row[2],
            "folder": row[3],
            "duration": row[4],
            "status": row[5],
            "category": row[6],
            "preprocessed_at": row[7].isoformat() if row[7] else None,
            "version_id": str(row[8]),
            "submitted_at": row[9].isoformat() if row[9] else None,
            "skip_reasons": list(row[10] or []),
            "segments": segments,
            "waveform_b64": wf,
        }
        from annotation_metadata.serializers import attach_metadata
        attach_metadata(
            cur, payload, tid, version_id=row[8],
            published_version_id=row[8],
        )
        return payload


def reopen_completed(fence: SessionFence, task_id: str, operation_id: str,
                     policy: SessionPolicy | None = None) -> dict:
    """Create a revision draft + assignment from the user's own submission."""
    fence = _as_fence(fence)
    uid = fence.user_id
    tid = _validate_uuid(task_id, "task_id")
    op_uuid = _validate_uuid(operation_id, "operation_id")
    with db_tx() as conn, conn.cursor() as cur:
        _lock_active_annotator(cur, uid)
        _require_session_fence(cur, fence)
        request_hash = hashlib.sha256(str(tid).encode()).hexdigest()
        prior = _operation_replay(cur, op_uuid, uid, "reopen", request_hash)
        if prior:
            return {**prior["response"], "idempotent_replay": True}
        existing = cur.execute(
            _ASSIGNMENT_QUERY, (uid,)
        ).fetchone()
        if existing:
            raise ConflictError(
                "Finish or abandon your current task before correcting another one"
            )

        row = cur.execute(
            "SELECT id, current_published_version_id, status FROM annotation_tasks WHERE id = %s FOR UPDATE",
            (tid,),
        ).fetchone()
        if not row:
            raise NotFoundError("Task not found")
        published_id = row[1]
        if not published_id:
            raise ConflictError("Task has no published version to correct")
        published = cur.execute(
            """SELECT submitted_by_user_id, skip_reasons, extra
               FROM annotation_versions WHERE id = %s""",
            (published_id,),
        ).fetchone()
        submitter = published[0]
        if submitter != uid:
            raise ForbiddenError("You can only correct your own submissions")

        open_round = cur.execute(
            """SELECT state FROM cross_check_rounds
               WHERE task_id = %s
                 AND state IN ('in_progress', 'awaiting_review')
               FOR UPDATE""",
            (tid,),
        ).fetchone()
        if open_round:
            raise ConflictError(
                "This task has an open cross-check and cannot be reopened",
                code="cross_check_active",
            )

        open_draft = cur.execute(
            "SELECT id FROM annotation_versions WHERE task_id = %s AND lifecycle = 'draft' FOR UPDATE",
            (tid,),
        ).fetchone()
        if open_draft:
            raise ConflictError("Task already has an open draft")

        next_no = cur.execute(
            "SELECT COALESCE(max(version_no), 0) + 1 FROM annotation_versions WHERE task_id = %s",
            (tid,),
        ).fetchone()[0]
        draft_id = uuid.uuid4()
        cur.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status,
                    base_version_id, revision, created_by_user_id,
                    skip_reasons, extra)
               VALUES (%s, %s, %s, 'draft', %s, %s, 0, %s, %s, %s)""",
            (draft_id, tid, next_no, row[2], published_id, uid,
             list(published[1] or []), Json(published[2] or {})),
        )
        cur.execute(
            """INSERT INTO segments (version_id, segment_id, start_s, end_s,
                                     duration, asr_text, text,
                                     exclude_from_training, extra)
               SELECT %s, segment_id, start_s, end_s, duration, asr_text, text,
                      exclude_from_training, extra
               FROM segments WHERE version_id = %s""",
            (draft_id, published_id),
        )
        lease_token = uuid.uuid4()
        cur.execute(
            """INSERT INTO assignments
                   (user_id, task_id, working_version_id, mode, lease_token,
                    base_revision, assigned_at, last_activity_at)
               VALUES (%s, %s, %s, 'revision', %s, 0, now(), now())""",
            (uid, tid, draft_id, lease_token),
        )
        response = {
            "success": True,
            "task_id": str(tid),
            "mode": "revision",
            "lease_token": str(lease_token),
        }
        _store_operation(cur, op_uuid, uid, "reopen", request_hash, response)
        cur.execute(
            """INSERT INTO annotation_events
                   (operation_id, user_id, task_id, version_id, event_type,
                    from_status, details)
               VALUES (%s, %s, %s, %s, 'reopened', %s, %s)""",
            (op_uuid, uid, tid, draft_id, row[2], Json({})),
        )
        _touch_real_activity(cur, fence, policy=policy)
        return response


# ============================================================
# Dashboard / leaderboard
# ============================================================
def dashboard() -> dict:
    from annotation_quality.queries import (
        credited_annotator_sql,
        cross_check_submitted_workload,
        unique_annotated_corpus,
    )

    credited = credited_annotator_sql()
    with db_tx() as conn, conn.cursor() as cur:
        counts = dict(
            cur.execute("SELECT status, count(*) FROM annotation_tasks GROUP BY status").fetchall()
        )
        total = sum(counts.values())
        annotated = counts.get("annotated", 0)
        skipped = counts.get("skipped", 0)
        _, annotated_duration = unique_annotated_corpus(cur)
        workload_count, workload_seconds = cross_check_submitted_workload(cur)
        lb_rows = cur.execute(
            f"""SELECT u.username,
                       count(*) FILTER (WHERE v.target_status = 'annotated')
                           AS annotated,
                       count(*) FILTER (WHERE v.target_status = 'skipped')
                           AS skipped,
                       COALESCE(
                           sum(t.duration) FILTER (
                               WHERE v.target_status = 'annotated'
                           ),
                           0
                       ) AS dur
                FROM annotation_tasks t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                LEFT JOIN annotators u ON u.id = {credited}
                WHERE u.id IS NOT NULL
                  AND v.lifecycle = 'published'
                GROUP BY u.username
                ORDER BY dur DESC, username"""
        ).fetchall()
    return {
        "stats": {
            "total": total,
            "annotated": annotated,
            "skipped": skipped,
            "pending": total - annotated - skipped,
            "percent_complete": (
                round((annotated + skipped) / total * 100, 1) if total else 0.0
            ),
            "annotated_duration_seconds": annotated_duration,
            "cross_check_submitted_count": workload_count,
            "cross_check_submitted_audio_seconds": workload_seconds,
        },
        "leaderboard": [
            {
                "user": r[0],
                "annotated": r[1],
                "skipped": r[2],
                "duration_seconds": float(r[3]),
                # legacy field kept for old clients
                "hours": round(float(r[3]) / 3600.0, 2),
            }
            for r in lb_rows
        ],
    }


PUBLIC_ANNOTATION_SPEED_WINDOW_DAYS = 28
PUBLIC_ANNOTATION_SPEED_WEEK_DAYS = 7
PUBLIC_ANNOTATION_SPEED_SQL = f"""
WITH eligible AS (
    SELECT t.id AS task_id,
           timezone(%s, v.submitted_at)::date AS day,
           t.duration
    FROM annotation_versions v
    JOIN annotation_tasks t
      ON t.current_published_version_id = v.id
     AND t.status = 'annotated'
    WHERE v.lifecycle = 'published'
      AND v.target_status = 'annotated'
      AND v.submitted_at IS NOT NULL
      AND v.submitted_at >= %s
      AND v.submitted_at < %s
),
task_scenes AS (
    SELECT DISTINCT
           eligible.task_id,
           eligible.day,
           eligible.duration,
           {effective_source_scene_sql("source.scene_code")} AS scene_code
    FROM eligible
    LEFT JOIN task_sources source
      ON source.task_id = eligible.task_id
     AND source.is_current
)
SELECT CAST(NULL AS text) AS scene_code,
       day,
       COALESCE(SUM(duration), 0) AS duration_seconds
FROM eligible
GROUP BY day
UNION ALL
SELECT scene_code,
       day,
       COALESCE(SUM(duration), 0) AS duration_seconds
FROM task_scenes
GROUP BY scene_code, day
"""


def public_scene_options() -> list[dict]:
    """Public login-page scene catalog: stable codes and English labels."""
    return [
        {"code": str(item["code"]), "label": str(item["label_en"])}
        for item in SCENE_DEFS
    ]


def _speed_days_from_map(by_day: dict, start_date, today, window_days: int) -> list[dict]:
    days = []
    for offset in range(window_days):
        day = start_date + timedelta(days=offset)
        days.append({
            "date": day.isoformat(),
            "duration_seconds": float(by_day.get(day, 0.0)),
            "is_partial": day == today,
        })
    return days


def _speed_weeks_from_days(days: list[dict]) -> list[dict]:
    weeks = []
    step = PUBLIC_ANNOTATION_SPEED_WEEK_DAYS
    for index in range(0, len(days), step):
        chunk = days[index:index + step]
        total = sum(item["duration_seconds"] for item in chunk)
        weeks.append({
            "start_date": chunk[0]["date"],
            "end_date": chunk[-1]["date"],
            "average_daily_duration_seconds": total / float(step),
        })
    return weeks


def public_annotation_speed(
    timezone_name: str,
    *,
    window_days: int = PUBLIC_ANNOTATION_SPEED_WINDOW_DAYS,
) -> dict:
    """Project-level daily added annotated audio for the public login chart.

    Counts currently effective published/annotated versions, bucketed by the
    version's ``submitted_at`` calendar date in ``timezone_name``. Duration
    comes from ``annotation_tasks.duration`` (the same "currently valid
    annotated audio" definition as the leaderboard). Root ``days``/``weeks``
    are the All-scenes series; ``by_scene`` repeats the window for each
    source scene. A task with several current source scenes appears in each
    of those filters, but only once in All scenes.
    """
    if window_days <= 0 or window_days % PUBLIC_ANNOTATION_SPEED_WEEK_DAYS != 0:
        raise ValidationError(
            "window_days must be a positive multiple of "
            f"{PUBLIC_ANNOTATION_SPEED_WEEK_DAYS}"
        )
    try:
        tz = ZoneInfo(str(timezone_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValidationError("Invalid timezone") from exc

    now_utc = utcnow()
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    start_date = today - timedelta(days=window_days - 1)
    start_utc = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).astimezone(
        timezone.utc
    )
    end_utc = datetime.combine(
        today + timedelta(days=1), datetime.min.time(), tzinfo=tz
    ).astimezone(timezone.utc)

    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            PUBLIC_ANNOTATION_SPEED_SQL,
            (str(timezone_name), start_utc, end_utc),
        ).fetchall()

    all_by_day = {}
    scene_by_day = {code: {} for code in SCENE_ORDER}
    for scene_code, day, duration in rows:
        if day is None or day < start_date or day > today:
            continue
        amount = float(duration or 0.0)
        if scene_code is None:
            all_by_day[day] = amount
            continue
        code = effective_source_scene(scene_code)
        if code not in scene_by_day:
            continue
        scene_by_day[code][day] = scene_by_day[code].get(day, 0.0) + amount

    days = _speed_days_from_map(all_by_day, start_date, today, window_days)
    by_scene = {}
    for code in SCENE_ORDER:
        scene_days = _speed_days_from_map(
            scene_by_day[code], start_date, today, window_days,
        )
        by_scene[code] = {
            "days": scene_days,
            "weeks": _speed_weeks_from_days(scene_days),
        }

    return {
        "timezone": str(timezone_name),
        "window_days": window_days,
        "from": start_date.isoformat(),
        "through": today.isoformat(),
        "generated_at": now_local.isoformat(timespec="seconds"),
        "days": days,
        "weeks": _speed_weeks_from_days(days),
        "by_scene": by_scene,
    }


# ============================================================
# Admin sessions, filters and read models
# ============================================================
def _require_sha256_digest(value: str, field: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValidationError(f"{field} must be a lowercase SHA-256 hex digest")
    return digest


def _positive_seconds(value, field: str) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a positive integer") from exc
    if seconds <= 0:
        raise ValidationError(f"{field} must be a positive integer")
    return seconds


def _admin_session_dict(row) -> dict:
    return {
        "id": str(row[0]),
        "key_id": row[1],
        "csrf_digest": row[2],
        "created_at": row[3].isoformat(),
        "last_seen_at": row[4].isoformat(),
        "idle_expires_at": row[5].isoformat(),
        "absolute_expires_at": row[6].isoformat(),
    }


def create_admin_session(key_id: str, token_digest: str, csrf_digest: str,
                         idle_seconds: int, absolute_seconds: int,
                         ip_hash: str | None = None,
                         user_agent: str | None = None) -> dict:
    token = _require_sha256_digest(token_digest, "token_digest")
    csrf = _require_sha256_digest(csrf_digest, "csrf_digest")
    idle = _positive_seconds(idle_seconds, "idle_seconds")
    absolute = _positive_seconds(absolute_seconds, "absolute_seconds")
    if not str(key_id or "").strip():
        raise ValidationError("key_id is required")
    now = utcnow()
    absolute_expires = now + timedelta(seconds=absolute)
    idle_expires = min(now + timedelta(seconds=idle), absolute_expires)
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """INSERT INTO admin_sessions
                   (key_id, token_digest, csrf_digest, created_at, last_seen_at,
                    idle_expires_at, absolute_expires_at, ip_hash, user_agent)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, key_id, csrf_digest, created_at, last_seen_at,
                         idle_expires_at, absolute_expires_at""",
            (str(key_id).strip(), token, csrf, now, now, idle_expires,
             absolute_expires, str(ip_hash) if ip_hash else None,
             str(user_agent)[:1000] if user_agent else None),
        ).fetchone()
        return _admin_session_dict(row)


def validate_admin_session(token_digest: str, idle_seconds: int,
                           touch: bool = True) -> dict | None:
    token = _require_sha256_digest(token_digest, "token_digest")
    idle = _positive_seconds(idle_seconds, "idle_seconds")
    now = utcnow()
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """SELECT id, key_id, csrf_digest, created_at, last_seen_at,
                      idle_expires_at, absolute_expires_at
               FROM admin_sessions
               WHERE token_digest = %s AND revoked_at IS NULL
                 AND idle_expires_at > %s AND absolute_expires_at > %s
               FOR UPDATE""",
            (token, now, now),
        ).fetchone()
        if not row:
            return None
        if not touch:
            return _admin_session_dict(row)
        new_idle = min(now + timedelta(seconds=idle), row[6])
        updated = cur.execute(
            """UPDATE admin_sessions
               SET last_seen_at = %s, idle_expires_at = %s
               WHERE id = %s
               RETURNING id, key_id, csrf_digest, created_at, last_seen_at,
                         idle_expires_at, absolute_expires_at""",
            (now, new_idle, row[0]),
        ).fetchone()
        return _admin_session_dict(updated)


def revoke_admin_session(token_digest: str) -> bool:
    token = _require_sha256_digest(token_digest, "token_digest")
    with db_tx() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE admin_sessions SET revoked_at = COALESCE(revoked_at, now())
               WHERE token_digest = %s AND revoked_at IS NULL""",
            (token,),
        )
        return cur.rowcount == 1


def record_admin_auth_event(success: bool, key_id: str, ip_hash: str,
                            user_agent: str,
                            details: dict | None = None) -> dict:
    """Persist a login audit record without ever receiving the plaintext key."""
    payload = {
        "success": bool(success),
        "key_id": str(key_id or "unknown"),
        "ip_hash": str(ip_hash or ""),
        "user_agent": str(user_agent or "")[:1000],
        "details": details or {},
    }
    request_hash = _canonical_request_hash(payload)
    action_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    action_type = "admin_auth_success" if success else "admin_auth_failure"
    with db_tx() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO admin_actions
                   (id, operation_id, admin_session_id, admin_key_id,
                    action_type, reason, request_hash, status, request,
                    summary, created_at, completed_at)
               VALUES (%s, %s, NULL, %s, %s, '', %s, %s, %s, %s,
                       now(), now())""",
            (action_id, operation_id, payload["key_id"], action_type,
             request_hash, "completed" if success else "failed",
             Json(payload), Json({"success": bool(success)})),
        )
    return {"action_id": str(action_id), "operation_id": str(operation_id)}


def record_admin_action_failure(admin_session_id: str, action_type: str,
                                reason: str, request_payload: dict,
                                error: str) -> dict:
    """Persist a failed high-risk attempt in its own committed transaction."""
    session_id = _validate_uuid(admin_session_id, "admin_session_id")
    payload = dict(request_payload or {})
    request_hash = _canonical_request_hash({
        "action_type": str(action_type), "request": payload,
        "error": str(error),
    })
    action_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    with db_tx() as conn, conn.cursor() as cur:
        session_row = cur.execute(
            "SELECT key_id FROM admin_sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        if not session_row:
            raise ForbiddenError("Admin session is no longer valid")
        cur.execute(
            """INSERT INTO admin_actions
                   (id, operation_id, admin_session_id, admin_key_id,
                    action_type, reason, request_hash, status, request,
                    summary, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed', %s, %s,
                       now(), now())""",
            (action_id, operation_id, session_id, session_row[0],
             str(action_type), str(reason or "")[:4000], request_hash,
             Json(payload), Json({"error": str(error)[:1000]})),
        )
    return {"action_id": str(action_id), "operation_id": str(operation_id)}


def _parse_admin_datetime(value, field: str, *,
                          assumed_timezone=timezone.utc) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"Invalid {field} datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assumed_timezone)
    return parsed.astimezone(timezone.utc)


def _normalize_admin_filters(filters: dict | None) -> dict:
    raw = dict(filters or {})
    bucket = str(raw.get("bucket") or "day")
    if bucket not in ("day", "week"):
        raise ValidationError("bucket must be day|week")
    status = str(raw.get("status") or "all")
    if status not in ("all", "pending", "assigned", "annotated", "skipped"):
        raise ValidationError(
            "status must be all|pending|assigned|annotated|skipped"
        )
    signal = str(raw.get("signal") or "all")
    if signal not in ("all", "unusually_fast", "stale_assignment"):
        raise ValidationError(
            "signal must be all|unusually_fast|stale_assignment"
        )
    annotator = raw.get("annotator_id")
    timezone_name = str(raw.get("timezone") or "UTC")[:100]
    try:
        selected_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("Invalid timezone") from exc
    result = {
        "from": _parse_admin_datetime(
            raw.get("from", raw.get("from_", raw.get("date_from"))), "from",
            assumed_timezone=selected_timezone,
        ),
        "to": _parse_admin_datetime(
            raw.get("to", raw.get("date_to")), "to",
            assumed_timezone=selected_timezone,
        ),
        "timezone": timezone_name,
        "bucket": bucket,
        "status": status,
        "signal": signal,
        "annotator_id": (
            _validate_uuid(annotator, "annotator_id") if annotator else None
        ),
        "folder": str(raw.get("folder") or "")[:1000],
        "category": str(raw.get("category") or "")[:1000],
        "q": str(raw.get("q") or "")[:1000],
        "annotator_status": str(raw.get("annotator_status") or "all"),
        "action_type": str(raw.get("action_type") or ""),
        "lifecycle": str(raw.get("lifecycle") or "all"),
    }
    if result["from"] and result["to"] and result["from"] >= result["to"]:
        raise ValidationError("from must be earlier than to")
    if result["annotator_status"] not in ("all", "active", "deactivated"):
        raise ValidationError("annotator_status must be all|active|deactivated")
    if result["lifecycle"] not in (
        "all", "published", "revoked", "superseded"
    ):
        raise ValidationError(
            "lifecycle must be all|published|revoked|superseded"
        )
    from annotation_metadata.contracts import TaskFilter, parse_strict
    result["metadata"] = parse_strict(TaskFilter, {
        "source_scene": raw.get("source_scene") or None,
        "source_confidence": raw.get("source_confidence") or None,
        "batch_code": raw.get("batch_code") or None,
        "review_status": raw.get("review_status") or None,
        "prediction_scene": raw.get("prediction_scene") or None,
        "human_scene": raw.get("human_scene") or None,
    })
    return result


def _applied_admin_filters(filters: dict) -> dict:
    metadata = filters.get("metadata")
    dumped = metadata.model_dump() if metadata is not None else {}
    applied = {
        "status": filters.get("status"),
        "folder": filters.get("folder") or None,
        "category": filters.get("category") or None,
        "q": filters.get("q") or None,
        "annotator_id": str(filters["annotator_id"]) if filters.get("annotator_id") else None,
        "lifecycle": filters.get("lifecycle"),
        "timezone": filters.get("timezone"),
        "from": filters["from"].isoformat() if filters.get("from") else None,
        "to": filters["to"].isoformat() if filters.get("to") else None,
        **{key: value for key, value in dumped.items() if value},
    }
    return applied


def _admin_list_filter_digest(filters: dict) -> str:
    """Bind list cursors to the complete normalized filter, not only metadata."""
    metadata = filters.get("metadata")
    payload = {
        "status": filters.get("status"),
        "folder": filters.get("folder") or "",
        "category": filters.get("category") or "",
        "q": filters.get("q") or "",
        "annotator_id": str(filters["annotator_id"]) if filters.get("annotator_id") else None,
        "lifecycle": filters.get("lifecycle"),
        "timezone": filters.get("timezone"),
        "from": filters["from"].isoformat() if filters.get("from") else None,
        "to": filters["to"].isoformat() if filters.get("to") else None,
        "metadata": metadata.model_dump() if metadata is not None else {},
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()[:16]


def _admin_statistics_definitions() -> dict:
    return {
        "task_count": (
            "unique tasks; repeated sources in one scene or batch are counted once"
        ),
        "duration_seconds": (
            "sum of raw audio duration over unique tasks; never SUM(DISTINCT duration)"
        ),
        "scene_groups_overlap": True,
        "batch_groups_overlap": True,
        "confidence_buckets": (
            "mutually exclusive highest MATCHING source confidence in the "
            "current filter context"
        ),
        "review_statuses": (
            "published version latest non-superseded review; draft reviews "
            "are not published verification"
        ),
        "unknown_sources": (
            "legacy tasks with no current sources plus explicit NULL "
            "scene_code evidence, once per task"
        ),
        "activity_date_range": (
            "from/to apply to activity cards; corpus, queue, source groups, "
            "confidence buckets, and review groups use the current task snapshot"
        ),
        "snapshot": (
            "one overview response uses a single REPEATABLE READ snapshot"
        ),
    }


def _admin_task_filter_sql(filters: dict, *, task_alias="t",
                           version_alias="v") -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if filters["status"] == "assigned":
        clauses.append(
            f"EXISTS (SELECT 1 FROM assignments status_assignment "
            f"WHERE status_assignment.task_id = {task_alias}.id)"
        )
    elif filters["status"] != "all":
        clauses.append(f"{task_alias}.status = %s")
        params.append(filters["status"])
    if filters["folder"]:
        clauses.append(f"{task_alias}.folder = %s")
        params.append(filters["folder"])
    if filters["category"]:
        clauses.append(f"{task_alias}.category = %s")
        params.append(filters["category"])
    if filters["q"]:
        clauses.append(
            f"({task_alias}.filename ILIKE %s OR {task_alias}.rel_path ILIKE %s)"
        )
        like = f"%{filters['q']}%"
        params.extend([like, like])
    if filters["annotator_id"]:
        from annotation_quality.queries import credited_annotator_sql
        credited = credited_annotator_sql(version_alias)
        clauses.append(
            f"({credited} = %s OR EXISTS ("
            f"SELECT 1 FROM assignments af WHERE af.task_id = {task_alias}.id "
            "AND af.user_id = %s))"
        )
        params.extend([filters["annotator_id"], filters["annotator_id"]])
    timestamp = f"COALESCE({version_alias}.submitted_at, {task_alias}.created_at)"
    if filters["from"]:
        clauses.append(f"{timestamp} >= %s")
        params.append(filters["from"])
    if filters["to"]:
        clauses.append(f"{timestamp} < %s")
        params.append(filters["to"])
    metadata = filters.get("metadata")
    if metadata is not None:
        from annotation_metadata.queries import metadata_filter_sql
        meta_sql, meta_params = metadata_filter_sql(metadata, task_alias=task_alias)
        if meta_sql != "true":
            clauses.append(meta_sql)
            params.extend(meta_params)
    return (" AND ".join(clauses) if clauses else "true"), params


def admin_overview(filters: dict | None = None) -> dict:
    normalized = _normalize_admin_filters(filters)
    # Corpus/queue cards describe the current system snapshot and must not
    # shrink when the operator changes the historical activity date range.
    snapshot_filters = {**normalized, "from": None, "to": None}
    where, params = _admin_task_filter_sql(snapshot_filters)
    with db_tx() as conn, conn.cursor() as cur:
        # REPEATABLE READ snapshot. Temp table is session-local (not a durable
        # write); READ ONLY is omitted so the snapshot can be materialized once.
        trace = os.environ.get("ANNOTATION_OVERVIEW_TRACE")
        started = time.perf_counter()
        last = started

        def _mark(label: str) -> None:
            nonlocal last
            if not trace:
                return
            now = time.perf_counter()
            print(
                f"overview_trace {label}: {(now - last) * 1000:.1f} ms "
                f"(total {(now - started) * 1000:.1f})",
                flush=True,
            )
            last = now

        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SET LOCAL work_mem = '256MB'")
        cur.execute("SET LOCAL temp_buffers = '128MB'")
        cur.execute("SET LOCAL jit = off")
        cur.execute("SET LOCAL max_parallel_workers_per_gather = 2")
        cur.execute("SET LOCAL parallel_setup_cost = 10")
        cur.execute("SET LOCAL parallel_tuple_cost = 0.001")
        as_of = cur.execute("SELECT now()").fetchone()[0]
        cur.execute(
            f"""CREATE TEMP TABLE _overview_matched ON COMMIT DROP AS
                SELECT t.id, t.duration, t.status, t.eligible,
                       t.reserved_for_user_id, t.current_published_version_id,
                       t.baseline_version_id, t.category, t.created_at,
                       (d.id IS NOT NULL) AS has_draft,
                       (a.user_id IS NOT NULL) AS has_assignment,
                       a.last_activity_at AS assignment_last_activity_at
                FROM annotation_tasks t
                LEFT JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                LEFT JOIN annotation_versions d
                  ON d.task_id = t.id AND d.lifecycle = 'draft'
                LEFT JOIN assignments a ON a.task_id = t.id
                WHERE {where}""",
            params,
        )
        _mark("matched")
        row = cur.execute(
            """SELECT
                   count(*) AS total_audio_count,
                   COALESCE(sum(t.duration), 0) AS total_duration,
                   count(*) FILTER (WHERE t.status = 'annotated'),
                   COALESCE(sum(t.duration) FILTER
                       (WHERE t.status = 'annotated'), 0),
                   count(*) FILTER (WHERE t.status = 'skipped'),
                   COALESCE(sum(t.duration) FILTER
                       (WHERE t.status = 'skipped'), 0),
                   count(*) FILTER (WHERE t.status = 'pending'),
                   COALESCE(sum(t.duration) FILTER
                       (WHERE t.status = 'pending'), 0),
                   count(*) FILTER (WHERE t.status = 'pending' AND t.has_assignment),
                   count(*) FILTER (WHERE t.status = 'pending' AND t.eligible
                       AND t.has_draft AND NOT t.has_assignment),
                   count(*) FILTER (WHERE t.status = 'pending' AND NOT t.eligible),
                   count(*) FILTER (WHERE t.status = 'pending'
                       AND t.reserved_for_user_id IS NOT NULL),
                   min(t.created_at) FILTER (WHERE t.status = 'pending')
               FROM _overview_matched t"""
        ).fetchone()
        segment_row = cur.execute(
            """SELECT count(s.segment_id), COALESCE(sum(s.duration), 0),
                       count(s.segment_id) FILTER
                           (WHERE NOT s.exclude_from_training),
                       COALESCE(sum(s.duration) FILTER
                           (WHERE NOT s.exclude_from_training), 0),
                       count(s.segment_id) FILTER
                           (WHERE s.exclude_from_training),
                       COALESCE(sum(s.duration) FILTER
                           (WHERE s.exclude_from_training), 0)
                FROM _overview_matched t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                LEFT JOIN segments s ON s.version_id = v.id
                WHERE t.status = 'annotated'"""
        ).fetchone()
        _mark("totals_segments")

        activity_clauses = ["e.event_type = 'completed'"]
        activity_params: list = []
        if normalized["from"]:
            activity_clauses.append("e.created_at >= %s")
            activity_params.append(normalized["from"])
        if normalized["to"]:
            activity_clauses.append("e.created_at < %s")
            activity_params.append(normalized["to"])
        if normalized["annotator_id"]:
            activity_clauses.append("e.user_id = %s")
            activity_params.append(normalized["annotator_id"])
        activity = cur.execute(
            f"""SELECT count(*), count(DISTINCT e.user_id),
                       count(DISTINCT e.user_id) FILTER
                           (WHERE e.created_at >= now() - interval '24 hours'),
                       count(DISTINCT e.user_id) FILTER
                           (WHERE e.created_at >= now() - interval '7 days')
                FROM annotation_events e
                WHERE {' AND '.join(activity_clauses)}""",
            activity_params,
        ).fetchone()
        recent_activity_clauses = [
            "e.event_type = 'completed'",
            "e.created_at >= now() - interval '7 days'",
        ]
        recent_activity_params: list = []
        if normalized["annotator_id"]:
            recent_activity_clauses.append("e.user_id = %s")
            recent_activity_params.append(normalized["annotator_id"])
        recent_completed = cur.execute(
            f"""SELECT count(*) FROM annotation_events e
                WHERE {' AND '.join(recent_activity_clauses)}""",
            recent_activity_params,
        ).fetchone()[0]
        category_rows = cur.execute(
            """SELECT COALESCE(t.category, 'Uncategorized'), count(*),
                       COALESCE(sum(t.duration), 0)
                FROM _overview_matched t
                GROUP BY COALESCE(t.category, 'Uncategorized')
                ORDER BY count(*) DESC, COALESCE(t.category, 'Uncategorized')
                LIMIT 25"""
        ).fetchall()
        skip_rows = cur.execute(
            """SELECT reason, count(*)
                FROM _overview_matched t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                CROSS JOIN LATERAL unnest(v.skip_reasons) reason
                WHERE t.status = 'skipped'
                GROUP BY reason ORDER BY count(*) DESC, reason"""
        ).fetchall()
        queue = cur.execute(
            """SELECT
                   count(*) FILTER (WHERE t.status = 'pending' AND NOT t.has_draft),
                   count(*) FILTER (WHERE t.baseline_version_id IS NULL),
                   count(*) FILTER (WHERE t.has_assignment
                       AND t.assignment_last_activity_at < now() - interval '4 hours'),
                   count(*) FILTER (WHERE t.status = 'pending' AND t.eligible
                       AND t.has_draft AND NOT t.has_assignment)
                FROM _overview_matched t"""
        ).fetchone()
        _mark("activity_queue")
        from annotation_metadata.queries import (
            CONFIDENCE_CASE, NO_CURRENT_SOURCES, source_row_match_sql,
        )
        from annotation_metadata.taxonomy import FALLBACK_SOURCE_SCENE, scene_label
        metadata_filter = normalized["metadata"]
        match_sql, match_params = source_row_match_sql(
            metadata_filter, src_alias="src"
        )
        conf_rank = CONFIDENCE_CASE.format(expr="confidence")
        cur.execute(
            """CREATE TEMP TABLE _overview_review ON COMMIT DROP AS
               SELECT DISTINCT ON (sr.version_id) sr.version_id, sr.status
               FROM scene_reviews sr
               JOIN _overview_matched t
                 ON t.current_published_version_id = sr.version_id
               WHERE NOT sr.superseded
               ORDER BY sr.version_id, sr.review_no DESC"""
        )
        _mark("review_temp")
        virtual_sql = ""
        if metadata_filter.allows_virtual_unknown():
            virtual_sql = f"""
                UNION ALL
                SELECT t.id, t.duration, '{FALLBACK_SOURCE_SCENE}'::text, NULL::uuid,
                       'unknown'::text, NULL::uuid
                FROM _overview_matched t
                WHERE {NO_CURRENT_SOURCES.format(task="t")}
            """
        grouped_rows = cur.execute(
            f"""
            WITH src AS (
                SELECT t.id AS task_id, t.duration, src.scene_code, src.batch_id,
                       src.confidence, src.id AS source_id
                FROM annotation_tasks t
                LEFT JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                JOIN task_sources src ON src.task_id = t.id AND {match_sql}
                WHERE {where}
                {virtual_sql}
            ),
            best AS (
                SELECT DISTINCT ON (task_id) task_id, confidence
                FROM src
                ORDER BY task_id, {conf_rank}, COALESCE(scene_code, ''), source_id
            )
            SELECT 'conf' AS kind, key, task_count, duration_seconds
            FROM (
                SELECT COALESCE(b.confidence, 'unknown') AS key,
                       count(*) AS task_count,
                       COALESCE(sum(t.duration), 0) AS duration_seconds
                FROM _overview_matched t
                LEFT JOIN best b ON b.task_id = t.id
                GROUP BY 1
            ) confidence
            UNION ALL
            SELECT 'scene', key, task_count, duration_seconds
            FROM (
                SELECT COALESCE(scene_code, '{FALLBACK_SOURCE_SCENE}') AS key,
                       count(*) AS task_count,
                       COALESCE(sum(duration), 0) AS duration_seconds
                FROM (
                    SELECT task_id, duration,
                           COALESCE(scene_code, '{FALLBACK_SOURCE_SCENE}') AS scene_code
                    FROM src
                    GROUP BY task_id, duration,
                             COALESCE(scene_code, '{FALLBACK_SOURCE_SCENE}')
                ) scene_tasks
                GROUP BY 1
            ) scenes
            UNION ALL
            SELECT 'batch', key, task_count, duration_seconds
            FROM (
                SELECT sb.batch_code AS key,
                       count(*) AS task_count,
                       COALESCE(sum(duration), 0) AS duration_seconds
                FROM (
                    SELECT src.task_id, src.duration, src.batch_id
                    FROM src
                    WHERE src.batch_id IS NOT NULL
                    GROUP BY src.task_id, src.duration, src.batch_id
                ) batch_tasks
                JOIN source_batches sb ON sb.id = batch_tasks.batch_id
                GROUP BY sb.batch_code
            ) batches
            """,
            (*match_params, *params),
        ).fetchall()
        _mark("source_groups")
        conf_rows = [
            (row[1], row[2], row[3]) for row in grouped_rows if row[0] == "conf"
        ]
        scene_rows = [
            (row[1], row[2], row[3]) for row in grouped_rows if row[0] == "scene"
        ]
        batch_rows = [
            (row[1], row[2], row[3]) for row in grouped_rows if row[0] == "batch"
        ]
        conf_rows.sort(key=lambda item: item[0] or "")
        scene_rows.sort(key=lambda item: (-int(item[1]), item[0] or ""))
        batch_rows.sort(key=lambda item: (-int(item[1]), item[0] or ""))
        review_rows = cur.execute(
            """SELECT status, count(*) AS task_count,
                      COALESCE(sum(duration), 0) AS duration_seconds
               FROM (
                   SELECT t.id, t.duration,
                          CASE WHEN t.current_published_version_id IS NULL
                               THEN 'unreviewed_unpublished'
                               ELSE COALESCE(r.status, 'pending')
                          END AS status
                   FROM _overview_matched t
                   LEFT JOIN _overview_review r
                     ON r.version_id = t.current_published_version_id
               ) grouped
               GROUP BY status
               ORDER BY task_count DESC, status"""
        ).fetchall()
        _mark("reviews")
        from annotation_quality.queries import (
            cross_check_quality_summary, cross_check_submitted_workload,
        )
        cross_check = cross_check_quality_summary(cur)
        workload_count, workload_seconds = cross_check_submitted_workload(cur)
        _mark("cross_check")

    confidence_buckets = [
        {"confidence": item[0], "task_count": int(item[1]),
         "duration_seconds": float(item[2])}
        for item in conf_rows
    ]
    source_scene_groups = [
        {"scene_code": item[0],
         "label": scene_label(item[0]),
         "task_count": int(item[1]), "duration_seconds": float(item[2]),
         "overlapping": True}
        for item in scene_rows
    ]
    source_batch_groups = [
        {"batch_code": item[0], "task_count": int(item[1]),
         "duration_seconds": float(item[2]), "overlapping": True}
        for item in batch_rows
    ]
    review_status_groups = [
        {"status": item[0], "task_count": int(item[1]),
         "duration_seconds": float(item[2]), "overlapping": False}
        for item in review_rows
    ]
    review_by_status = {
        item["status"]: item for item in review_status_groups
    }
    review_stats = {
        "unreviewed_unpublished": int(
            review_by_status.get("unreviewed_unpublished", {}).get("task_count", 0)
        ),
        "unreviewed_published": int(
            review_by_status.get("pending", {}).get("task_count", 0)
        ),
        "pending": int(review_by_status.get("pending", {}).get("task_count", 0)),
        "confirmed": int(review_by_status.get("confirmed", {}).get("task_count", 0)),
        "mixed": int(review_by_status.get("mixed", {}).get("task_count", 0)),
        "out_of_scope": int(
            review_by_status.get("out_of_scope", {}).get("task_count", 0)
        ),
        "uncertain": int(review_by_status.get("uncertain", {}).get("task_count", 0)),
    }

    total_segments = int(segment_row[0] or 0)
    excluded_segments = int(segment_row[4] or 0)
    return {
        "totals": {
            "total_audio_count": int(row[0]),
            "total_audio_duration_seconds": float(row[1]),
            "annotated_count": int(row[2]),
            "annotated_duration_seconds": float(row[3]),
            "skipped_count": int(row[4]),
            "skipped_duration_seconds": float(row[5]),
            "pending_count": int(row[6]),
            "pending_duration_seconds": float(row[7]),
            "cross_check_submitted_count": workload_count,
            "cross_check_submitted_audio_seconds": workload_seconds,
        },
        "pending": {
            "assigned_count": int(row[8]),
            "available_count": int(row[9]),
            "ineligible_count": int(row[10]),
            "reserved_count": int(row[11]),
            "oldest_created_at": row[12].isoformat() if row[12] else None,
            "cross_check_in_progress_count": cross_check["in_progress_count"],
        },
        "cross_check": cross_check,
        "segments": {
            "total_count": total_segments,
            "total_duration_seconds": float(segment_row[1]),
            "trainable_count": int(segment_row[2] or 0),
            "trainable_duration_seconds": float(segment_row[3]),
        },
        "quality": {
            "excluded_count": excluded_segments,
            "excluded_duration_seconds": float(segment_row[5]),
            "excluded_ratio": (
                round(excluded_segments / total_segments, 4)
                if total_segments else 0.0
            ),
        },
        "activity": {
            "completed_count": int(activity[0]),
            "active_annotators": int(activity[1]),
            "active_annotators_24h": int(activity[2]),
            "active_annotators_7d": int(activity[3]),
            "completed_count_7d": int(recent_completed),
            "daily_throughput_7d": round(int(recent_completed) / 7, 2),
        },
        "categories": [
            {"category": item[0], "count": int(item[1]),
             "duration_seconds": float(item[2])}
            for item in category_rows
        ],
        "skip_reasons": [
            {"reason": item[0], "count": int(item[1])}
            for item in skip_rows
        ],
        "queue_health": {
            "pending_without_draft": int(queue[0]),
            "missing_baseline": int(queue[1]),
            "stale_assignments": int(queue[2]),
            "claimable_tasks": int(queue[3]),
        },
        "source_scenes": source_scene_groups,
        "source_batches": source_batch_groups,
        "confidence_buckets": confidence_buckets,
        "review_statuses": review_status_groups,
        "review_stats": review_stats,
        "applied_filters": _applied_admin_filters(normalized),
        "as_of": as_of.isoformat(),
        "updated_at": as_of.isoformat(),
        "definitions": _admin_statistics_definitions(),
        "notes": {
            "task_counts": "unique tasks; multi-source joins are not summed",
            "scene_groups_overlap": True,
            "batch_groups_overlap": True,
            "confidence_buckets": (
                "mutually exclusive highest matching source confidence"
            ),
        },
    }


def admin_timeseries(filters: dict | None = None) -> dict:
    normalized = _normalize_admin_filters(filters)
    clauses = ["e.event_type IN ('completed', 'revoked_admin')"]
    params: list = []
    if normalized["from"]:
        clauses.append("e.created_at >= %s")
        params.append(normalized["from"])
    if normalized["to"]:
        clauses.append("e.created_at < %s")
        params.append(normalized["to"])
    if normalized["annotator_id"]:
        clauses.append("COALESCE(e.user_id, v.submitted_by_user_id) = %s")
        params.append(normalized["annotator_id"])
    if normalized["status"] in ("annotated", "skipped"):
        clauses.append("COALESCE(e.to_status, v.target_status) = %s")
        params.append(normalized["status"])
    elif normalized["status"] == "pending":
        clauses.append("e.event_type = 'revoked_admin'")
    elif normalized["status"] == "assigned":
        clauses.append("false")
    if normalized["folder"]:
        clauses.append("t.folder = %s")
        params.append(normalized["folder"])
    if normalized["category"]:
        clauses.append("t.category = %s")
        params.append(normalized["category"])
    if normalized["q"]:
        clauses.append("(t.filename ILIKE %s OR t.rel_path ILIKE %s)")
        like = f"%{normalized['q']}%"
        params.extend([like, like])

    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""WITH event_values AS (
                    SELECT date_trunc(%s, timezone(%s, e.created_at)) AS period,
                           e.event_type, COALESCE(e.to_status, v.target_status) AS status,
                           t.duration,
                           CASE WHEN e.event_type = 'completed'
                                  AND COALESCE(e.to_status, v.target_status) = 'annotated'
                                THEN COALESCE((
                                  SELECT sum(s.duration) FROM segments s
                                  WHERE s.version_id = e.version_id
                                    AND NOT s.exclude_from_training
                                ), 0) ELSE 0 END AS training_duration
                    FROM annotation_events e
                    JOIN annotation_tasks t ON t.id = e.task_id
                    LEFT JOIN annotation_versions v ON v.id = e.version_id
                    WHERE {' AND '.join(clauses)}
                )
                SELECT period,
                       count(*) FILTER
                           (WHERE event_type = 'completed' AND status = 'annotated'),
                       count(*) FILTER
                           (WHERE event_type = 'completed' AND status = 'skipped'),
                       count(*) FILTER (WHERE event_type = 'revoked_admin'),
                       COALESCE(sum(duration) FILTER
                           (WHERE event_type = 'completed'), 0),
                       COALESCE(sum(training_duration), 0)
                FROM event_values
                GROUP BY period ORDER BY period""",
            (normalized["bucket"], normalized["timezone"], *params),
        ).fetchall()
    return {
        "bucket": normalized["bucket"],
        "timezone": normalized["timezone"],
        "from": normalized["from"].isoformat() if normalized["from"] else None,
        "to": normalized["to"].isoformat() if normalized["to"] else None,
        "items": [
            {
                "period": row[0].isoformat(),
                "annotated": int(row[1]),
                "skipped": int(row[2]),
                "revoked": int(row[3]),
                "audio_duration_seconds": float(row[4]),
                "training_duration_seconds": float(row[5]),
            }
            for row in rows
        ],
    }


def admin_quality(filters: dict | None = None, limit: int = 50) -> dict:
    """Return rule-based quality signals; these are review cues, not scores."""
    normalized = _normalize_admin_filters(filters)
    limit = max(1, min(int(limit), 100))
    event_clauses = ["e.event_type = 'completed'"]
    event_params: list = []
    if normalized["from"]:
        event_clauses.append("e.created_at >= %s")
        event_params.append(normalized["from"])
    if normalized["to"]:
        event_clauses.append("e.created_at < %s")
        event_params.append(normalized["to"])
    if normalized["annotator_id"]:
        event_clauses.append("e.user_id = %s")
        event_params.append(normalized["annotator_id"])
    if normalized["status"] in ("annotated", "skipped"):
        event_clauses.append("e.to_status = %s")
        event_params.append(normalized["status"])
    elif normalized["status"] in ("pending", "assigned"):
        event_clauses.append("false")
    if normalized["folder"]:
        event_clauses.append("t.folder = %s")
        event_params.append(normalized["folder"])
    if normalized["category"]:
        event_clauses.append("t.category = %s")
        event_params.append(normalized["category"])
    if normalized["q"]:
        event_clauses.append("(t.filename ILIKE %s OR t.rel_path ILIKE %s)")
        like = f"%{normalized['q']}%"
        event_params.extend([like, like])

    task_where, task_params = _admin_task_filter_sql(normalized)
    revoked_clauses = ["rv.lifecycle = 'revoked'"]
    revoked_params: list = []
    if normalized["from"]:
        revoked_clauses.append("rv.revoked_at >= %s")
        revoked_params.append(normalized["from"])
    if normalized["to"]:
        revoked_clauses.append("rv.revoked_at < %s")
        revoked_params.append(normalized["to"])
    if normalized["annotator_id"]:
        revoked_clauses.append("rv.submitted_by_user_id = %s")
        revoked_params.append(normalized["annotator_id"])

    with db_tx() as conn, conn.cursor() as cur:
        fast_rows = cur.execute(
            f"""WITH turnarounds AS (
                    SELECT e.id, e.task_id, e.user_id, e.created_at,
                           e.to_status, t.filename, t.rel_path, t.duration,
                           u.username,
                           extract(epoch FROM (e.created_at - started.at))
                               AS elapsed_seconds,
                           greatest(30.0, t.duration * 0.25) AS threshold_seconds
                    FROM annotation_events e
                    JOIN annotation_tasks t ON t.id = e.task_id
                    LEFT JOIN annotators u ON u.id = e.user_id
                    JOIN LATERAL (
                        SELECT max(begin_event.created_at) AS at
                        FROM annotation_events begin_event
                        WHERE begin_event.task_id = e.task_id
                          AND begin_event.user_id = e.user_id
                          AND begin_event.event_type IN ('claimed', 'reopened')
                          AND begin_event.created_at <= e.created_at
                    ) started ON started.at IS NOT NULL
                    WHERE {' AND '.join(event_clauses)}
                )
                SELECT * FROM turnarounds
                WHERE elapsed_seconds < threshold_seconds
                ORDER BY created_at DESC, id DESC LIMIT %s""",
            (*event_params, limit),
        ).fetchall()
        fast_count = cur.execute(
            f"""SELECT count(*)
                FROM annotation_events e
                JOIN annotation_tasks t ON t.id = e.task_id
                JOIN LATERAL (
                    SELECT max(begin_event.created_at) AS at
                    FROM annotation_events begin_event
                    WHERE begin_event.task_id = e.task_id
                      AND begin_event.user_id = e.user_id
                      AND begin_event.event_type IN ('claimed', 'reopened')
                      AND begin_event.created_at <= e.created_at
                ) started ON started.at IS NOT NULL
                WHERE {' AND '.join(event_clauses)}
                  AND extract(epoch FROM (e.created_at - started.at))
                      < greatest(30.0, t.duration * 0.25)""",
            event_params,
        ).fetchone()[0]
        stale_rows = cur.execute(
            f"""SELECT a.task_id, a.user_id, u.username, a.mode,
                       a.assigned_at, a.last_activity_at, t.filename,
                       t.rel_path, t.duration
                FROM assignments a
                JOIN annotation_tasks t ON t.id = a.task_id
                LEFT JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                JOIN annotators u ON u.id = a.user_id
                WHERE a.last_activity_at < now() - interval '4 hours'
                  AND {task_where}
                ORDER BY a.last_activity_at, a.task_id LIMIT %s""",
            (*task_params, limit),
        ).fetchall()
        stale_count = cur.execute(
            f"""SELECT count(*) FROM assignments a
                JOIN annotation_tasks t ON t.id = a.task_id
                LEFT JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                WHERE a.last_activity_at < now() - interval '4 hours'
                  AND {task_where}""",
            task_params,
        ).fetchone()[0]
        excluded = cur.execute(
            f"""SELECT count(s.segment_id) FILTER
                           (WHERE s.exclude_from_training),
                       count(s.segment_id)
                FROM annotation_tasks t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                LEFT JOIN segments s ON s.version_id = v.id
                WHERE t.status = 'annotated' AND {task_where}""",
            task_params,
        ).fetchone()
        revoked_count = cur.execute(
            f"""SELECT count(*) FROM annotation_versions rv
                WHERE {' AND '.join(revoked_clauses)}""",
            revoked_params,
        ).fetchone()[0]
        from annotation_quality.queries import cross_check_quality_summary
        cross_check = cross_check_quality_summary(cur)

    items = [
        {
            "type": "unusually_fast", "severity": "warning",
            "event_id": row[0], "task_id": str(row[1]),
            "annotator_id": str(row[2]) if row[2] else None,
            "created_at": row[3].isoformat(), "status": row[4],
            "filename": row[5], "rel_path": row[6],
            "audio_duration_seconds": float(row[7]), "username": row[8],
            "elapsed_seconds": float(row[9]),
            "threshold_seconds": float(row[10]),
        }
        for row in fast_rows
    ]
    items.extend({
        "type": "stale_assignment", "severity": "warning",
        "task_id": str(row[0]), "annotator_id": str(row[1]),
        "username": row[2], "mode": row[3],
        "assigned_at": row[4].isoformat(),
        "last_activity_at": row[5].isoformat(), "filename": row[6],
        "rel_path": row[7], "audio_duration_seconds": float(row[8]),
    } for row in stale_rows)
    if normalized["signal"] != "all":
        items = [
            item for item in items
            if item["type"] == normalized["signal"]
        ]
    items = sorted(
        items,
        key=lambda item: item.get("created_at")
        or item.get("last_activity_at") or "",
        reverse=True,
    )[:limit]
    excluded_count = int(excluded[0] or 0)
    segment_count = int(excluded[1] or 0)
    return {
        "stats": {
            "unusually_fast": int(fast_count),
            "stale_assignments": int(stale_count),
            "excluded_segment_count": excluded_count,
            "excluded_segment_rate": (
                round(excluded_count / segment_count, 4)
                if segment_count else 0.0
            ),
            "revoked_count": int(revoked_count),
        },
        "items": items,
        "methodology": {
            "unusually_fast": "wall clock < max(30 seconds, 25% of audio duration)",
            "stale_assignment": "last activity more than 4 hours ago",
            "warning": "Signals require human review and are not quality scores",
        },
        "cross_check": cross_check,
        "updated_at": utcnow().isoformat(),
    }


def _encode_admin_cursor(*values) -> str:
    serializable = [
        value.isoformat() if isinstance(value, datetime) else str(value)
        for value in values
    ]
    return base64.urlsafe_b64encode(
        json.dumps(serializable, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_admin_cursor(cursor: str, count: int) -> list[str]:
    try:
        raw = str(cursor)
        if not raw or len(raw) > 4096:
            raise ValueError
        raw += "=" * (-len(raw) % 4)
        decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
        values = json.loads(decoded.decode("utf-8"))
        if not isinstance(values, list) or len(values) != count:
            raise ValueError
        return [str(value) for value in values]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationError("Invalid cursor") from exc


def admin_annotators(filters: dict | None = None, limit: int = 50,
                     cursor: str | None = None,
                     presence_lease_seconds: int = DEFAULT_PRESENCE_LEASE_SECONDS
                     ) -> dict:
    normalized = _normalize_admin_filters(filters)
    limit = max(1, min(int(limit), 100))
    user_where: list[str] = ["true"]
    user_params: list = []
    if normalized["annotator_status"] != "all":
        user_where.append("u.status = %s")
        user_params.append(normalized["annotator_status"])
    if normalized["q"]:
        user_where.append("u.username ILIKE %s")
        user_params.append(f"%{normalized['q']}%")
    if cursor:
        cursor_name, cursor_id = _decode_admin_cursor(cursor, 2)
        cursor_id = _validate_uuid(cursor_id, "cursor")
        user_where.append("(lower(u.username), u.id) > (%s, %s::uuid)")
        user_params.extend([cursor_name, cursor_id])

    stats_filters = dict(normalized)
    stats_filters["annotator_id"] = None
    stats_filters["q"] = ""
    current_where, current_params = _admin_task_filter_sql(stats_filters)

    event_where = ["e.event_type = 'completed'"]
    event_params: list = []
    revoked_where = ["rv.lifecycle = 'revoked'"]
    revoked_params: list = []
    if normalized["from"]:
        event_where.append("e.created_at >= %s")
        event_params.append(normalized["from"])
        revoked_where.append("rv.revoked_at >= %s")
        revoked_params.append(normalized["from"])
    if normalized["to"]:
        event_where.append("e.created_at < %s")
        event_params.append(normalized["to"])
        revoked_where.append("rv.revoked_at < %s")
        revoked_params.append(normalized["to"])

    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""WITH current_stats AS (
                    SELECT COALESCE(
                               v.credited_annotator_id, v.submitted_by_user_id
                           ) AS user_id,
                           count(*) FILTER (WHERE t.status = 'annotated') AS annotated,
                           count(*) FILTER (WHERE t.status = 'skipped') AS skipped,
                           COALESCE(sum(t.duration), 0) AS duration
                    FROM annotation_tasks t
                    JOIN annotation_versions v
                      ON v.id = t.current_published_version_id
                    WHERE {current_where}
                      AND COALESCE(
                          v.credited_annotator_id, v.submitted_by_user_id
                      ) IS NOT NULL
                    GROUP BY COALESCE(
                        v.credited_annotator_id, v.submitted_by_user_id
                    )
                ), history_stats AS (
                    SELECT e.user_id, count(*) AS completed,
                           max(e.created_at) AS last_completed_at
                    FROM annotation_events e
                    WHERE {' AND '.join(event_where)}
                    GROUP BY e.user_id
                ), revoked_stats AS (
                    SELECT rv.submitted_by_user_id AS user_id,
                           count(*) AS revoked
                    FROM annotation_versions rv
                    WHERE {' AND '.join(revoked_where)}
                    GROUP BY rv.submitted_by_user_id
                )
                SELECT u.id, u.username, u.status, u.created_at,
                       u.deactivated_at, u.deactivated_reason,
                       COALESCE(cs.annotated, 0), COALESCE(cs.skipped, 0),
                       COALESCE(cs.duration, 0), COALESCE(hs.completed, 0),
                       COALESCE(rs.revoked, 0), hs.last_completed_at,
                       EXISTS (SELECT 1 FROM active_sessions ses
                               WHERE ses.user_id = u.id
                                 AND ses.expires_at > now()
                                 AND ses.absolute_expires_at > now()
                                 AND ses.last_seen_at > now() - %s),
                       a.task_id, a.mode, a.assigned_at, a.last_activity_at
                FROM annotators u
                LEFT JOIN current_stats cs ON cs.user_id = u.id
                LEFT JOIN history_stats hs ON hs.user_id = u.id
                LEFT JOIN revoked_stats rs ON rs.user_id = u.id
                LEFT JOIN assignments a ON a.user_id = u.id
                WHERE {' AND '.join(user_where)}
                ORDER BY lower(u.username), u.id
                LIMIT %s""",
            (*current_params, *event_params, *revoked_params,
             _interval_seconds(presence_lease_seconds),
             *user_params, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": str(row[0]),
            "username": row[1],
            "status": row[2],
            "created_at": row[3].isoformat(),
            "deactivated_at": row[4].isoformat() if row[4] else None,
            "deactivated_reason": row[5],
            "current": {
                "annotated_count": int(row[6]),
                "skipped_count": int(row[7]),
                "duration_seconds": float(row[8]),
            },
            "history": {
                "completed_count": int(row[9]),
                "revoked_count": int(row[10]),
                "last_completed_at": row[11].isoformat() if row[11] else None,
            },
            "online": bool(row[12]),
            "assignment": ({
                "task_id": str(row[13]),
                "mode": row[14],
                "assigned_at": row[15].isoformat(),
                "last_activity_at": row[16].isoformat(),
            } if row[13] else None),
        }
        for row in rows
    ]
    next_cursor = None
    if has_more and rows:
        next_cursor = _encode_admin_cursor(rows[-1][1].lower(), rows[-1][0])
    return {"items": items, "next_cursor": next_cursor}


def _annotator_current_where(filters: dict) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if filters["status"] in ("annotated", "skipped"):
        clauses.append("t.status = %s")
        params.append(filters["status"])
    elif filters["status"] in ("pending", "assigned"):
        clauses.append("false")
    if filters["folder"]:
        clauses.append("t.folder = %s")
        params.append(filters["folder"])
    if filters["category"]:
        clauses.append("t.category = %s")
        params.append(filters["category"])
    if filters["from"]:
        clauses.append("v.submitted_at >= %s")
        params.append(filters["from"])
    if filters["to"]:
        clauses.append("v.submitted_at < %s")
        params.append(filters["to"])
    return (" AND ".join(clauses) if clauses else "true"), params


def admin_annotator_detail(annotator_id: str,
                           filters: dict | None = None,
                           presence_lease_seconds: int = DEFAULT_PRESENCE_LEASE_SECONDS
                           ) -> dict:
    uid = _validate_uuid(annotator_id, "annotator_id")
    normalized = _normalize_admin_filters(filters)
    current_where, current_params = _annotator_current_where(normalized)
    history_where = ["e.event_type = 'completed'", "e.user_id = %s"]
    history_params: list = [uid]
    if normalized["from"]:
        history_where.append("e.created_at >= %s")
        history_params.append(normalized["from"])
    if normalized["to"]:
        history_where.append("e.created_at < %s")
        history_params.append(normalized["to"])

    with db_tx() as conn, conn.cursor() as cur:
        user = cur.execute(
            """SELECT id, username, status, created_at, deactivated_at,
                      deactivated_reason
               FROM annotators WHERE id = %s""",
            (uid,),
        ).fetchone()
        if not user:
            raise NotFoundError("Annotator not found")
        current = cur.execute(
            f"""SELECT count(*) FILTER (WHERE t.status = 'annotated'),
                       count(*) FILTER (WHERE t.status = 'skipped'),
                       COALESCE(sum(t.duration), 0),
                       COALESCE(sum(t.duration) FILTER
                           (WHERE t.status = 'annotated'), 0)
                FROM annotation_tasks t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                 AND COALESCE(v.credited_annotator_id, v.submitted_by_user_id) = %s
                WHERE {current_where}""",
            (uid, *current_params),
        ).fetchone()
        quality = cur.execute(
            f"""SELECT count(s.segment_id),
                       count(s.segment_id) FILTER
                           (WHERE s.exclude_from_training),
                       COALESCE(sum(s.duration) FILTER
                           (WHERE NOT s.exclude_from_training), 0)
                FROM annotation_tasks t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                 AND COALESCE(v.credited_annotator_id, v.submitted_by_user_id) = %s
                LEFT JOIN segments s ON s.version_id = v.id
                WHERE {current_where}""",
            (uid, *current_params),
        ).fetchone()
        history = cur.execute(
            f"""SELECT count(*),
                       count(*) FILTER (WHERE e.to_status = 'annotated'),
                       count(*) FILTER (WHERE e.to_status = 'skipped'),
                       count(DISTINCT (e.created_at AT TIME ZONE %s)::date),
                       max(e.created_at)
                FROM annotation_events e
                WHERE {' AND '.join(history_where)}""",
            (normalized["timezone"], *history_params),
        ).fetchone()
        turnaround = cur.execute(
            f"""WITH durations AS (
                    SELECT extract(epoch FROM (e.created_at - started.at)) AS seconds
                    FROM annotation_events e
                    JOIN LATERAL (
                        SELECT max(begin_event.created_at) AS at
                        FROM annotation_events begin_event
                        WHERE begin_event.task_id = e.task_id
                          AND begin_event.user_id = e.user_id
                          AND begin_event.event_type IN ('claimed', 'reopened')
                          AND begin_event.created_at <= e.created_at
                    ) started ON started.at IS NOT NULL
                    WHERE {' AND '.join(history_where)}
                )
                SELECT avg(seconds),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds),
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY seconds)
                FROM durations""",
            history_params,
        ).fetchone()
        revoked_where = [
            "submitted_by_user_id = %s", "lifecycle = 'revoked'"
        ]
        revoked_params: list = [uid]
        action_where = ["user_id = %s"]
        action_params: list = [uid]
        if normalized["from"]:
            revoked_where.append("revoked_at >= %s")
            revoked_params.append(normalized["from"])
            action_where.append("created_at >= %s")
            action_params.append(normalized["from"])
        if normalized["to"]:
            revoked_where.append("revoked_at < %s")
            revoked_params.append(normalized["to"])
            action_where.append("created_at < %s")
            action_params.append(normalized["to"])
        revoked_count = cur.execute(
            f"""SELECT count(*) FROM annotation_versions
                WHERE {' AND '.join(revoked_where)}""",
            revoked_params,
        ).fetchone()[0]
        actions = cur.execute(
            f"""SELECT count(*) FILTER (WHERE event_type = 'abandoned'),
                       count(*) FILTER (WHERE event_type = 'reopened')
                FROM annotation_events WHERE {' AND '.join(action_where)}""",
            action_params,
        ).fetchone()
        skip_rows = cur.execute(
            f"""SELECT reason, count(*)
                FROM annotation_tasks t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                 AND COALESCE(v.credited_annotator_id, v.submitted_by_user_id) = %s
                CROSS JOIN LATERAL unnest(v.skip_reasons) reason
                WHERE {current_where}
                GROUP BY reason ORDER BY count(*) DESC, reason""",
            (uid, *current_params),
        ).fetchall()
        category_rows = cur.execute(
            f"""SELECT COALESCE(t.category, 'Uncategorized'), count(*)
                FROM annotation_tasks t
                JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                 AND COALESCE(v.credited_annotator_id, v.submitted_by_user_id) = %s
                WHERE {current_where}
                GROUP BY COALESCE(t.category, 'Uncategorized')
                ORDER BY count(*) DESC, COALESCE(t.category, 'Uncategorized')""",
            (uid, *current_params),
        ).fetchall()
        assignment = cur.execute(
            """SELECT a.task_id, a.mode, a.assigned_at, a.last_activity_at,
                      t.filename, t.rel_path
               FROM assignments a JOIN annotation_tasks t ON t.id = a.task_id
               WHERE a.user_id = %s""",
            (uid,),
        ).fetchone()
        session = cur.execute(
            """SELECT last_seen_at, last_activity_at,
                      expires_at > now()
                      AND absolute_expires_at > now()
                      AND last_seen_at > now() - %s AS online
               FROM active_sessions WHERE user_id = %s""",
            (_interval_seconds(presence_lease_seconds), uid),
        ).fetchone()
        from annotation_metadata.queries import load_scope
        from annotation_metadata.repository import scope_payload
        scene_scope_payload = scope_payload(load_scope(cur, uid))

    total_segments = int(quality[0] or 0)
    bad_segments = int(quality[1] or 0)
    return {
        "annotator": {
            "id": str(user[0]), "username": user[1], "status": user[2],
            "created_at": user[3].isoformat(),
            "deactivated_at": user[4].isoformat() if user[4] else None,
            "deactivated_reason": user[5],
        },
        "id": str(user[0]),
        "username": user[1],
        "status": user[2],
        "created_at": user[3].isoformat(),
        "deactivated_at": user[4].isoformat() if user[4] else None,
        "deactivated_reason": user[5],
        "current": {
            "annotated_count": int(current[0]),
            "skipped_count": int(current[1]),
            "duration_seconds": float(current[2]),
            "annotated_duration_seconds": float(current[3]),
            "trainable_duration_seconds": float(quality[2]),
        },
        "history": {
            "completed_count": int(history[0]),
            "annotated_count": int(history[1]),
            "skipped_count": int(history[2]),
            "active_days": int(history[3]),
            "last_completed_at": history[4].isoformat() if history[4] else None,
            "revoked_count": int(revoked_count),
            "abandoned_count": int(actions[0]),
            "revision_count": int(actions[1]),
        },
        "quality": {
            "segment_count": total_segments,
            "excluded_count": bad_segments,
            "excluded_ratio": (
                round(bad_segments / total_segments, 4) if total_segments else 0.0
            ),
        },
        "efficiency": {
            "turnaround_average_seconds": (
                float(turnaround[0]) if turnaround[0] is not None else None
            ),
            "turnaround_median_seconds": (
                float(turnaround[1]) if turnaround[1] is not None else None
            ),
            "turnaround_p90_seconds": (
                float(turnaround[2]) if turnaround[2] is not None else None
            ),
            "measurement": "wall_clock",
        },
        "activity": {
            "last_seen_at": session[0].isoformat() if session and session[0] else None,
            "last_activity_at": (
                session[1].isoformat() if session and session[1] else None
            ),
            "online": bool(session[2]) if session else False,
        },
        "assignment": ({
            "task_id": str(assignment[0]), "mode": assignment[1],
            "assigned_at": assignment[2].isoformat(),
            "last_activity_at": assignment[3].isoformat(),
            "filename": assignment[4], "rel_path": assignment[5],
        } if assignment else None),
        "skip_reasons": [
            {"reason": row[0], "count": int(row[1])} for row in skip_rows
        ],
        "categories": [
            {"category": row[0], "count": int(row[1])}
            for row in category_rows
        ],
        "scene_scope": scene_scope_payload,
    }


def admin_tasks(filters: dict | None = None, limit: int = 50,
                cursor: str | None = None) -> dict:
    """Task-centric corpus list, including pending and assigned work."""
    normalized = _normalize_admin_filters(filters)
    limit = max(1, min(int(limit), 100))
    where, filter_params = _admin_task_filter_sql(normalized)
    clauses = [where]
    params = list(filter_params)
    filter_digest = _admin_list_filter_digest(normalized)
    if cursor:
        created_at, task_id, cursor_digest = _decode_admin_cursor(cursor, 3)
        if cursor_digest != filter_digest:
            raise ValidationError("Invalid cursor")
        created_at = _parse_admin_datetime(created_at, "cursor")
        task_id = _validate_uuid(task_id, "cursor")
        clauses.append("(t.created_at, t.id) < (%s::timestamptz, %s::uuid)")
        params.extend([created_at, task_id])
    with db_tx() as conn, conn.cursor() as cur:
        matched = cur.execute(
            f"""SELECT count(*), COALESCE(sum(t.duration), 0)
                FROM annotation_tasks t
                LEFT JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                WHERE {where}""",
            filter_params,
        ).fetchone()
        matched_count = int(matched[0])
        matched_duration_seconds = float(matched[1])
        rows = cur.execute(
            f"""SELECT t.id, t.created_at, t.updated_at, t.filename,
                       t.folder, t.rel_path, t.duration, t.status, t.eligible,
                       t.category, t.current_published_version_id,
                       t.baseline_version_id, t.baseline_quality,
                       v.target_status, v.submitted_at, v.submitted_by_user_id,
                       submitter.username, submitter.status,
                       a.user_id, assignee.username, a.mode, a.assigned_at,
                       a.last_activity_at, a.working_version_id,
                       d.id AS draft_id, d.revision, d.human_modified,
                       COALESCE(stats.segment_count, 0),
                       COALESCE(stats.excluded_count, 0),
                       COALESCE(stats.trainable_duration, 0),
                       (SELECT count(*) FROM task_annotator_blocks b
                        WHERE b.task_id = t.id)
                FROM annotation_tasks t
                LEFT JOIN annotation_versions v
                  ON v.id = t.current_published_version_id
                LEFT JOIN annotators submitter
                  ON submitter.id = COALESCE(
                      v.credited_annotator_id, v.submitted_by_user_id
                  )
                LEFT JOIN assignments a ON a.task_id = t.id
                LEFT JOIN annotators assignee ON assignee.id = a.user_id
                LEFT JOIN annotation_versions d
                  ON d.task_id = t.id AND d.lifecycle = 'draft'
                LEFT JOIN LATERAL (
                    SELECT count(*) AS segment_count,
                           count(*) FILTER (WHERE s.exclude_from_training)
                               AS excluded_count,
                           COALESCE(sum(s.duration) FILTER
                               (WHERE NOT s.exclude_from_training), 0)
                               AS trainable_duration
                    FROM segments s
                    WHERE s.version_id = COALESCE(
                        t.current_published_version_id, d.id
                    )
                ) stats ON true
                WHERE {' AND '.join(clauses)}
                ORDER BY t.created_at DESC, t.id DESC LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            if row[7] != "pending":
                pool_state_value = "completed"
            elif row[18] is not None:
                pool_state_value = "assigned"
            elif not row[8]:
                pool_state_value = "ineligible"
            elif row[24] is not None:
                pool_state_value = "available"
            else:
                pool_state_value = "unavailable"
            items.append({
                "task_id": str(row[0]), "id": str(row[0]),
                "created_at": row[1].isoformat(), "updated_at": row[2].isoformat(),
                "filename": row[3], "folder": row[4], "rel_path": row[5],
                "duration": float(row[6]),
                "task_status": row[7],
                "status": (
                    "assigned" if row[7] == "pending" and row[18] is not None
                    else row[7]
                ),
                "eligible": bool(row[8]), "category": row[9],
                "pool_state": pool_state_value,
                "current_version_id": str(row[10]) if row[10] else None,
                "baseline_version_id": str(row[11]) if row[11] else None,
                "baseline_quality": row[12], "target_status": row[13],
                "submitted_at": row[14].isoformat() if row[14] else None,
                "current_submitter": ({
                    "id": str(row[15]), "username": row[16], "status": row[17]
                } if row[15] else None),
                "submitted_by": row[16],
                "assignment": ({
                    "annotator_id": str(row[18]), "username": row[19],
                    "mode": row[20], "assigned_at": row[21].isoformat(),
                    "last_activity_at": row[22].isoformat(),
                    "working_version_id": str(row[23]),
                } if row[18] else None),
                "draft": ({
                    "version_id": str(row[24]), "revision": int(row[25]),
                    "human_modified": bool(row[26]),
                } if row[24] else None),
                "segment_count": int(row[27]),
                "excluded_segment_count": int(row[28]),
                "trainable_duration_seconds": float(row[29]),
                "blocked_annotator_count": int(row[30]),
            })
        if items:
            from annotation_metadata.repository import metadata_summaries
            summaries = metadata_summaries(
                cur, [item["task_id"] for item in items],
                filters=normalized["metadata"],
            )
            for item in items:
                summary = summaries.get(item["task_id"], {})
                item["source_scenes"] = summary.get("source_scenes") or []
                item["source_confidence"] = summary.get("source_confidence") or "unknown"
                item["batch_codes"] = summary.get("batch_codes") or []
                item["review_status"] = summary.get("review_status") or "pending"
                item["prediction_label"] = summary.get("prediction_label")
                item["prediction_scene"] = summary.get("prediction_scene")
                item["human_scenes"] = summary.get("human_scenes") or []
    next_cursor = None
    if has_more and rows:
        next_cursor = _encode_admin_cursor(rows[-1][1], rows[-1][0], filter_digest)
    return {
        "items": items, "next_cursor": next_cursor,
        "matched_count": matched_count,
        "matched_duration_seconds": matched_duration_seconds,
        "applied_filters": _applied_admin_filters(normalized),
        "filter_digest": filter_digest,
    }


def admin_annotations(filters: dict | None = None, limit: int = 50,
                      cursor: str | None = None) -> dict:
    normalized = _normalize_admin_filters(filters)
    limit = max(1, min(int(limit), 100))
    clauses = ["v.lifecycle IN ('published', 'revoked', 'superseded')"]
    params: list = []
    if normalized["annotator_id"]:
        clauses.append(
            "COALESCE(v.credited_annotator_id, v.submitted_by_user_id) = %s"
        )
        params.append(normalized["annotator_id"])
    if normalized["status"] in ("annotated", "skipped"):
        clauses.append("v.target_status = %s")
        params.append(normalized["status"])
    elif normalized["status"] in ("pending", "assigned"):
        clauses.append("false")
    if normalized["lifecycle"] != "all":
        clauses.append("v.lifecycle = %s")
        params.append(normalized["lifecycle"])
    if normalized["folder"]:
        clauses.append("t.folder = %s")
        params.append(normalized["folder"])
    if normalized["category"]:
        clauses.append("t.category = %s")
        params.append(normalized["category"])
    if normalized["q"]:
        clauses.append("(t.filename ILIKE %s OR t.rel_path ILIKE %s)")
        like = f"%{normalized['q']}%"
        params.extend([like, like])
    if normalized["from"]:
        clauses.append("v.submitted_at >= %s")
        params.append(normalized["from"])
    if normalized["to"]:
        clauses.append("v.submitted_at < %s")
        params.append(normalized["to"])
    from annotation_metadata.queries import metadata_filter_sql
    meta_sql, meta_params = metadata_filter_sql(normalized["metadata"], task_alias="t")
    if meta_sql != "true":
        clauses.append(meta_sql)
        params.extend(meta_params)
    filter_digest = _admin_list_filter_digest(normalized)
    if cursor:
        submitted_at, version_id, cursor_digest = _decode_admin_cursor(cursor, 3)
        if cursor_digest != filter_digest:
            raise ValidationError("Invalid cursor")
        submitted_at = _parse_admin_datetime(submitted_at, "cursor")
        version_id = _validate_uuid(version_id, "cursor")
        clauses.append("(v.submitted_at, v.id) < (%s::timestamptz, %s::uuid)")
        params.extend([submitted_at, version_id])

    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""SELECT v.id, v.submitted_at, v.lifecycle, v.target_status,
                       v.skip_reasons, v.revision,
                       COALESCE(v.credited_annotator_id, v.submitted_by_user_id),
                       u.username, u.status, t.id, t.filename, t.folder,
                       t.rel_path, t.duration, t.category,
                       (t.current_published_version_id = v.id) AS is_current,
                       count(s.segment_id),
                       count(s.segment_id) FILTER
                           (WHERE s.exclude_from_training),
                       COALESCE(sum(s.duration) FILTER
                           (WHERE NOT s.exclude_from_training), 0),
                       v.revoked_at, v.revoked_reason,
                       v.revoked_by_admin_action_id,
                       extract(epoch FROM (v.submitted_at - (
                           SELECT max(begin_event.created_at)
                           FROM annotation_events begin_event
                           WHERE begin_event.task_id = v.task_id
                             AND begin_event.user_id = COALESCE(
                                 v.submitted_by_user_id, v.credited_annotator_id
                             )
                             AND begin_event.event_type IN (
                                 'claimed', 'reopened', 'cross_check_claimed'
                             )
                             AND begin_event.created_at <= v.submitted_at
                       ))) AS turnaround_seconds
                FROM annotation_versions v
                JOIN annotation_tasks t ON t.id = v.task_id
                LEFT JOIN annotators u
                  ON u.id = COALESCE(
                      v.credited_annotator_id, v.submitted_by_user_id
                  )
                LEFT JOIN segments s ON s.version_id = v.id
                WHERE {' AND '.join(clauses)}
                GROUP BY v.id, t.id, u.id
                ORDER BY v.submitted_at DESC, v.id DESC
                LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "task_id": str(row[9]),
                "version_id": str(row[0]),
                "submitted_at": row[1].isoformat() if row[1] else None,
                "lifecycle": row[2],
                "status": row[3],
                "skip_reasons": list(row[4] or []),
                "revision": int(row[5]),
                "annotator": ({
                    "id": str(row[6]), "username": row[7], "status": row[8]
                } if row[6] else None),
                "submitted_by": row[7],
                "filename": row[10], "folder": row[11], "rel_path": row[12],
                "duration": float(row[13]), "category": row[14],
                "is_current": bool(row[15]), "can_revoke": bool(row[15]),
                "segment_count": int(row[16]),
                "excluded_segment_count": int(row[17]),
                "trainable_duration_seconds": float(row[18]),
                "revoked_at": row[19].isoformat() if row[19] else None,
                "revoked_reason": row[20],
                "revoked_by_admin_action_id": str(row[21]) if row[21] else None,
                "turnaround_seconds": (
                    float(row[22]) if row[22] is not None else None
                ),
            }
            for row in rows
        ]
        if items:
            from annotation_metadata.repository import metadata_summaries
            summaries = metadata_summaries(
                cur, [item["task_id"] for item in items],
                filters=normalized["metadata"],
            )
            for item in items:
                summary = summaries.get(item["task_id"], {})
                item["source_scenes"] = summary.get("source_scenes") or []
                item["source_confidence"] = summary.get("source_confidence") or "unknown"
                item["batch_codes"] = summary.get("batch_codes") or []
                item["review_status"] = summary.get("review_status") or "pending"
                item["prediction_label"] = summary.get("prediction_label")
                item["prediction_scene"] = summary.get("prediction_scene")
                item["human_scenes"] = summary.get("human_scenes") or []
    next_cursor = None
    if has_more and rows:
        next_cursor = _encode_admin_cursor(rows[-1][1], rows[-1][0], filter_digest)
    return {
        "items": items, "next_cursor": next_cursor,
        "applied_filters": _applied_admin_filters(normalized),
        "filter_digest": filter_digest,
    }


def admin_annotator_annotations(annotator_id: str,
                                filters: dict | None = None,
                                limit: int = 50,
                                cursor: str | None = None) -> dict:
    uid = _validate_uuid(annotator_id, "annotator_id")
    merged = dict(filters or {})
    merged["annotator_id"] = str(uid)
    return admin_annotations(merged, limit=limit, cursor=cursor)


def admin_annotation_detail(task_id: str) -> dict:
    tid = _validate_uuid(task_id, "task_id")
    with db_tx() as conn, conn.cursor() as cur:
        task = cur.execute(
            """SELECT t.id, t.rel_path, t.filename, t.folder, t.duration,
                      t.status, t.eligible, t.category, t.preprocessed_at,
                      t.created_at, t.updated_at, t.current_published_version_id,
                      t.baseline_version_id, t.baseline_quality,
                      t.reserved_for_user_id
               FROM annotation_tasks t WHERE t.id = %s""",
            (tid,),
        ).fetchone()
        if not task:
            raise NotFoundError("Task not found")
        versions = cur.execute(
            """SELECT v.id, v.version_no, v.lifecycle, v.target_status,
                      v.revision, v.human_modified, v.created_at, v.updated_at,
                      v.submitted_at, v.revoked_at, v.revoked_reason,
                      v.submitted_by_user_id, u.username, v.base_version_id,
                      v.revoked_by_admin_action_id, v.skip_reasons
               FROM annotation_versions v
               LEFT JOIN annotators u ON u.id = v.submitted_by_user_id
               WHERE v.task_id = %s
               ORDER BY v.version_no DESC, v.id DESC""",
            (tid,),
        ).fetchall()
        display_version_id = task[11]
        if display_version_id is None:
            for version in versions:
                if version[2] in ("revoked", "superseded", "published"):
                    display_version_id = version[0]
                    break
        if display_version_id is None:
            for version in versions:
                if version[2] == "draft":
                    display_version_id = version[0]
                    break
        segments = (
            _load_segments(cur, display_version_id) if display_version_id else []
        )
        assignment = cur.execute(
            """SELECT a.user_id, u.username, a.mode, a.working_version_id,
                      a.assigned_at, a.last_activity_at
               FROM assignments a JOIN annotators u ON u.id = a.user_id
               WHERE a.task_id = %s""",
            (tid,),
        ).fetchone()
        version_items = [
            {
                "id": str(row[0]), "version_no": int(row[1]),
                "lifecycle": row[2], "target_status": row[3],
                "revision": int(row[4]), "human_modified": bool(row[5]),
                "created_at": row[6].isoformat(), "updated_at": row[7].isoformat(),
                "submitted_at": row[8].isoformat() if row[8] else None,
                "revoked_at": row[9].isoformat() if row[9] else None,
                "revoked_reason": row[10],
                "submitter": ({"id": str(row[11]), "username": row[12]}
                              if row[11] else None),
                "base_version_id": str(row[13]) if row[13] else None,
                "revoked_by_admin_action_id": str(row[14]) if row[14] else None,
                "skip_reasons": list(row[15] or []),
                "is_current": row[0] == task[11],
            }
            for row in versions
        ]
        payload = {
            "task_id": str(task[0]), "rel_path": task[1], "filename": task[2],
            "folder": task[3], "duration": float(task[4]), "status": task[5],
            "eligible": bool(task[6]), "category": task[7],
            "preprocessed_at": task[8].isoformat() if task[8] else None,
            "created_at": task[9].isoformat(), "updated_at": task[10].isoformat(),
            "current_version_id": str(task[11]) if task[11] else None,
            "baseline_version_id": str(task[12]) if task[12] else None,
            "baseline_quality": task[13],
            "reserved_for_user_id": str(task[14]) if task[14] else None,
            "display_version_id": str(display_version_id) if display_version_id else None,
            "segments": segments, "versions": version_items,
            "assignment": ({
                "annotator_id": str(assignment[0]), "username": assignment[1],
                "mode": assignment[2], "working_version_id": str(assignment[3]),
                "assigned_at": assignment[4].isoformat(),
                "last_activity_at": assignment[5].isoformat(),
            } if assignment else None),
        }
        from annotation_metadata.serializers import attach_metadata
        attach_metadata(
            cur, payload, tid,
            version_id=display_version_id,
            published_version_id=task[11],
            include_draft_review=True,
        )
        from annotation_metadata.repository import list_source_history
        payload["source_history"] = list_source_history(cur, tid)
        return payload


def admin_authorized_media(task_id: str) -> dict:
    tid = _validate_uuid(task_id, "task_id")
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """SELECT t.rel_path, w.payload FROM annotation_tasks t
               LEFT JOIN waveforms w ON w.task_id = t.id WHERE t.id = %s""",
            (tid,),
        ).fetchone()
        if not row:
            raise NotFoundError("Task not found")
        waveform = (
            base64.b64encode(bytes(row[1])).decode("ascii")
            if row[1] is not None else None
        )
        return {"rel_path": row[0], "waveform_b64": waveform}


def admin_audit(filters: dict | None = None, limit: int = 50,
                cursor: str | None = None) -> dict:
    normalized = _normalize_admin_filters(filters)
    raw = dict(filters or {})
    limit = max(1, min(int(limit), 100))
    clauses = ["true"]
    params: list = []
    if normalized["from"]:
        clauses.append("aa.created_at >= %s")
        params.append(normalized["from"])
    if normalized["to"]:
        clauses.append("aa.created_at < %s")
        params.append(normalized["to"])
    if normalized["action_type"] == "admin_auth":
        clauses.append(
            "aa.action_type IN ('admin_auth_success', 'admin_auth_failure')"
        )
    elif normalized["action_type"]:
        clauses.append("aa.action_type = %s")
        params.append(normalized["action_type"])
    if normalized["q"]:
        clauses.append(
            "(aa.reason ILIKE %s OR aa.action_type ILIKE %s "
            "OR aa.request::text ILIKE %s OR aa.summary::text ILIKE %s "
            "OR COALESCE(aa.admin_key_id, ses.key_id, '') ILIKE %s)"
        )
        like = f"%{normalized['q']}%"
        params.extend([like, like, like, like, like])
    if raw.get("key_id"):
        clauses.append("COALESCE(aa.admin_key_id, ses.key_id) = %s")
        params.append(str(raw["key_id"]))
    if normalized["annotator_id"]:
        clauses.append(
            "EXISTS (SELECT 1 FROM admin_action_items ai "
            "WHERE ai.admin_action_id = aa.id AND ai.annotator_id = %s)"
        )
        params.append(normalized["annotator_id"])
    if raw.get("task_id"):
        clauses.append(
            "EXISTS (SELECT 1 FROM admin_action_items ai "
            "WHERE ai.admin_action_id = aa.id AND ai.task_id = %s)"
        )
        params.append(_validate_uuid(raw["task_id"], "task_id"))
    if cursor:
        created_at, action_id = _decode_admin_cursor(cursor, 2)
        created_at = _parse_admin_datetime(created_at, "cursor")
        action_id = _validate_uuid(action_id, "cursor")
        clauses.append("(aa.created_at, aa.id) < (%s::timestamptz, %s::uuid)")
        params.extend([created_at, action_id])
    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""SELECT aa.id, aa.operation_id, aa.action_type, aa.reason,
                       aa.status, aa.request, aa.summary, aa.created_at,
                       aa.completed_at, aa.admin_session_id,
                       COALESCE(aa.admin_key_id, ses.key_id),
                       (SELECT count(*) FROM admin_action_items ai
                        WHERE ai.admin_action_id = aa.id)
                FROM admin_actions aa
                LEFT JOIN admin_sessions ses ON ses.id = aa.admin_session_id
                WHERE {' AND '.join(clauses)}
                ORDER BY aa.created_at DESC, aa.id DESC LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": str(row[0]), "action_id": str(row[0]),
            "operation_id": str(row[1]),
            "action_type": row[2], "reason": row[3], "status": row[4],
            "request": row[5] or {}, "summary": row[6] or {},
            "created_at": row[7].isoformat(),
            "completed_at": row[8].isoformat() if row[8] else None,
            "admin_session_id": str(row[9]) if row[9] else None,
            "key_id": row[10], "item_count": int(row[11]),
        }
        for row in rows
    ]
    next_cursor = None
    if has_more and rows:
        next_cursor = _encode_admin_cursor(rows[-1][7], rows[-1][0])
    return {"items": items, "next_cursor": next_cursor}


# ============================================================
# Admin state transitions
# ============================================================
def _canonical_request_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _required_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if not value:
        raise ValidationError("reason is required")
    if len(value) > 4000:
        raise ValidationError("reason is too long")
    return value


def _normalize_revoke_items(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValidationError("items must be a non-empty array")
    if len(items) > 100:
        raise ValidationError("A revoke batch can contain at most 100 tasks")
    normalized: list[dict] = []
    seen: set[uuid.UUID] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("Each item must be an object")
        task_id = _validate_uuid(item.get("task_id"), "task_id")
        version_id = _validate_uuid(
            item.get("expected_version_id"), "expected_version_id"
        )
        if task_id in seen:
            raise ValidationError(f"Task {task_id} appears more than once")
        seen.add(task_id)
        normalized.append({"task_id": task_id, "expected_version_id": version_id})
    return sorted(normalized, key=lambda item: str(item["task_id"]))


def _normalize_task_ids(task_ids: list[str]) -> list[uuid.UUID]:
    if not isinstance(task_ids, list) or not task_ids:
        raise ValidationError("task_ids must be a non-empty array")
    if len(task_ids) > 100:
        raise ValidationError("A batch can contain at most 100 tasks")
    result = sorted(
        {_validate_uuid(task_id, "task_id") for task_id in task_ids}, key=str
    )
    if len(result) != len(task_ids):
        raise ValidationError("task_ids must not contain duplicates")
    return result


def _begin_admin_action(cur, *, admin_session_id, operation_id,
                        action_type: str, reason: str,
                        request_hash: str, request_payload: dict):
    session_id = _validate_uuid(admin_session_id, "admin_session_id")
    op_id = _validate_uuid(operation_id, "operation_id")
    # Compatible admin write lock order:
    #   1. advisory lock on operation_id (idempotency)
    #   2. admin_sessions row FOR UPDATE
    #   3. annotator row FOR UPDATE when the command targets a person
    #   4. task then version, in stable id order
    # Callers must not lock an annotator before entering this function.
    # A globally stable lock makes simultaneous retries deterministic instead
    # of exposing the unique(operation_id) race as an IntegrityError.
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(op_id),)
    )
    prior = cur.execute(
        """SELECT id, admin_session_id, action_type, request_hash, summary
           FROM admin_actions WHERE operation_id = %s""",
        (op_id,),
    ).fetchone()
    if prior:
        if (prior[1] != session_id or prior[2] != action_type
                or prior[3] != request_hash):
            raise ConflictError(
                "operation_id was already used for a different admin request"
            )
        return {
            "replay": True, "action_id": prior[0],
            "operation_id": op_id, "summary": prior[4] or {},
        }
    session = cur.execute(
        """SELECT id, key_id FROM admin_sessions
           WHERE id = %s AND revoked_at IS NULL
             AND idle_expires_at > now() AND absolute_expires_at > now()
           FOR UPDATE""",
        (session_id,),
    ).fetchone()
    if not session:
        raise ForbiddenError("Admin session is no longer valid")
    action_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO admin_actions
               (id, operation_id, admin_session_id, admin_key_id,
                action_type, reason, request_hash, status, request,
                summary, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed', %s,
                   '{}'::jsonb, now())""",
        (action_id, op_id, session_id, session[1], action_type, reason,
         request_hash, Json(request_payload)),
    )
    return {
        "replay": False, "action_id": action_id,
        "operation_id": op_id, "summary": {},
    }


def _finish_admin_action(cur, action_id, summary: dict) -> None:
    cur.execute(
        """UPDATE admin_actions
           SET summary = %s, status = 'completed', completed_at = now()
           WHERE id = %s""",
        (Json(summary), action_id),
    )


def _admin_replay_response(action: dict) -> dict:
    return {
        "success": True,
        "action_id": str(action["action_id"]),
        "operation_id": str(action["operation_id"]),
        "idempotent_replay": True,
        "summary": action["summary"] or {},
    }


def _insert_admin_action_item(cur, action_id, *, task_id=None,
                              annotator_id=None, expected_version_id=None,
                              before_version_id=None, after_version_id=None,
                              result: str, details: dict | None = None) -> None:
    cur.execute(
        """INSERT INTO admin_action_items
               (admin_action_id, task_id, annotator_id, expected_version_id,
                before_version_id, after_version_id, result, details)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (action_id, task_id, annotator_id, expected_version_id,
         before_version_id, after_version_id, result, Json(details or {})),
    )


def _clone_clean_draft_from_baseline(cur, task_id, *, action_id,
                                     created_by_user_id=None):
    baseline = cur.execute(
        """SELECT t.baseline_version_id, t.baseline_quality, b.extra
           FROM annotation_tasks t
           LEFT JOIN annotation_versions b ON b.id = t.baseline_version_id
           WHERE t.id = %s AND b.lifecycle = 'baseline'""",
        (task_id,),
    ).fetchone()
    if not baseline or not baseline[0]:
        raise ConflictError(f"Task {task_id} has no usable baseline")
    if cur.execute(
        """SELECT 1 FROM annotation_versions
           WHERE task_id = %s AND lifecycle = 'draft'""",
        (task_id,),
    ).fetchone():
        raise ConflictError(f"Task {task_id} already has an open draft")
    version_no = cur.execute(
        """SELECT COALESCE(max(version_no), 0) + 1
           FROM annotation_versions WHERE task_id = %s""",
        (task_id,),
    ).fetchone()[0]
    draft_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO annotation_versions
               (id, task_id, version_no, lifecycle, target_status,
                base_version_id, revision, human_modified,
                created_by_user_id, skip_reasons, extra)
           VALUES (%s, %s, %s, 'draft', 'pending', %s, 0, false,
                   %s, '{}', %s)""",
        (draft_id, task_id, version_no, baseline[0], created_by_user_id,
         Json({
             "reset_from_baseline": True,
             "baseline_quality": baseline[1],
             "admin_action_id": str(action_id),
         })),
    )
    cur.execute(
        """INSERT INTO segments
               (version_id, segment_id, start_s, end_s, duration, asr_text,
                text, exclude_from_training, extra)
           SELECT %s, segment_id, start_s, end_s, duration, asr_text,
                  '', false, extra
           FROM segments WHERE version_id = %s""",
        (draft_id, baseline[0]),
    )
    return draft_id, baseline[1]


def _release_task_draft(cur, task_id, *, action_id, reason: str,
                        event: bool = True) -> dict | None:
    assignment = cur.execute(
        """SELECT a.user_id, a.working_version_id, a.mode, u.username,
                  v.human_modified, a.cross_check_round_id
           FROM assignments a
           JOIN annotators u ON u.id = a.user_id
           JOIN annotation_versions v ON v.id = a.working_version_id
           WHERE a.task_id = %s FOR UPDATE OF a""",
        (task_id,),
    ).fetchone()
    if assignment and assignment[2] == "cross_check":
        _cancel_in_progress_cross_check(
            cur, round_id=assignment[5], task_id=task_id,
            version_id=assignment[1], reason=reason,
        )
        cur.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, from_status,
                    to_status, admin_action_id, details)
               SELECT %s, t.id, %s, 'cross_check_cancelled', t.status, t.status,
                      %s, %s FROM annotation_tasks t WHERE t.id = %s""",
            (assignment[0], assignment[1], action_id,
             Json({
                 "mode": "cross_check",
                 "round_id": str(assignment[5]),
                 "termination_reason": reason,
             }), task_id),
        )
    drafts = cur.execute(
        """SELECT id FROM annotation_versions
           WHERE task_id = %s AND lifecycle = 'draft' FOR UPDATE""",
        (task_id,),
    ).fetchall()
    if assignment:
        if assignment[2] != "cross_check":
            cur.execute(
                """UPDATE annotation_versions
                   SET lifecycle = 'abandoned', updated_at = now()
                   WHERE id = %s AND lifecycle = 'draft'""",
                (assignment[1],),
            )
        cur.execute("DELETE FROM assignments WHERE task_id = %s", (task_id,))
        if event:
            cur.execute(
                """INSERT INTO annotation_events
                       (user_id, task_id, version_id, event_type, from_status,
                        to_status, admin_action_id, details)
                   SELECT %s, t.id, %s, 'released_admin', t.status, t.status,
                          %s, %s FROM annotation_tasks t WHERE t.id = %s""",
                (assignment[0], assignment[1], action_id,
                 Json({"mode": assignment[2], "reason": reason}), task_id),
            )
    for (draft_id,) in drafts:
        if not assignment or draft_id != assignment[1]:
            cur.execute(
                """UPDATE annotation_versions
                   SET lifecycle = 'abandoned', updated_at = now()
                   WHERE id = %s AND lifecycle = 'draft'""",
                (draft_id,),
            )
    if not assignment and not drafts:
        return None
    return {
        "annotator_id": assignment[0] if assignment else None,
        "username": assignment[3] if assignment else None,
        "working_version_id": assignment[1] if assignment else drafts[0][0],
        "mode": assignment[2] if assignment else "orphan_draft",
        "human_modified": bool(assignment[4]) if assignment else None,
    }


def _related_cross_check_user_ids(cur, task_ids) -> set:
    if not task_ids:
        return set()
    rows = cur.execute(
        """SELECT secondary_annotator_id
           FROM cross_check_rounds
           WHERE task_id = ANY(%s)
             AND state IN ('in_progress', 'awaiting_review')
           UNION
           SELECT user_id FROM assignments WHERE task_id = ANY(%s)""",
        (list(task_ids), list(task_ids)),
    ).fetchall()
    return {row[0] for row in rows if row[0]}


def _lock_annotators_stable(cur, user_ids, *, required_id=None) -> dict:
    ids = sorted({uid for uid in user_ids if uid}, key=str)
    if required_id is not None and required_id not in ids:
        ids = sorted(ids + [required_id], key=str)
    if not ids:
        return {}
    rows = cur.execute(
        """SELECT id, status FROM annotators
           WHERE id = ANY(%s) ORDER BY id FOR UPDATE""",
        (ids,),
    ).fetchall()
    found = {row[0]: row[1] for row in rows}
    if required_id is not None and required_id not in found:
        raise NotFoundError("Annotator not found")
    if any(uid not in found for uid in ids):
        raise ConflictError("Annotator no longer exists")
    return found


def _lock_assignments_for_tasks(cur, task_ids) -> None:
    if not task_ids:
        return
    cur.execute(
        """SELECT user_id FROM assignments
           WHERE task_id = ANY(%s)
           ORDER BY task_id
           FOR UPDATE""",
        (list(task_ids),),
    )


def _is_admin_edited_version(submitted_by, purpose, published_by) -> bool:
    return (
        purpose == "adjudication"
        and submitted_by is None
        and published_by is not None
    )


def _admin_edited_block_reclaim(annotator_id, block_reclaim: bool) -> bool:
    if annotator_id is None:
        if block_reclaim:
            raise ValidationError(
                "Admin-edited revoke requires block_reclaim=false"
            )
        return False
    return bool(block_reclaim)


def admin_revoke_preview(annotator_id: str | None, items: list[dict],
                         block_reclaim: bool = True,
                         release_conflicts: bool = False) -> dict:
    uid = (
        None if annotator_id is None
        else _validate_uuid(annotator_id, "annotator_id")
    )
    block_reclaim = _admin_edited_block_reclaim(uid, block_reclaim)
    normalized = _normalize_revoke_items(items)
    results = []
    with db_tx() as conn, conn.cursor() as cur:
        if uid is not None and not cur.execute(
            "SELECT 1 FROM annotators WHERE id = %s", (uid,)
        ).fetchone():
            raise NotFoundError("Annotator not found")
        for item in normalized:
            row = cur.execute(
                """SELECT t.id, t.filename, t.rel_path, t.duration, t.status,
                          t.current_published_version_id, t.baseline_version_id,
                          v.submitted_by_user_id,
                          EXISTS (SELECT 1 FROM assignments a
                                  WHERE a.task_id = t.id),
                          EXISTS (SELECT 1 FROM annotation_versions d
                                  WHERE d.task_id = t.id
                                    AND d.lifecycle = 'draft'),
                          v.purpose, v.published_by_admin_action_id,
                          (SELECT r.state FROM cross_check_rounds r
                           WHERE r.task_id = t.id
                             AND r.state IN ('in_progress', 'awaiting_review')
                           LIMIT 1)
                   FROM annotation_tasks t
                   LEFT JOIN annotation_versions v
                     ON v.id = t.current_published_version_id
                   WHERE t.id = %s""",
                (item["task_id"],),
            ).fetchone()
            conflict = None
            open_state = row[12] if row else None
            if not row:
                conflict = "not_found"
            elif row[5] != item["expected_version_id"]:
                conflict = "current_version_changed"
            elif uid is None:
                if not _is_admin_edited_version(row[7], row[10], row[11]):
                    conflict = "not_admin_adjudication"
            elif row[7] != uid:
                conflict = "not_current_submitter"
            if conflict is None and row[6] is None:
                conflict = "baseline_missing"
            if conflict is None and (row[8] or row[9] or open_state) and not release_conflicts:
                conflict = "active_revision"
            results.append({
                "task_id": str(item["task_id"]),
                "expected_version_id": str(item["expected_version_id"]),
                "filename": row[1] if row else None,
                "rel_path": row[2] if row else None,
                "duration_seconds": float(row[3]) if row else 0.0,
                "revokeable": conflict is None,
                "conflict": conflict,
                "will_block_reclaim": bool(block_reclaim and conflict is None),
                "will_release_assignment": bool(
                    row and (row[8] or row[9]) and release_conflicts
                ),
                "open_cross_check_state": open_state,
                "will_invalidate_cross_check": bool(
                    open_state and release_conflicts and conflict is None
                ),
            })
    revokeable = [item for item in results if item["revokeable"]]
    return {
        "summary": {
            "requested": len(results), "revokeable": len(revokeable),
            "conflicts": len(results) - len(revokeable),
            "duration_seconds": sum(
                item["duration_seconds"] for item in revokeable
            ),
            "open_cross_check_rounds": sum(
                1 for item in results if item.get("open_cross_check_state")
            ),
        },
        "items": results,
    }


def _revoke_locked_task(cur, *, task_row, expected_version_id,
                        annotator_id, action_id, reason: str,
                        block_reclaim: bool, release_conflicts: bool):
    from annotation_quality.repository import (
        invalidate_open_cross_check_for_task, lock_open_round_for_task,
    )
    task_id, duration, task_status, current_id, baseline_id = task_row
    if current_id != expected_version_id:
        raise ConflictError(f"Task {task_id} current version changed")
    open_round = lock_open_round_for_task(cur, task_id)
    version_ids = {current_id}
    if open_round and open_round[2]:
        version_ids.add(open_round[2])
    for version_id in sorted(version_ids, key=str):
        cur.execute(
            "SELECT id FROM annotation_versions WHERE id = %s FOR UPDATE",
            (version_id,),
        )
    version = cur.execute(
        """SELECT id, submitted_by_user_id, target_status, lifecycle,
                  purpose, published_by_admin_action_id
           FROM annotation_versions WHERE id = %s""",
        (current_id,),
    ).fetchone()
    if not version or version[3] != "published":
        raise ConflictError(f"Task {task_id} no longer has a published version")
    if annotator_id is None:
        if not _is_admin_edited_version(version[1], version[4], version[5]):
            raise ConflictError(
                f"Task {task_id} is not an admin-edited adjudication version"
            )
        block_reclaim = False
    elif version[1] != annotator_id:
        raise ConflictError(f"Task {task_id} is not currently submitted by annotator")
    if baseline_id is None:
        raise ConflictError(f"Task {task_id} has no baseline")
    has_conflict = cur.execute(
        """SELECT EXISTS (SELECT 1 FROM assignments WHERE task_id = %s),
                  EXISTS (SELECT 1 FROM annotation_versions
                          WHERE task_id = %s AND lifecycle = 'draft')""",
        (task_id, task_id),
    ).fetchone()
    released = None
    invalidated = None
    if has_conflict[0] or has_conflict[1] or open_round:
        if not release_conflicts:
            raise ConflictError(f"Task {task_id} has an active revision")
        if open_round:
            invalidated = invalidate_open_cross_check_for_task(
                cur, task_id=task_id, action_id=action_id, reason=reason,
                from_status=task_status,
            )
            if invalidated and invalidated.get("released"):
                released = {
                    "annotator_id": invalidated["secondary_annotator_id"],
                    "working_version_id": invalidated["secondary_version_id"],
                    "mode": "cross_check",
                }
        still = cur.execute(
            """SELECT EXISTS (SELECT 1 FROM assignments WHERE task_id = %s),
                      EXISTS (SELECT 1 FROM annotation_versions
                              WHERE task_id = %s AND lifecycle = 'draft')""",
            (task_id, task_id),
        ).fetchone()
        if still[0] or still[1]:
            released = _release_task_draft(
                cur, task_id, action_id=action_id, reason=reason
            )
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'revoked', revoked_at = now(), revoked_reason = %s,
               revoked_by_admin_action_id = %s, updated_at = now()
           WHERE id = %s AND lifecycle = 'published'""",
        (reason, action_id, current_id),
    )
    if cur.rowcount != 1:
        raise ConflictError(f"Task {task_id} version changed during revoke")
    cur.execute(
        """UPDATE annotation_tasks
           SET status = 'pending', current_published_version_id = NULL,
               reserved_for_user_id = NULL, updated_at = now()
           WHERE id = %s AND current_published_version_id = %s""",
        (task_id, current_id),
    )
    if cur.rowcount != 1:
        raise ConflictError(f"Task {task_id} changed during revoke")
    draft_id, baseline_quality = _clone_clean_draft_from_baseline(
        cur, task_id, action_id=action_id
    )
    if block_reclaim and annotator_id is not None:
        cur.execute(
            """INSERT INTO task_annotator_blocks
                   (task_id, user_id, reason, admin_action_id)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (task_id, user_id) DO UPDATE SET
                   reason = EXCLUDED.reason,
                   admin_action_id = EXCLUDED.admin_action_id,
                   created_at = now()""",
            (task_id, annotator_id, reason, action_id),
        )
    event_details = {"reason": reason, "clean_draft_id": str(draft_id)}
    if invalidated:
        event_details["invalidated_round_id"] = str(invalidated["round_id"])
        event_details["invalidated_previous_state"] = invalidated["previous_state"]
    if annotator_id is None:
        event_details["actual_author"] = "admin"
        event_details["source_publish_action_id"] = str(version[5])
    cur.execute(
        """INSERT INTO annotation_events
               (user_id, task_id, version_id, event_type, from_status,
                to_status, admin_action_id, details)
           VALUES (%s, %s, %s, 'revoked_admin', %s, 'pending', %s, %s)""",
        (annotator_id, task_id, current_id, task_status, action_id,
         Json(event_details)),
    )
    return {
        "task_id": task_id, "before_version_id": current_id,
        "after_version_id": draft_id, "duration": float(duration),
        "blocked": bool(block_reclaim and annotator_id is not None),
        "released": released,
        "baseline_quality": baseline_quality,
        "target_status": version[2],
        "invalidated": invalidated,
        "published_by_admin_action_id": version[5],
        "purpose": version[4],
    }


def admin_revoke(admin_session_id: str, operation_id: str,
                 annotator_id: str | None, items: list[dict], reason: str,
                 block_reclaim: bool = True, confirm: bool = False,
                 release_conflicts: bool = False) -> dict:
    if not confirm:
        raise ValidationError("Revoke requires explicit confirmation")
    uid = (
        None if annotator_id is None
        else _validate_uuid(annotator_id, "annotator_id")
    )
    block_reclaim = _admin_edited_block_reclaim(uid, block_reclaim)
    normalized = _normalize_revoke_items(items)
    reason_value = _required_reason(reason)
    request_payload = {
        "annotator_id": str(uid) if uid is not None else None,
        "items": [
            {"task_id": str(item["task_id"]),
             "expected_version_id": str(item["expected_version_id"])}
            for item in normalized
        ],
        "reason": reason_value,
        "block_reclaim": bool(block_reclaim),
        "confirm": bool(confirm),
        "release_conflicts": bool(release_conflicts),
    }
    request_hash = _canonical_request_hash(request_payload)
    with db_tx() as conn, conn.cursor() as cur:
        action = _begin_admin_action(
            cur, admin_session_id=admin_session_id, operation_id=operation_id,
            action_type="revoke_annotations", reason=reason_value,
            request_hash=request_hash, request_payload=request_payload,
        )
        if action["replay"]:
            return _admin_replay_response(action)
        task_ids = [item["task_id"] for item in normalized]
        related = _related_cross_check_user_ids(cur, task_ids)
        _lock_annotators_stable(cur, related, required_id=uid)
        _lock_assignments_for_tasks(cur, task_ids)
        results = []
        for item in normalized:
            task = cur.execute(
                """SELECT id, duration, status, current_published_version_id,
                          baseline_version_id
                   FROM annotation_tasks WHERE id = %s FOR UPDATE""",
                (item["task_id"],),
            ).fetchone()
            if not task:
                raise NotFoundError(f"Task {item['task_id']} not found")
            result = _revoke_locked_task(
                cur, task_row=task,
                expected_version_id=item["expected_version_id"],
                annotator_id=uid, action_id=action["action_id"],
                reason=reason_value, block_reclaim=bool(block_reclaim),
                release_conflicts=bool(release_conflicts),
            )
            results.append(result)
            item_details = {
                "blocked": result["blocked"],
                "released_assignment": bool(result["released"]),
                "baseline_quality": result["baseline_quality"],
            }
            if result.get("invalidated"):
                item_details["invalidated_round_id"] = str(
                    result["invalidated"]["round_id"]
                )
                item_details["invalidated_previous_state"] = (
                    result["invalidated"]["previous_state"]
                )
            if uid is None:
                item_details["actual_author"] = "admin"
                item_details["source_publish_action_id"] = str(
                    result["published_by_admin_action_id"]
                )
            _insert_admin_action_item(
                cur, action["action_id"], task_id=result["task_id"],
                annotator_id=uid,
                expected_version_id=item["expected_version_id"],
                before_version_id=result["before_version_id"],
                after_version_id=result["after_version_id"], result="revoked",
                details=item_details,
            )
        summary = {
            "requested": len(normalized), "revoked": len(results),
            "blocked": sum(1 for result in results if result["blocked"]),
            "released": sum(1 for result in results if result["released"]),
            "invalidated": sum(
                1 for result in results if result.get("invalidated")
            ),
            "duration_seconds": sum(result["duration"] for result in results),
        }
        _finish_admin_action(cur, action["action_id"], summary)
        return {
            "success": True, "action_id": str(action["action_id"]),
            "operation_id": str(action["operation_id"]),
            "idempotent_replay": False, "summary": summary,
        }


def admin_restore(admin_session_id: str, operation_id: str,
                  admin_action_id: str, task_ids: list[str], reason: str,
                  confirm: bool = False) -> dict:
    if not confirm:
        raise ValidationError("Restore requires explicit confirmation")
    source_action_id = _validate_uuid(admin_action_id, "admin_action_id")
    tids = _normalize_task_ids(task_ids)
    reason_value = _required_reason(reason)
    request_payload = {
        "admin_action_id": str(source_action_id),
        "task_ids": [str(task_id) for task_id in tids],
        "reason": reason_value,
        "confirm": bool(confirm),
    }
    request_hash = _canonical_request_hash(request_payload)
    with db_tx() as conn, conn.cursor() as cur:
        action = _begin_admin_action(
            cur, admin_session_id=admin_session_id, operation_id=operation_id,
            action_type="restore_annotations", reason=reason_value,
            request_hash=request_hash, request_payload=request_payload,
        )
        if action["replay"]:
            return _admin_replay_response(action)
        source = cur.execute(
            """SELECT action_type, created_at FROM admin_actions WHERE id = %s""",
            (source_action_id,),
        ).fetchone()
        if not source or source[0] != "revoke_annotations":
            raise ConflictError("Source action is not a restorable revoke")
        # Lock every affected ordinary submitter before any task/version row.
        # Admin-edited items have annotator_id NULL and no ordinary user to
        # activate-check. This matches deactivate (annotator -> task ->
        # version) and prevents the reverse task -> annotator deadlock.
        source_items = {}
        for task_id in tids:
            source_item = cur.execute(
                """SELECT annotator_id, before_version_id, after_version_id
                   FROM admin_action_items
                   WHERE admin_action_id = %s AND task_id = %s
                     AND result = 'revoked'
                   ORDER BY id DESC LIMIT 1""",
                (source_action_id, task_id),
            ).fetchone()
            if not source_item:
                raise ConflictError(
                    f"Task {task_id} was not revoked by the source action"
                )
            source_items[task_id] = source_item
        annotator_ids = {item[0] for item in source_items.values() if item[0]}
        annotator_status = _lock_annotators_stable(cur, annotator_ids)
        for annotator_id_value in annotator_ids:
            if annotator_status.get(annotator_id_value) != "active":
                raise ConflictError(
                    "Restored annotation submitter is not active"
                )
        # An existing assignment is already a terminal restore conflict. Check
        # it without taking an assignment lock before acquiring task locks;
        # after a task is locked, claim cannot insert a new assignment.
        if cur.execute(
            "SELECT 1 FROM assignments WHERE task_id = ANY(%s) LIMIT 1",
            (tids,),
        ).fetchone():
            raise ConflictError("A task in this restore batch has already been claimed")
        restored = []
        for task_id in tids:
            source_item = source_items[task_id]
            task = cur.execute(
                """SELECT id, status, current_published_version_id
                   FROM annotation_tasks WHERE id = %s FOR UPDATE""",
                (task_id,),
            ).fetchone()
            if not task:
                raise NotFoundError(f"Task {task_id} not found")
            if task[1] != "pending" or task[2] is not None:
                raise ConflictError(f"Task {task_id} is no longer pending")
            if cur.execute(
                "SELECT 1 FROM assignments WHERE task_id = %s",
                (task_id,),
            ).fetchone():
                raise ConflictError(f"Task {task_id} has already been claimed")
            if cur.execute(
                """SELECT 1 FROM cross_check_rounds
                   WHERE task_id = %s
                     AND state IN ('in_progress', 'awaiting_review')""",
                (task_id,),
            ).fetchone():
                raise ConflictError(
                    f"Task {task_id} has an open cross-check",
                    code="cross_check_active",
                )
            if cur.execute(
                """SELECT 1 FROM annotation_events
                   WHERE task_id = %s AND event_type = 'claimed'
                     AND created_at > %s LIMIT 1""",
                (task_id, source[1]),
            ).fetchone():
                raise ConflictError(
                    f"Task {task_id} was claimed after it was revoked"
                )
            clean = cur.execute(
                """SELECT lifecycle, revision, human_modified, task_id
                   FROM annotation_versions WHERE id = %s FOR UPDATE""",
                (source_item[2],),
            ).fetchone()
            if (not clean or clean[0] != "draft" or clean[1] != 0
                    or clean[2] or clean[3] != task_id):
                raise ConflictError(
                    f"Task {task_id} clean draft has been changed"
                )
            old = cur.execute(
                """SELECT lifecycle, target_status, submitted_by_user_id,
                          purpose, published_by_admin_action_id
                   FROM annotation_versions WHERE id = %s FOR UPDATE""",
                (source_item[1],),
            ).fetchone()
            if not old or old[0] != "revoked":
                raise ConflictError(
                    f"Task {task_id} revoked version is no longer restorable"
                )
            if source_item[0] is None:
                if not _is_admin_edited_version(old[2], old[3], old[4]):
                    raise ConflictError(
                        f"Task {task_id} is not an admin-edited adjudication version"
                    )
            elif old[2] != source_item[0]:
                raise ConflictError(
                    f"Task {task_id} submitter does not match the revoke audit"
                )
            owner_action = cur.execute(
                """SELECT revoked_by_admin_action_id
                   FROM annotation_versions WHERE id = %s""",
                (source_item[1],),
            ).fetchone()[0]
            if owner_action != source_action_id:
                raise ConflictError(
                    f"Task {task_id} was changed by another admin action"
                )
            cur.execute(
                """UPDATE annotation_versions
                   SET lifecycle = 'abandoned', updated_at = now()
                   WHERE id = %s""",
                (source_item[2],),
            )
            cur.execute(
                """UPDATE annotation_versions
                   SET lifecycle = 'published', revoked_at = NULL,
                       revoked_reason = NULL,
                       revoked_by_admin_action_id = NULL, updated_at = now()
                   WHERE id = %s AND lifecycle = 'revoked'""",
                (source_item[1],),
            )
            cur.execute(
                """UPDATE annotation_tasks
                   SET status = %s, current_published_version_id = %s,
                       updated_at = now()
                   WHERE id = %s""",
                (old[1], source_item[1], task_id),
            )
            if source_item[0] is not None:
                cur.execute(
                    """DELETE FROM task_annotator_blocks
                       WHERE task_id = %s AND user_id = %s
                         AND admin_action_id = %s""",
                    (task_id, source_item[0], source_action_id),
                )
            cur.execute(
                """INSERT INTO annotation_events
                       (user_id, task_id, version_id, event_type, from_status,
                        to_status, admin_action_id, details)
                   VALUES (%s, %s, %s, 'restored_admin', 'pending', %s,
                           %s, %s)""",
                (old[2], task_id, source_item[1], old[1], action["action_id"],
                 Json({"reason": reason_value,
                       "source_admin_action_id": str(source_action_id)})),
            )
            _insert_admin_action_item(
                cur, action["action_id"], task_id=task_id,
                annotator_id=source_item[0],
                before_version_id=source_item[2],
                after_version_id=source_item[1], result="restored",
                details={"source_admin_action_id": str(source_action_id)},
            )
            restored.append(task_id)
        summary = {"requested": len(tids), "restored": len(restored)}
        _finish_admin_action(cur, action["action_id"], summary)
        return {
            "success": True, "action_id": str(action["action_id"]),
            "operation_id": str(action["operation_id"]),
            "idempotent_replay": False, "summary": summary,
        }


def admin_release_assignment(admin_session_id: str, operation_id: str,
                             task_id: str, reason: str,
                             confirm: bool = False) -> dict:
    if not confirm:
        raise ValidationError("Assignment release requires explicit confirmation")
    tid = _validate_uuid(task_id, "task_id")
    reason_value = _required_reason(reason)
    request_payload = {
        "task_id": str(tid), "reason": reason_value,
        "confirm": bool(confirm),
    }
    request_hash = _canonical_request_hash(request_payload)
    with db_tx() as conn, conn.cursor() as cur:
        action = _begin_admin_action(
            cur, admin_session_id=admin_session_id, operation_id=operation_id,
            action_type="release_assignment", reason=reason_value,
            request_hash=request_hash, request_payload=request_payload,
        )
        if action["replay"]:
            return _admin_replay_response(action)
        assignment_hint = cur.execute(
            "SELECT user_id FROM assignments WHERE task_id = %s",
            (tid,),
        ).fetchone()
        if not assignment_hint:
            raise ConflictError("Task has no active assignment")
        # Completion locks annotator -> assignment -> task -> version. Resolve
        # the owner without a row lock, then acquire the same leading lock.
        if not cur.execute(
            "SELECT 1 FROM annotators WHERE id = %s FOR UPDATE",
            (assignment_hint[0],),
        ).fetchone():
            raise ConflictError("Assignment owner no longer exists")
        assignment = cur.execute(
            """SELECT user_id, working_version_id, mode
               FROM assignments WHERE task_id = %s FOR UPDATE""",
            (tid,),
        ).fetchone()
        if not assignment or assignment[0] != assignment_hint[0]:
            raise ConflictError("Task assignment changed during release")
        # Match the annotator completion path after the shared user lock.
        task = cur.execute(
            """SELECT id, status, current_published_version_id
               FROM annotation_tasks WHERE id = %s FOR UPDATE""",
            (tid,),
        ).fetchone()
        if not task:
            raise NotFoundError("Task not found")
        released = _release_task_draft(
            cur, tid, action_id=action["action_id"], reason=reason_value
        )
        clean_draft_id = None
        if task[1] == "pending" and task[2] is None:
            clean_draft_id, _quality = _clone_clean_draft_from_baseline(
                cur, tid, action_id=action["action_id"]
            )
            cur.execute(
                """UPDATE annotation_tasks
                   SET reserved_for_user_id = NULL, updated_at = now()
                   WHERE id = %s""",
                (tid,),
            )
        _insert_admin_action_item(
            cur, action["action_id"], task_id=tid,
            annotator_id=assignment[0],
            before_version_id=assignment[1],
            after_version_id=clean_draft_id,
            result="assignment_released",
            details={"mode": assignment[2]},
        )
        summary = {
            "released": 1, "mode": assignment[2],
            "annotator_id": str(assignment[0]),
            "clean_draft_id": str(clean_draft_id) if clean_draft_id else None,
        }
        _finish_admin_action(cur, action["action_id"], summary)
        return {
            "success": True, "action_id": str(action["action_id"]),
            "operation_id": str(action["operation_id"]),
            "idempotent_replay": False, "summary": summary,
        }


def admin_deactivate_preview(annotator_id: str) -> dict:
    uid = _validate_uuid(annotator_id, "annotator_id")
    with db_tx() as conn, conn.cursor() as cur:
        user = cur.execute(
            """SELECT id, username, status, created_at
               FROM annotators WHERE id = %s""",
            (uid,),
        ).fetchone()
        if not user:
            raise NotFoundError("Annotator not found")
        published = cur.execute(
            """SELECT count(*), COALESCE(sum(t.duration), 0)
               FROM annotation_tasks t
               JOIN annotation_versions v
                 ON v.id = t.current_published_version_id
               WHERE v.submitted_by_user_id = %s""",
            (uid,),
        ).fetchone()
        assignment = cur.execute(
            """SELECT a.task_id, a.mode, a.working_version_id, t.filename,
                      t.rel_path, v.human_modified
               FROM assignments a
               JOIN annotation_tasks t ON t.id = a.task_id
               JOIN annotation_versions v ON v.id = a.working_version_id
               WHERE a.user_id = %s""",
            (uid,),
        ).fetchone()
        reservations = cur.execute(
            """SELECT count(*) FROM annotation_tasks
               WHERE reserved_for_user_id = %s""",
            (uid,),
        ).fetchone()[0]
        sessions = cur.execute(
            "SELECT count(*) FROM active_sessions WHERE user_id = %s", (uid,)
        ).fetchone()[0]
        own_in_progress = cur.execute(
            """SELECT count(*) FROM cross_check_rounds r
               JOIN assignments a ON a.cross_check_round_id = r.id
               WHERE a.user_id = %s AND r.state = 'in_progress'""",
            (uid,),
        ).fetchone()[0]
        awaiting_as_secondary = cur.execute(
            """SELECT count(*) FROM cross_check_rounds
               WHERE secondary_annotator_id = %s
                 AND state = 'awaiting_review'""",
            (uid,),
        ).fetchone()[0]
        open_on_published = cur.execute(
            """SELECT count(*) FROM cross_check_rounds r
               JOIN annotation_tasks t ON t.id = r.task_id
               JOIN annotation_versions v
                 ON v.id = t.current_published_version_id
               WHERE v.submitted_by_user_id = %s
                 AND r.state IN ('in_progress', 'awaiting_review')""",
            (uid,),
        ).fetchone()[0]
    return {
        "annotator": {
            "id": str(user[0]), "username": user[1], "status": user[2],
            "created_at": user[3].isoformat(),
        },
        "summary": {
            "published_to_revoke": int(published[0]),
            "published_duration_seconds": float(published[1]),
            "assignments_to_release": 1 if assignment else 0,
            "reservations_to_release": int(reservations),
            "sessions_to_revoke": int(sessions),
            "cross_check_in_progress_to_cancel": int(own_in_progress),
            "cross_check_awaiting_review_kept": int(awaiting_as_secondary),
            "cross_check_open_on_published": int(open_on_published),
        },
        "assignment": ({
            "task_id": str(assignment[0]), "mode": assignment[1],
            "working_version_id": str(assignment[2]),
            "filename": assignment[3], "rel_path": assignment[4],
            "human_modified": bool(assignment[5]),
        } if assignment else None),
    }


def admin_deactivate(admin_session_id: str, operation_id: str,
                     annotator_id: str, reason: str,
                     confirm: bool = False,
                     confirm_username: str | None = None) -> dict:
    if not confirm:
        raise ValidationError("Deactivation requires explicit confirmation")
    uid = _validate_uuid(annotator_id, "annotator_id")
    reason_value = _required_reason(reason)
    username_confirmation = (
        str(confirm_username).strip() if confirm_username is not None else None
    )
    request_payload = {
        "annotator_id": str(uid), "reason": reason_value,
        "confirm": bool(confirm), "confirm_username": username_confirmation,
    }
    request_hash = _canonical_request_hash(request_payload)
    with db_tx() as conn, conn.cursor() as cur:
        action = _begin_admin_action(
            cur, admin_session_id=admin_session_id, operation_id=operation_id,
            action_type="deactivate_annotator", reason=reason_value,
            request_hash=request_hash, request_payload=request_payload,
        )
        if action["replay"]:
            return _admin_replay_response(action)
        user = cur.execute(
            """SELECT id, username, status FROM annotators
               WHERE id = %s FOR UPDATE""",
            (uid,),
        ).fetchone()
        if not user:
            raise NotFoundError("Annotator not found")
        if username_confirmation is not None and username_confirmation != user[1]:
            raise ValidationError("confirm_username does not match the annotator")
        if user[2] != "active":
            raise ConflictError("Annotator is already deactivated")

        cur.execute("DELETE FROM active_sessions WHERE user_id = %s", (uid,))
        session_count = cur.rowcount
        released_count = 0
        assignment = cur.execute(
            """SELECT a.task_id, a.working_version_id, a.mode,
                      t.status, t.current_published_version_id
               FROM assignments a
               JOIN annotation_tasks t ON t.id = a.task_id
               WHERE a.user_id = %s FOR UPDATE OF a, t""",
            (uid,),
        ).fetchone()
        if assignment:
            released = _release_task_draft(
                cur, assignment[0], action_id=action["action_id"],
                reason=reason_value
            )
            clean_draft_id = None
            if assignment[3] == "pending" and assignment[4] is None:
                clean_draft_id, _quality = _clone_clean_draft_from_baseline(
                    cur, assignment[0], action_id=action["action_id"]
                )
            _insert_admin_action_item(
                cur, action["action_id"], task_id=assignment[0],
                annotator_id=uid, before_version_id=assignment[1],
                after_version_id=clean_draft_id,
                result="assignment_released",
                details={"mode": assignment[2],
                         "released": bool(released)},
            )
            released_count = 1

        cur.execute(
            """UPDATE annotation_tasks SET reserved_for_user_id = NULL,
                      updated_at = now()
               WHERE reserved_for_user_id = %s""",
            (uid,),
        )
        reservation_count = cur.rowcount

        published_ids = [
            row[0] for row in cur.execute(
                """SELECT t.id FROM annotation_tasks t
                   JOIN annotation_versions v
                     ON v.id = t.current_published_version_id
                   WHERE v.submitted_by_user_id = %s ORDER BY t.id""",
                (uid,),
            ).fetchall()
        ]
        revoked = []
        for task_id_value in published_ids:
            task = cur.execute(
                """SELECT id, duration, status, current_published_version_id,
                          baseline_version_id
                   FROM annotation_tasks WHERE id = %s FOR UPDATE""",
                (task_id_value,),
            ).fetchone()
            other_assignment = cur.execute(
                """SELECT user_id FROM assignments WHERE task_id = %s""",
                (task_id_value,),
            ).fetchone()
            if other_assignment and other_assignment[0] != uid:
                raise ConflictError(
                    f"Task {task_id_value} has another annotator's active revision"
                )
            from annotation_quality.repository import (
                invalidate_open_cross_check_for_task,
            )
            invalidate_open_cross_check_for_task(
                cur, task_id=task_id_value, action_id=action["action_id"],
                reason=reason_value, from_status=task[2],
            )
            result = _revoke_locked_task(
                cur, task_row=task,
                expected_version_id=task[3], annotator_id=uid,
                action_id=action["action_id"], reason=reason_value,
                block_reclaim=True, release_conflicts=False,
            )
            revoked.append(result)
            _insert_admin_action_item(
                cur, action["action_id"], task_id=result["task_id"],
                annotator_id=uid,
                expected_version_id=result["before_version_id"],
                before_version_id=result["before_version_id"],
                after_version_id=result["after_version_id"], result="revoked",
                details={"offboarding": True,
                         "baseline_quality": result["baseline_quality"]},
            )

        cur.execute(
            """UPDATE annotators
               SET status = 'deactivated', deactivated_at = now(),
                   deactivated_reason = %s
               WHERE id = %s""",
            (reason_value, uid),
        )
        summary = {
            "revoked": len(revoked), "released": released_count,
            "blocked": len(revoked),
            "duration_seconds": sum(item["duration"] for item in revoked),
            "reservations_released": int(reservation_count),
            "sessions_revoked": int(session_count),
        }
        _finish_admin_action(cur, action["action_id"], summary)
        return {
            "success": True, "action_id": str(action["action_id"]),
            "operation_id": str(action["operation_id"]),
            "idempotent_replay": False, "summary": summary,
        }


# ============================================================
# Authorized media (current assignment or own published submission)
# ============================================================
def authorized_media(user_id: str, task_id: str) -> dict:
    uid = _validate_uuid(user_id, "user_id")
    tid = _validate_uuid(task_id, "task_id")
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """SELECT t.rel_path, w.payload
               FROM annotation_tasks t
               LEFT JOIN waveforms w ON w.task_id = t.id
               WHERE t.id = %s AND (
                 EXISTS (
                   SELECT 1 FROM assignments a
                   WHERE a.task_id = t.id AND a.user_id = %s
                 ) OR EXISTS (
                   SELECT 1 FROM annotation_versions v
                   WHERE v.id = t.current_published_version_id
                     AND v.submitted_by_user_id = %s
                 ) OR EXISTS (
                   SELECT 1 FROM cross_check_rounds r
                   WHERE r.task_id = t.id
                     AND r.secondary_annotator_id = %s
                     AND r.submitted_at IS NOT NULL
                 )
               )""",
            (tid, uid, uid, uid),
        ).fetchone()
        if not row:
            raise ForbiddenError("You may only access media for your current task or your own submissions")
        waveform = (
            base64.b64encode(bytes(row[1])).decode("ascii")
            if row[1] is not None else None
        )
        return {"rel_path": row[0], "waveform_b64": waveform}


def admin_correct_scene_review(admin_session_id: str, task_id: str,
                               payload: dict) -> dict:
    from annotation_metadata.contracts import AdminSceneReviewCommand, parse_strict
    from annotation_metadata.reviews import admin_correct_review
    command = parse_strict(AdminSceneReviewCommand, payload)
    with db_tx() as conn, conn.cursor() as cur:
        return admin_correct_review(
            cur, admin_session_id=admin_session_id, command=command,
            task_id=task_id,
        )


def admin_set_scene_scope(admin_session_id: str, annotator_id: str,
                          payload: dict) -> dict:
    from annotation_metadata.contracts import SceneScopeCommand, parse_strict
    from annotation_metadata.repository import replace_scope, scope_payload
    command = parse_strict(SceneScopeCommand, payload)
    uid = _validate_uuid(annotator_id, "annotator_id")
    reason_value = _required_reason(command.reason)
    request_payload = {**command.model_dump(), "annotator_id": str(uid)}
    request_hash = _canonical_request_hash(request_payload)
    with db_tx() as conn, conn.cursor() as cur:
        # admin_session then annotator: same order as revoke/deactivate.
        action = _begin_admin_action(
            cur, admin_session_id=admin_session_id,
            operation_id=command.operation_id,
            action_type="set_scene_scope", reason=reason_value,
            request_hash=request_hash, request_payload=request_payload,
        )
        if action.get("replay"):
            return _admin_replay_response(action)
        cur.execute(
            "SELECT id, username FROM annotators WHERE id = %s FOR UPDATE",
            (uid,),
        )
        user = cur.fetchone()
        if not user:
            raise NotFoundError("Annotator not found")
        scope = replace_scope(
            cur, uid, mode=command.mode, scene_codes=command.scene_codes,
            allow_unknown=command.allow_unknown,
            expected_revision=command.expected_revision,
        )
        summary = {
            "annotator_id": str(uid),
            "username": user[1],
            "scope": scope_payload(scope),
        }
        _insert_admin_action_item(
            cur, action["action_id"], annotator_id=uid, result="updated",
            details=summary,
        )
        _finish_admin_action(cur, action["action_id"], summary)
        return {"success": True, "action_id": str(action["action_id"]), **summary}


def admin_metadata_facets(filters: dict | None = None) -> dict:
    from annotation_metadata.repository import list_active_scenes, list_batches
    from annotation_metadata.taxonomy import CONFIDENCE_LEVELS, REVIEW_STATUSES
    normalized = _normalize_admin_filters(filters)
    with db_tx() as conn, conn.cursor() as cur:
        scenes = list_active_scenes(cur)
        batches = list_batches(cur)
    return {
        "scenes": scenes,
        "batches": batches,
        "confidences": list(CONFIDENCE_LEVELS),
        "review_statuses": list(REVIEW_STATUSES) + [
            "unreviewed_unpublished", "unreviewed_published",
        ],
        "applied_filters": _applied_admin_filters(normalized),
        "as_of": utcnow().isoformat(),
        "definitions": {
            "facets": (
                "vocabulary and filter options, not grouped statistics; "
                "counts and durations live on admin_overview"
            ),
        },
    }


# ============================================================
# UUID helpers
# ============================================================
def _validate_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError(f"Invalid {field}: {value!r}")


def _opt_uuid(value: str | None) -> uuid.UUID | None:
    return _validate_uuid(value, "operation_id") if value else None


def waveform_checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
