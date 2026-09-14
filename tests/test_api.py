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
    login(client, "alice")
    conflict = other.post("/api/login", json={"username": "alice"})
    assert conflict.status_code == 409
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
