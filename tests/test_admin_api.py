from __future__ import annotations

import base64
import hashlib
import json
import uuid

import pytest

import annotation_repository as repo
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


def _admin_login(client) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/admin/login", json={"key": ADMIN_KEY})
    assert response.status_code == 200, response.json
    assert response.json["authenticated"] is True
    assert response.json["key_id"] == "primary"
    csrf = response.json["csrf_token"]
    assert isinstance(csrf, str) and len(csrf) >= 32
    return response.json, {"X-CSRF-Token": csrf}


def _segments(assignment: dict, prefix: str = "human") -> list[dict]:
    return [
        {
            "id": segment["id"],
            "start": segment["start"],
            "end": segment["end"],
            "duration": segment["duration"],
            "text": f"{prefix} {segment['id']}",
            "exclude_from_training": False,
        }
        for segment in assignment["segments"]
    ]


def _make_user(username: str) -> dict:
    return repo.login(username, str(uuid.uuid4()), 1800)


def _complete_next(user: dict, *, prefix: str = "human",
                   status: str = "annotated") -> dict:
    assignment = repo.claim(user["id"])
    result = repo.complete(
        user["id"], assignment["lease_token"], assignment["revision"],
        status, ["noisy"] if status == "skipped" else [],
        _segments(assignment, prefix) if status == "annotated" else [],
        str(uuid.uuid4()), f"api-complete-{uuid.uuid4()}",
    )
    return {**result, "version_id": assignment["version_id"]}


def _revoke_body(user: dict, *completed: dict,
                 operation_id: str | None = None) -> dict:
    return {
        "operation_id": operation_id or str(uuid.uuid4()),
        "annotator_id": str(user["id"]),
        "items": [
            {
                "task_id": item["task_id"],
                "expected_version_id": item["version_id"],
            }
            for item in completed
        ],
        "reason": "quality review failed",
        "block_reclaim": True,
        "confirm": True,
    }


def _opaque_cursor(*values: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(list(values), separators=(",", ":")).encode()
    ).decode().rstrip("=")


def test_admin_auth_fails_closed_rejects_wrong_key_and_sets_safe_cookie(
        client, database, monkeypatch, caplog):
    import db

    monkeypatch.delenv("ANNOTATION_ADMIN_KEY_SHA256", raising=False)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_SHA256", "")
    disabled = client.post("/api/admin/login", json={"key": ADMIN_KEY})
    assert disabled.status_code == 503
    assert disabled.json["error"] == "Admin authentication is not configured"

    monkeypatch.setenv("ANNOTATION_ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_ID", "primary")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_SECURE", False)
    wrong_key = "wrong-" + ADMIN_KEY
    rejected = client.post("/api/admin/login", json={"key": wrong_key})
    assert rejected.status_code == 401
    assert rejected.json == {"error": "Invalid admin credentials"}
    assert wrong_key not in caplog.text

    response = client.post("/api/admin/login", json={"key": ADMIN_KEY})
    assert response.status_code == 200
    cookie = response.headers.get("Set-Cookie", "")
    assert "admin_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert ADMIN_KEY not in cookie
    assert response.headers["Cache-Control"] == "no-store"

    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT key_id, token_digest, csrf_digest FROM admin_sessions"
        ).fetchone()
    assert row[0] == "primary"
    assert ADMIN_KEY not in repr(row)
    assert wrong_key not in repr(row)


def test_admin_session_logout_expiry_and_csrf(admin_client, database, seed_tasks):
    import db

    seed_tasks(1)
    alice = _make_user("alice")
    completed = _complete_next(alice)
    _, headers = _admin_login(admin_client)
    assert admin_client.get("/api/admin/session").status_code == 200

    body = _revoke_body(alice, completed)
    body.pop("operation_id")
    body.pop("reason")
    body.pop("confirm")
    missing = admin_client.post(
        "/api/admin/annotations/revoke/preview", json=body,
    )
    assert missing.status_code == 403
    wrong = admin_client.post(
        "/api/admin/annotations/revoke/preview", json=body,
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert wrong.status_code == 403
    valid = admin_client.post(
        "/api/admin/annotations/revoke/preview", json=body, headers=headers,
    )
    assert valid.status_code == 200

    assert admin_client.post("/api/admin/logout").status_code == 403
    logged_out = admin_client.post("/api/admin/logout", headers=headers)
    assert logged_out.status_code == 200
    assert admin_client.get("/api/admin/session").status_code == 401

    _, _ = _admin_login(admin_client)
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE admin_sessions SET idle_expires_at = now() - interval '1 second' "
            "WHERE revoked_at IS NULL"
        )
        conn.commit()
    expired = admin_client.get("/api/admin/session")
    assert expired.status_code == 401

    _admin_login(admin_client)
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE admin_sessions
               SET idle_expires_at = now() - interval '2 seconds',
                   absolute_expires_at = now() - interval '1 second'
               WHERE revoked_at IS NULL"""
        )
        conn.commit()
    assert admin_client.get("/api/admin/session").status_code == 401


def test_admin_and_annotator_sessions_are_separate(admin_client, database):
    annotator_client = server.app.test_client()
    assert annotator_client.post(
        "/api/login", json={"username": "alice"},
    ).status_code == 200
    assert annotator_client.get("/api/admin/overview").status_code == 401

    _admin_login(admin_client)
    assert admin_client.get("/api/current-user").status_code == 401
    overview = admin_client.get("/api/admin/overview")
    assert overview.status_code == 200
    assert overview.headers["Cache-Control"] == "no-store"


def test_admin_overview_annotator_detail_and_keyset_pagination(
        admin_client, database, seed_tasks):
    seed_tasks(4)
    alice = _make_user("alice")
    bob = _make_user("bob")
    carol = _make_user("carol")
    _complete_next(alice, prefix="alice")
    _complete_next(bob, prefix="bob", status="skipped")
    _complete_next(carol, prefix="carol")
    _admin_login(admin_client)

    overview = admin_client.get("/api/admin/overview")
    assert overview.status_code == 200
    assert overview.json["totals"]["total_audio_count"] == 4
    assert overview.json["totals"]["annotated_count"] == 2
    assert overview.json["totals"]["skipped_count"] == 1
    assert overview.json["totals"]["pending_count"] == 1

    first = admin_client.get("/api/admin/annotators?limit=2")
    assert first.status_code == 200
    assert len(first.json["items"]) == 2
    assert first.json["next_cursor"]
    second = admin_client.get(
        "/api/admin/annotators",
        query_string={"limit": 2, "cursor": first.json["next_cursor"]},
    )
    assert second.status_code == 200
    identifiers = [item["id"] for item in first.json["items"] + second.json["items"]]
    assert len(identifiers) == len(set(identifiers)) == 3

    detail = admin_client.get(f"/api/admin/annotators/{alice['id']}")
    assert detail.status_code == 200
    assert detail.json["username"] == "alice"
    assert detail.json["current"]["annotated_count"] == 1
    records = admin_client.get(
        f"/api/admin/annotators/{alice['id']}/annotations?limit=10"
    )
    assert records.status_code == 200
    assert len(records.json["items"]) == 1

    assert admin_client.get("/api/admin/annotators?limit=abc").status_code == 400
    assert admin_client.get(
        "/api/admin/annotators?cursor=invalid-cursor"
    ).status_code == 400


def test_admin_quality_api_combines_queue_filters(
        admin_client, database, seed_tasks):
    import db

    seed_tasks(2)
    alice = _make_user("alice")
    bob = _make_user("bob")
    _complete_next(alice, prefix="alice")
    stale_assignment = repo.claim(bob["id"])
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE assignments SET last_activity_at = now() - interval '5 hours' "
            "WHERE task_id = %s",
            (stale_assignment["task_id"],),
        )
        conn.commit()
    _admin_login(admin_client)

    response = admin_client.get(
        "/api/admin/quality",
        query_string={
            "q": stale_assignment["filename"],
            "annotator_id": bob["id"],
            "signal": "stale_assignment",
        },
    )
    assert response.status_code == 200, response.json
    assert [item["task_id"] for item in response.json["items"]] == [
        stale_assignment["task_id"]
    ]
    assert response.json["items"][0]["type"] == "stale_assignment"

    invalid = admin_client.get(
        "/api/admin/quality", query_string={"signal": "unknown"},
    )
    assert invalid.status_code == 400
    assert "signal must be" in invalid.json["error"]


def test_admin_naive_shanghai_dates_are_interpreted_as_local_midnight(
        admin_client, database):
    _admin_login(admin_client)
    response = admin_client.get(
        "/api/admin/timeseries",
        query_string={
            "from": "2026-08-28",
            "to": "2026-08-29",
            "timezone": "Asia/Shanghai",
            "bucket": "day",
        },
    )
    assert response.status_code == 200, response.json
    assert response.json["from"] == "2026-08-27T16:00:00+00:00"
    assert response.json["to"] == "2026-08-28T16:00:00+00:00"
    assert response.json["timezone"] == "Asia/Shanghai"


def test_all_admin_cursors_reject_malformed_and_semantically_invalid_values(
        admin_client, database):
    alice = _make_user("alice")
    _admin_login(admin_client)
    endpoints = [
        "/api/admin/annotators",
        "/api/admin/tasks",
        "/api/admin/annotations",
        f"/api/admin/annotators/{alice['id']}/annotations",
        "/api/admin/audit",
    ]

    for endpoint in endpoints:
        malformed = admin_client.get(
            endpoint, query_string={"cursor": "***not-base64***"},
        )
        assert malformed.status_code == 400, (endpoint, malformed.json)

    invalid_uuid_cursors = {
        "/api/admin/annotators": _opaque_cursor("alice", "not-a-uuid"),
        "/api/admin/tasks": _opaque_cursor(
            "2026-08-28T00:00:00+00:00", "not-a-uuid"
        ),
        "/api/admin/annotations": _opaque_cursor(
            "2026-08-28T00:00:00+00:00", "not-a-uuid"
        ),
        f"/api/admin/annotators/{alice['id']}/annotations": _opaque_cursor(
            "2026-08-28T00:00:00+00:00", "not-a-uuid"
        ),
        "/api/admin/audit": _opaque_cursor(
            "2026-08-28T00:00:00+00:00", "not-a-uuid"
        ),
    }
    for endpoint, cursor in invalid_uuid_cursors.items():
        response = admin_client.get(endpoint, query_string={"cursor": cursor})
        assert response.status_code == 400, (endpoint, response.json)

    for endpoint in endpoints[1:]:
        response = admin_client.get(
            endpoint,
            query_string={
                "cursor": _opaque_cursor("not-a-date", str(uuid.uuid4()))
            },
        )
        assert response.status_code == 400, (endpoint, response.json)


def test_admin_revoke_preview_execute_replay_and_expected_version_conflict(
        admin_client, database, seed_tasks):
    import db

    seed_tasks(2)
    alice = _make_user("alice")
    first = _complete_next(alice, prefix="private")
    second = _complete_next(alice, prefix="keep")
    _, headers = _admin_login(admin_client)

    body = _revoke_body(alice, first)
    preview_body = {
        key: value for key, value in body.items()
        if key not in {"operation_id", "reason", "confirm"}
    }
    preview = admin_client.post(
        "/api/admin/annotations/revoke/preview",
        json=preview_body,
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json["summary"]["revokeable"] == 1
    with db.db_conn() as conn:
        assert conn.execute(
            "SELECT status FROM annotation_tasks WHERE id = %s",
            (first["task_id"],),
        ).fetchone()[0] == "annotated"

    revoked = admin_client.post(
        "/api/admin/annotations/revoke", json=body, headers=headers,
    )
    assert revoked.status_code == 200, revoked.json
    assert revoked.json["summary"]["revoked"] == 1
    replay = admin_client.post(
        "/api/admin/annotations/revoke", json=body, headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json["action_id"] == revoked.json["action_id"]
    assert replay.json["idempotent_replay"] is True

    stale = _revoke_body(alice, second)
    stale["items"][0]["expected_version_id"] = str(uuid.uuid4())
    conflict = admin_client.post(
        "/api/admin/annotations/revoke", json=stale, headers=headers,
    )
    assert conflict.status_code == 409
    with db.db_conn() as conn:
        assert conn.execute(
            "SELECT status FROM annotation_tasks WHERE id = %s",
            (second["task_id"],),
        ).fetchone()[0] == "annotated"


def test_admin_batch_revoke_is_atomic_via_api(admin_client, database, seed_tasks):
    import db

    seed_tasks(2)
    alice = _make_user("alice")
    first = _complete_next(alice, prefix="one")
    second = _complete_next(alice, prefix="two")
    _, headers = _admin_login(admin_client)
    body = _revoke_body(alice, first, second)
    body["admin_key"] = ADMIN_KEY
    body["items"][1]["expected_version_id"] = str(uuid.uuid4())

    response = admin_client.post(
        "/api/admin/annotations/revoke", json=body, headers=headers,
    )
    assert response.status_code == 409
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT status FROM annotation_tasks ORDER BY allocation_order"
        ).fetchall()
        actions = conn.execute(
            "SELECT count(*) FROM admin_actions WHERE operation_id = %s",
            (body["operation_id"],),
        ).fetchone()[0]
    assert rows == [("annotated",), ("annotated",)]
    assert actions == 0


def test_batch_revoke_requires_step_up_key_but_single_revoke_does_not(
        admin_client, database, seed_tasks):
    import db

    seed_tasks(3)
    alice = _make_user("alice")
    first = _complete_next(alice, prefix="single")
    second = _complete_next(alice, prefix="batch-one")
    third = _complete_next(alice, prefix="batch-two")
    _, headers = _admin_login(admin_client)

    single = admin_client.post(
        "/api/admin/annotations/revoke",
        json=_revoke_body(alice, first),
        headers=headers,
    )
    assert single.status_code == 200, single.json
    assert single.json["summary"]["revoked"] == 1

    batch_body = _revoke_body(alice, second, third)
    missing_key = admin_client.post(
        "/api/admin/annotations/revoke",
        json=batch_body,
        headers=headers,
    )
    assert missing_key.status_code == 400
    assert "admin_key" in missing_key.json["error"]
    with db.db_conn() as conn:
        untouched = conn.execute(
            """SELECT status FROM annotation_tasks
               WHERE id = ANY(%s) ORDER BY id""",
            ([second["task_id"], third["task_id"]],),
        ).fetchall()
    assert untouched == [("annotated",), ("annotated",)]

    approved = admin_client.post(
        "/api/admin/annotations/revoke",
        json={**batch_body, "admin_key": ADMIN_KEY},
        headers=headers,
    )
    assert approved.status_code == 200, approved.json
    assert approved.json["summary"]["requested"] == 2
    assert approved.json["summary"]["revoked"] == 2


def test_wrong_batch_revoke_step_up_key_is_rate_limited_and_audited(
        admin_client, database, seed_tasks, monkeypatch):
    import db

    seed_tasks(2)
    alice = _make_user("alice")
    first = _complete_next(alice, prefix="one")
    second = _complete_next(alice, prefix="two")
    _, headers = _admin_login(admin_client)
    monkeypatch.setitem(server.app.config, "ADMIN_LOGIN_MAX_FAILURES", 2)
    wrong_key = "definitely-wrong-batch-admin-key"
    body = {
        **_revoke_body(alice, first, second),
        "admin_key": wrong_key,
    }
    remote = {"REMOTE_ADDR": "198.51.100.77"}

    first_failure = admin_client.post(
        "/api/admin/annotations/revoke", json=body, headers=headers,
        environ_overrides=remote,
    )
    assert first_failure.status_code == 403
    second_failure = admin_client.post(
        "/api/admin/annotations/revoke", json=body, headers=headers,
        environ_overrides=remote,
    )
    assert second_failure.status_code == 429
    still_limited = admin_client.post(
        "/api/admin/annotations/revoke",
        json={**body, "admin_key": ADMIN_KEY},
        headers=headers,
        environ_overrides=remote,
    )
    assert still_limited.status_code == 429

    with db.db_conn() as conn:
        task_statuses = conn.execute(
            "SELECT status FROM annotation_tasks ORDER BY allocation_order"
        ).fetchall()
        auth_failures = conn.execute(
            """SELECT status, request FROM admin_actions
               WHERE action_type = 'admin_auth_failure'
               ORDER BY created_at"""
        ).fetchall()
        failed_revokes = conn.execute(
            """SELECT status, request FROM admin_actions
               WHERE action_type = 'revoke_annotations' AND status = 'failed'
               ORDER BY created_at"""
        ).fetchall()
    assert task_statuses == [("annotated",), ("annotated",)]
    assert len(auth_failures) == 3
    assert all(status == "failed" for status, _request in auth_failures)
    assert [
        request["details"]["rate_limited"]
        for _status, request in auth_failures
    ] == [False, True, True]
    assert all(
        request["details"]["purpose"] == "batch_revoke"
        for _status, request in auth_failures
    )
    assert len(failed_revokes) == 3
    assert all(status == "failed" for status, _request in failed_revokes)
    audit_payload = json.dumps(
        [request for _status, request in auth_failures + failed_revokes],
        ensure_ascii=False,
    )
    assert wrong_key not in audit_payload
    assert "[redacted]" in audit_payload


def test_admin_deactivate_requires_key_reauthentication_and_recycles_work(
        admin_client, database, seed_tasks):
    import db

    seed_tasks(2)
    alice = _make_user("alice")
    completed = _complete_next(alice, prefix="published")
    claimed = repo.claim(alice["id"])
    _, headers = _admin_login(admin_client)

    preview = admin_client.post(
        f"/api/admin/annotators/{alice['id']}/deactivate/preview",
        json={}, headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json["summary"]["published_to_revoke"] == 1
    assert preview.json["summary"]["assignments_to_release"] == 1

    body = {
        "operation_id": str(uuid.uuid4()),
        "reason": "offboarding",
        "confirm": True,
        "confirm_username": "alice",
    }
    no_key = admin_client.post(
        f"/api/admin/annotators/{alice['id']}/deactivate",
        json=body, headers=headers,
    )
    assert no_key.status_code in {400, 403}
    wrong = admin_client.post(
        f"/api/admin/annotators/{alice['id']}/deactivate",
        json={**body, "admin_key_confirmation": "wrong-key"}, headers=headers,
    )
    assert wrong.status_code == 403

    done = admin_client.post(
        f"/api/admin/annotators/{alice['id']}/deactivate",
        json={**body, "admin_key_confirmation": ADMIN_KEY}, headers=headers,
    )
    assert done.status_code == 200, done.json
    assert done.json["success"] is True
    with db.db_conn() as conn:
        status = conn.execute(
            "SELECT status FROM annotators WHERE id = %s", (alice["id"],),
        ).fetchone()[0]
        tasks = conn.execute(
            "SELECT id, status, current_published_version_id FROM annotation_tasks"
        ).fetchall()
        assignments = conn.execute(
            "SELECT count(*) FROM assignments WHERE user_id = %s", (alice["id"],),
        ).fetchone()[0]
    assert status == "deactivated"
    assert assignments == 0
    assert all(row[1] == "pending" and row[2] is None for row in tasks)
    assert {completed["task_id"], claimed["task_id"]} == {
        str(row[0]) for row in tasks
    }

    rejected_login = server.app.test_client().post(
        "/api/login", json={"username": "alice"},
    )
    assert rejected_login.status_code == 403
def test_admin_audit_api_records_revoke(admin_client, database, seed_tasks):
    seed_tasks(1)
    alice = _make_user("alice")
    completed = _complete_next(alice)
    _, headers = _admin_login(admin_client)
    revoked = admin_client.post(
        "/api/admin/annotations/revoke",
        json=_revoke_body(alice, completed),
        headers=headers,
    )
    assert revoked.status_code == 200

    audit = admin_client.get("/api/admin/audit?limit=10")
    assert audit.status_code == 200
    assert any(
        item["action_id"] == revoked.json["action_id"]
        and item["action_type"] == "revoke_annotations"
        for item in audit.json["items"]
    )
    assert audit.headers["Cache-Control"] == "no-store"
