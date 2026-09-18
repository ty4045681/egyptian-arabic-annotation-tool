from __future__ import annotations

import uuid
from pathlib import Path

import server


def login(client, username="alice"):
    response = client.post("/api/login", json={"username": username})
    assert response.status_code == 200, response.json
    return response.json


def segment_payload(assignment, prefix="text"):
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


def test_assignment_is_explicit_and_survives_logout(client, seed_tasks):
    seed_tasks(2)
    login(client)
    current = client.get("/api/assignment")
    assert current.status_code == 200
    assert current.json["assigned"] is False
    assert current.json["pool"]["available"] == 2

    claim = client.post("/api/assignment/claim", json={})
    assert claim.status_code == 200
    task_id = claim.json["task_id"]
    token = claim.json["lease_token"]
    assert claim.json["resumed"] is False

    assert client.post("/api/logout", json={}).status_code == 200
    login(client)
    resumed = client.get("/api/assignment")
    assert resumed.status_code == 200
    assert resumed.json["assigned"] is True
    assert resumed.json["task_id"] == task_id
    assert resumed.json["lease_token"] == token
    assert resumed.json["resumed"] is True


def test_assignment_pool_is_scoped_to_current_user(client, seed_tasks):
    import annotation_repository as repo
    import db

    task_id = seed_tasks(1)[0]
    with db.db_tx() as conn, conn.cursor() as cur:
        alice = repo.ensure_user(cur, "alice")
        cur.execute(
            "UPDATE annotation_tasks SET reserved_for_user_id = %s WHERE id = %s",
            (alice["id"], task_id),
        )

    login(client, "bob")
    current = client.get("/api/assignment")
    assert current.status_code == 200
    assert current.json["pool"]["available"] == 0
    assert current.json["pool"]["reason"] == "temporarily_all_assigned"

    claim = client.post("/api/assignment/claim", json={})
    assert claim.status_code == 409
    assert claim.json["pool"]["available"] == 0
    assert claim.json["pool"]["reason"] == "temporarily_all_assigned"


def test_claim_reports_temporarily_locked_pool(client, seed_tasks):
    import db

    task_id = seed_tasks(1)[0]
    login(client, "alice")
    with db.db_conn() as blocker:
        blocker.execute(
            "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
            (task_id,),
        )
        claim = client.post("/api/assignment/claim", json={})

    assert claim.status_code == 409
    assert claim.json["pool"]["reason"] == "temporarily_busy"
    assert claim.json["error"] == "Task pool is busy; retry claim"


def test_save_complete_history_and_completed_api(client, seed_tasks):
    seed_tasks(1)
    login(client)
    assignment = client.post("/api/assignment/claim", json={}).json
    segments = segment_payload(assignment)

    save_body = {
        "lease_token": assignment["lease_token"],
        "expected_revision": 0,
        "operation_id": str(uuid.uuid4()),
        "segments": segments,
    }
    saved = client.patch("/api/assignment/current", json=save_body)
    assert saved.status_code == 200
    assert saved.json["revision"] == 1
    replay = client.patch("/api/assignment/current", json=save_body)
    assert replay.status_code == 200
    assert replay.json == saved.json

    complete_body = {
        "lease_token": assignment["lease_token"],
        "expected_revision": 1,
        "target_status": "annotated",
        "skip_reasons": [],
        "operation_id": str(uuid.uuid4()),
        "segments": segments,
    }
    completed = client.post("/api/assignment/current/complete", json=complete_body)
    assert completed.status_code == 200
    assert completed.json["status"] == "annotated"
    replay = client.post("/api/assignment/current/complete", json=complete_body)
    assert replay.status_code == 200
    assert replay.json == completed.json

    history = client.get("/api/history/recent")
    assert history.status_code == 200
    assert history.json["items"][0]["task_id"] == assignment["task_id"]

    records = client.get("/api/completed")
    assert records.status_code == 200
    assert records.json["summary"] == {
        "annotated": 1,
        "skipped": 0,
        "duration_seconds": 10.0,
    }
    assert records.json["has_assignment"] is False
    assert records.json["items"][0]["task_id"] == assignment["task_id"]

    detail = client.get(f"/api/completed/{assignment['task_id']}")
    assert detail.status_code == 200
    assert detail.json["segments"][0]["text"] == "text 1"


def test_completed_privacy_and_reopen_conflict(client, seed_tasks):
    seed_tasks(2)
    login(client, "alice")
    first = client.post("/api/assignment/claim", json={}).json
    client.post(
        "/api/assignment/current/complete",
        json={
            "lease_token": first["lease_token"],
            "expected_revision": 0,
            "target_status": "annotated",
            "skip_reasons": [],
            "operation_id": str(uuid.uuid4()),
            "segments": segment_payload(first, "alice"),
        },
    )
    second = client.post("/api/assignment/claim", json={}).json
    blocked = client.post(
        f"/api/completed/{first['task_id']}/reopen",
        json={"operation_id": str(uuid.uuid4())},
    )
    assert blocked.status_code == 409
    assert client.get("/api/assignment").json["task_id"] == second["task_id"]

    client.post(
        "/api/assignment/current/abandon",
        json={"lease_token": second["lease_token"],
              "operation_id": str(uuid.uuid4()), "confirm": True},
    )
    reopened = client.post(
        f"/api/completed/{first['task_id']}/reopen",
        json={"operation_id": str(uuid.uuid4())},
    )
    assert reopened.status_code == 200
    assert reopened.json["mode"] == "revision"
    assert client.get("/api/assignment").json["task_id"] == first["task_id"]

    client.post("/api/logout", json={})
    login(client, "bob")
    assert client.get("/api/completed").json["items"] == []
    assert client.get(f"/api/completed/{first['task_id']}").status_code == 403


def test_same_name_login_conflict(client, database):
    other = server.app.test_client()
    first = login(client, "alice")
    assert first["session"]["mode"] in {"login", "resume"}
    conflict = other.post("/api/login", json={"username": "alice"})
    assert conflict.status_code == 409
    body = conflict.json
    assert body["code"] == "session_active"
    assert body["takeover_token"]
    assert body["takeover_token_expires_in"] == 60
    assert "sid" not in body
    assert "session_id" not in str(body)
    assert set(body["active_session"]) == {"last_seen_at", "login_time"}
    client.post("/api/logout", json={})
    assert other.post("/api/login", json={"username": "alice"}).status_code == 200


def test_audio_range_and_object_authorization(client, database, tmp_path):
    import db
    from preprocess_store import store_preprocessed_task

    audio_root = Path(server.app.config["AUDIO_DIR"])
    (audio_root / "folder").mkdir(parents=True)
    payload = bytes(range(256)) * 8
    (audio_root / "folder" / "sample.wav").write_bytes(payload)
    with db.db_conn() as conn:
        stored = store_preprocessed_task(
            conn, rel_path="folder/sample.wav", filename="sample.wav",
            folder="folder", duration=10.0,
            segments=[{"id": 1, "start": 0.0, "end": 10.0,
                       "duration": 10.0, "asr_text": "asr", "text": "",
                       "exclude_from_training": False}],
            waveform_payload=b"\x01\x00\xff\xff",
        )
    login(client, "alice")
    assignment = client.post("/api/assignment/claim", json={}).json
    assert assignment["task_id"] == stored["task_id"]

    full = client.get(f"/api/audio/{assignment['task_id']}")
    assert full.status_code == 200
    assert full.data == payload
    assert full.headers["Accept-Ranges"] == "bytes"
    assert "private" in full.headers["Cache-Control"]

    partial = client.get(
        f"/api/audio/{assignment['task_id']}",
        headers={"Range": "bytes=10-19"},
    )
    assert partial.status_code == 206
    assert partial.data == payload[10:20]
    assert partial.headers["Content-Range"].startswith("bytes 10-19/")

    bob = server.app.test_client()
    login(bob, "bob")
    assert bob.get(f"/api/audio/{assignment['task_id']}").status_code == 403
    assert bob.get(f"/api/waveform/{assignment['task_id']}").status_code == 403


def test_health_fails_closed_when_schema_is_stale(client, database):
    import db
    with db.db_conn() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 1")
        conn.commit()
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json["ok"] is False


def test_pages_require_login_and_completed_route(client, database):
    assert client.get("/").status_code == 302
    assert client.get("/completed.html").status_code == 302
    assert client.get("/login.html").status_code == 200
    login(client)
    assert client.get("/").status_code == 200
    assert client.get("/completed.html").status_code == 200
    assert client.get("/login.html").status_code == 302


def test_login_resume_and_stale_takeover_contract(client, database):
    first = login(client, "alice")
    assert first["success"] is True
    assert first["session"]["mode"] == "login"
    assert first["session"]["idle_expires_at"]
    assert first["session"]["absolute_expires_at"]
    resumed = login(client, "alice")
    assert resumed["session"]["mode"] == "resume"

    other = server.app.test_client()
    import db
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE active_sessions SET last_seen_at = now() - interval '200 seconds'"
        )
        conn.commit()
    stale = other.post("/api/login", json={"username": "alice"})
    assert stale.status_code == 200
    assert stale.json["session"]["mode"] == "stale_takeover"
    rejected = client.get("/api/current-user")
    assert rejected.status_code == 401
    assert rejected.json["code"] == "session_replaced"
    assert "reason=session_replaced" in rejected.json["redirect"]


def test_forced_takeover_api_and_old_client_replaced(client, seed_tasks):
    seed_tasks(1)
    login(client, "alice")
    claimed = client.post("/api/assignment/claim", json={})
    assert claimed.status_code == 200
    task_id = claimed.json["task_id"]
    token = claimed.json["lease_token"]
    other = server.app.test_client()
    conflict = other.post("/api/login", json={"username": "alice"})
    assert conflict.status_code == 409
    taken = other.post("/api/login/takeover", json={
        "username": "alice",
        "takeover_token": conflict.json["takeover_token"],
    })
    assert taken.status_code == 200
    assert taken.json["session"]["mode"] == "forced_takeover"
    replaced = client.get("/api/assignment")
    assert replaced.status_code == 401
    assert replaced.json["code"] == "session_replaced"
    resumed = other.get("/api/assignment")
    assert resumed.status_code == 200
    assert resumed.json["task_id"] == task_id
    assert resumed.json["lease_token"] == token
    replay = other.post("/api/login/takeover", json={
        "username": "alice",
        "takeover_token": conflict.json["takeover_token"],
    })
    assert replay.status_code == 409
    assert replay.json["code"] == "session_changed"


def test_takeover_token_tamper_user_mismatch_and_expiry(client, database, monkeypatch):
    login(client, "alice")
    other = server.app.test_client()
    conflict = other.post("/api/login", json={"username": "alice"})
    token = conflict.json["takeover_token"]
    tampered = other.post("/api/login/takeover", json={
        "username": "alice",
        "takeover_token": token + "ab",
    })
    assert tampered.status_code == 400
    mismatch = other.post("/api/login/takeover", json={
        "username": "bob",
        "takeover_token": token,
    })
    assert mismatch.status_code == 400
    import time
    monkeypatch.setitem(server.app.config, "SESSION_TAKEOVER_TOKEN_SECONDS", 1)
    time.sleep(2.2)
    response = other.post("/api/login/takeover", json={
        "username": "alice",
        "takeover_token": token,
    })
    assert response.status_code == 400
    assert response.json["code"] == "takeover_token_expired"


def test_idle_and_absolute_api_codes(client, database):
    login(client, "alice")
    import db
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE active_sessions
                  SET expires_at = now() - interval '1 second',
                      absolute_expires_at = now() + interval '20 hours'"""
        )
        conn.commit()
    idle = client.get("/api/current-user")
    assert idle.status_code == 401
    assert idle.json["code"] == "idle_timeout"
    login(client, "alice")
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE active_sessions
                  SET absolute_expires_at = now() - interval '1 second',
                      expires_at = now() - interval '1 second'"""
        )
        conn.commit()
    absolute = client.get("/api/current-user")
    assert absolute.status_code == 401
    assert absolute.json["code"] == "absolute_timeout"
    page = client.get("/")
    assert page.status_code == 302
    assert "reason=absolute_timeout" in page.headers["Location"]


def test_get_heartbeat_is_presence_only_and_post_can_extend_idle(client, database):
    login(client, "alice")
    import db
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE active_sessions
                  SET last_seen_at = now() - interval '20 seconds',
                      last_activity_at = now() - interval '45 seconds',
                      expires_at = now() + interval '5 minutes'"""
        )
        before = conn.execute(
            "SELECT expires_at FROM active_sessions"
        ).fetchone()[0]
        conn.commit()
    legacy = client.get("/api/heartbeat")
    assert legacy.status_code == 200
    assert legacy.headers.get("Deprecation") == "true"
    with db.db_conn() as conn:
        after_get = conn.execute("SELECT expires_at FROM active_sessions").fetchone()[0]
    assert after_get == before
    posted = client.post("/api/session/heartbeat", json={"activity": True})
    assert posted.status_code == 200
    assert posted.json["idle_expires_at"]
    assert posted.json["absolute_expires_at"]
    with db.db_conn() as conn:
        after_post = conn.execute("SELECT expires_at FROM active_sessions").fetchone()[0]
    assert after_post > before


def test_current_user_is_client_config_source(client, database):
    login(client, "alice")
    me = client.get("/api/current-user")
    assert me.status_code == 200
    session = me.json["session"]
    assert session["heartbeat_seconds"] == 30
    assert session["idle_warning_seconds"] == 120
    assert session["offline_draft_retention_days"] == 7
    assert "sid" not in session
    assert "generation" not in session
    assert "takeover" not in str(session).lower()


def test_query_validation_and_abandon_returns_pool(client, seed_tasks):
    seed_tasks(1)
    login(client)
    assignment = client.post("/api/assignment/claim", json={}).json
    assert client.get("/api/completed?limit=abc").status_code == 400
    assert client.get("/api/completed?cursor=not-a-cursor").status_code == 400
    assert client.get("/api/history/recent?before=nope").status_code == 400
    abandoned = client.post(
        "/api/assignment/current/abandon",
        json={
            "lease_token": assignment["lease_token"],
            "operation_id": str(uuid.uuid4()),
            "confirm": True,
        },
    )
    assert abandoned.status_code == 200
    assert abandoned.json["pool"]["available"] == 1
    assert client.get("/api/assignment").json["assigned"] is False


def test_scenes_api_exposes_review_taxonomy_separately_from_claim_scenes(
        client, database):
    import db

    login(client, "restricted-reviewer")
    with db.db_conn() as conn:
        uid = conn.execute(
            "SELECT id FROM annotators WHERE username = %s",
            ("restricted-reviewer",),
        ).fetchone()[0]
        conn.execute(
            "UPDATE annotator_scene_scopes SET mode = 'restricted' WHERE user_id = %s",
            (uid,),
        )
        conn.execute(
            "INSERT INTO annotator_scene_access(user_id, scene_code) VALUES (%s, 'airport')",
            (uid,),
        )
    response = client.get("/api/scenes")
    assert response.status_code == 200
    body = response.json
    assert [item["code"] for item in body["scenes"]] == ["airport"]
    assert body["claim_scenes"] == body["scenes"]
    taxonomy = {item["code"] for item in body["review_taxonomy"]}
    assert "airport" in taxonomy
    assert "shopping" in taxonomy
    assert body["features"]["scene_scope_enforced"] is True
    assert "claim_policy" in body["features"]


def test_scenes_api_requires_login(client, database):
    response = client.get("/api/scenes")
    assert response.status_code == 401
