"""Annotation credit preserves history and counts new trainable segments."""

import json
import uuid

import psycopg
import pytest

import annotation_repository as repo
import db
from preprocess_store import store_preprocessed_task
from tests.test_cross_check_migration import database_at_007


def seed_audio(name="credit.wav"):
    with db.db_conn() as conn:
        return store_preprocessed_task(
            conn, rel_path=name, filename=name, folder="", duration=60,
            segments=[
                {"id": 1, "start": 5, "end": 15, "duration": 10,
                 "asr_text": "speech", "text": "", "exclude_from_training": False},
                {"id": 2, "start": 35, "end": 45, "duration": 10,
                 "asr_text": "speech", "text": "", "exclude_from_training": False},
            ],
        )["task_id"]


def submit(user, assignment, *, status="annotated", seconds=10, exclude=True):
    segments = [dict(segment, text="annotated speech")
                for segment in assignment["segments"]]
    segments[0].update(end=segments[0]["start"] + seconds, duration=999)
    segments[1]["exclude_from_training"] = exclude
    args = (user["fence"], assignment["lease_token"], assignment["revision"],
            status, ["noisy"] if status == "skipped" else [], segments,
            str(uuid.uuid4()), "duration-submit")
    result = repo.complete(*args)
    return result, args


def test_new_completion_credits_final_trainable_segments(client):
    seed_audio()
    user = repo.login("duration", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["fence"])
    _, args = submit(user, assignment)
    public = client.get("/api/leaderboard").json
    assert public["total_annotated_duration_seconds"] == 10
    assert public["leaderboard"][0]["duration_seconds"] == 10
    assert sum(day["duration_seconds"] for day in public["annotation_speed"]["days"]) == 10
    assert repo.completed_list(user["id"])["summary"]["duration_seconds"] == 10
    assert repo.completed_list(user["id"])["items"][0]["annotation_duration_seconds"] == 10
    detail = repo.completed_detail(user["id"], assignment["task_id"])
    assert (detail["duration"], detail["annotation_duration_seconds"]) == (60, 10)
    assert repo.admin_overview({})["totals"]["annotated_duration_seconds"] == 10
    assert repo.admin_annotator_detail(user["id"])["current"]["annotated_duration_seconds"] == 10
    assert repo.complete(*args)["idempotent_replay"]
    assert repo.dashboard()["stats"]["annotated_duration_seconds"] == 10
    with db.db_conn() as conn:
        assert conn.execute("SELECT duration FROM annotation_tasks").fetchone() == (60,)


def test_all_excluded_is_zero_and_draft_is_not_credited(client):
    seed_audio()
    user = repo.login("duration", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["fence"])
    segments = [dict(segment, text="", exclude_from_training=True)
                for segment in assignment["segments"]]
    repo.save_draft(user["fence"], assignment["lease_token"], 0, segments,
                    str(uuid.uuid4()), "save")
    assert repo.dashboard()["stats"]["annotated_duration_seconds"] == 0
    repo.complete(user["fence"], assignment["lease_token"], 1, "annotated", [], [],
                  str(uuid.uuid4()), "zero")
    assert client.get("/api/leaderboard").json["total_annotated_duration_seconds"] == 0


@pytest.fixture
def upgraded_legacy(database_at_007):
    with db.db_conn() as conn:
        conn.execute((db.MIGRATIONS_DIR / "008_cross_annotation_quality.sql").read_text())
        conn.execute("INSERT INTO schema_migrations (version, name) VALUES (8, 'cross_annotation_quality')")
        conn.execute(
            """INSERT INTO annotation_versions (
                   task_id, version_no, lifecycle, target_status, purpose,
                   submitted_by_user_id, submitted_at)
               VALUES (%s, 3, 'cross_check_submitted', 'annotated', 'cross_check', %s, now())""",
            (database_at_007["published"], database_at_007["bob"]),
        )
        conn.execute(
            """INSERT INTO annotation_versions (
                   task_id, version_no, lifecycle, target_status, purpose,
                   submitted_by_user_id, submitted_at)
               VALUES (%s, 4, 'superseded', 'annotated', 'annotation', %s, now())""",
            (database_at_007["published"], database_at_007["alice"]),
        )
        before = conn.execute(
            """SELECT v.submitted_by_user_id, sum(t.duration)
               FROM annotation_tasks t JOIN annotation_versions v
                 ON v.id = t.current_published_version_id
               WHERE t.status = 'annotated' GROUP BY v.submitted_by_user_id
               ORDER BY v.submitted_by_user_id"""
        ).fetchall()
        conn.commit()
        assert db.apply_migrations(conn) == [9]
        assert db.apply_migrations(conn) == []
        after = conn.execute(
            """SELECT v.submitted_by_user_id, sum(v.annotation_duration_seconds)
               FROM annotation_tasks t JOIN annotation_versions v
                 ON v.id = t.current_published_version_id
               WHERE t.status = 'annotated' GROUP BY v.submitted_by_user_id
               ORDER BY v.submitted_by_user_id"""
        ).fetchall()
        assert before == after
        conn.execute("SELECT setval('task_allocation_order_seq', (SELECT max(allocation_order) FROM annotation_tasks))")
    return database_at_007


def test_migration_preserves_history_and_only_new_work_uses_segments(upgraded_legacy):
    with db.db_conn() as conn:
        rows = conn.execute(
            """SELECT v.lifecycle, v.target_status, t.duration,
                      v.annotation_duration_seconds, v.annotation_duration_basis
               FROM annotation_versions v JOIN annotation_tasks t ON t.id = v.task_id"""
        ).fetchall()
        for lifecycle, status, raw, seconds, basis in rows:
            if lifecycle in ("baseline", "draft"):
                assert (seconds, basis) == (None, None)
            elif status == "annotated":
                assert (seconds, basis) == (raw, "legacy_audio_v1")
            else:
                assert (seconds, basis) == (0, "trainable_segments_v1")
        conn.execute("UPDATE annotation_tasks SET eligible = false")
    seed_audio()
    alice = repo.login("alice", str(uuid.uuid4()), 1800)
    submit(alice, repo.claim(alice["fence"]))
    assert repo.completed_list(alice["id"])["summary"]["duration_seconds"] == 23


def test_old_revisions_keep_credit_across_skipped_and_restore(upgraded_legacy):
    from tests.test_admin_repository import _admin_session, _revoke

    alice = repo.login("alice", str(uuid.uuid4()), 1800)
    task_id = str(upgraded_legacy["published"])
    for status, expected in [("annotated", 13), ("skipped", 0), ("annotated", 13)]:
        repo.reopen_completed(alice["fence"], task_id, str(uuid.uuid4()))
        draft = repo.get_assignment(alice["id"])
        segments = [dict(s, text="revised", end=3, duration=999,
                         exclude_from_training=False) for s in draft["segments"]]
        repo.complete(alice["fence"], draft["lease_token"], draft["revision"],
                      status, ["noisy"] if status == "skipped" else [], segments,
                      str(uuid.uuid4()), "legacy-revision")
        assert repo.completed_list(alice["id"])["summary"]["duration_seconds"] == expected
    admin = _admin_session()
    revoked = _revoke(admin, alice, [{"task_id": task_id,
                                    "expected_version_id": draft["version_id"]}])
    assert repo.completed_list(alice["id"])["summary"]["duration_seconds"] == 0
    repo.admin_restore(admin["id"], str(uuid.uuid4()), revoked["action_id"],
                       [task_id], reason="restore", confirm=True)
    assert repo.completed_list(alice["id"])["summary"]["duration_seconds"] == 13


def test_new_revision_replaces_credit_and_failed_submission_does_not_count(database):
    seed_audio()
    user = repo.login("duration", str(uuid.uuid4()), 1800)
    first = repo.claim(user["fence"])
    with pytest.raises(repo.ValidationError, match="without text"):
        repo.complete(user["fence"], first["lease_token"], 0, "annotated", [], [],
                      str(uuid.uuid4()), "invalid")
    with db.db_conn() as conn:
        assert conn.execute("SELECT annotation_duration_seconds FROM annotation_versions WHERE id = %s",
                            (first["version_id"],)).fetchone() == (None,)
    submit(user, first)
    repo.reopen_completed(user["fence"], first["task_id"], str(uuid.uuid4()))
    submit(user, repo.get_assignment(user["id"]), seconds=8)
    assert repo.dashboard()["stats"]["annotated_duration_seconds"] == 8


def test_legacy_skipped_first_annotation_uses_trainable_seconds(upgraded_legacy):
    eve = repo.login("eve", str(uuid.uuid4()), 1800)
    task_id = repo.completed_list(eve["id"])["items"][0]["task_id"]
    repo.reopen_completed(eve["fence"], task_id, str(uuid.uuid4()))
    draft = repo.get_assignment(eve["id"])
    segments = [dict(s, text="human", exclude_from_training=False) for s in draft["segments"]]
    repo.complete(eve["fence"], draft["lease_token"], 0, "annotated", [], segments,
                  str(uuid.uuid4()), "first-annotation")
    assert repo.completed_list(eve["id"])["summary"]["duration_seconds"] == 10


def test_task_pending_before_migration_uses_trainable_credit(upgraded_legacy):
    user = repo.login("after-migration", str(uuid.uuid4()), 1800)
    draft = repo.claim(user["fence"])
    assert (draft["filename"], draft["duration"]) == ("pending_exact.wav", 11)
    segments = [dict(s, text="human", exclude_from_training=False) for s in draft["segments"]]
    repo.complete(user["fence"], draft["lease_token"], 0, "annotated", [], segments,
                  str(uuid.uuid4()), "old-pending")
    assert repo.completed_list(user["id"])["summary"]["duration_seconds"] == 10


def test_revoked_legacy_task_reassigned_uses_new_credit(upgraded_legacy):
    from tests.test_admin_repository import _admin_session, _revoke

    alice = repo.login("alice", str(uuid.uuid4()), 1800)
    task_id = str(upgraded_legacy["published"])
    _revoke(_admin_session(), alice, [{"task_id": task_id,
                                      "expected_version_id": str(upgraded_legacy["published_version"])}])
    with db.db_conn() as conn:
        conn.execute("UPDATE annotation_tasks SET eligible = (id = %s)", (task_id,))
    new_user = repo.login("new-annotator", str(uuid.uuid4()), 1800)
    draft = repo.claim(new_user["fence"])
    assert draft["task_id"] == task_id
    segments = [dict(s, text="human", exclude_from_training=False) for s in draft["segments"]]
    repo.complete(new_user["fence"], draft["lease_token"], 0, "annotated", [], segments,
                  str(uuid.uuid4()), "reassigned")
    assert repo.completed_list(new_user["id"])["summary"]["duration_seconds"] == 10


@pytest.mark.parametrize("decision,expected", [("original", 60), ("secondary", 10), ("edited", 8)])
def test_cross_check_uses_the_selected_version_credit(client, decision, expected):
    from annotation_quality.contracts import CrossCheckDecisionCommand
    from annotation_quality.service import decide_cross_check
    from tests.test_admin_repository import _admin_session
    from tests.test_cross_check_claim import enable_cross_check

    seed_audio()
    alice = repo.login("alice", str(uuid.uuid4()), 1800)
    original = repo.claim(alice["fence"])
    submit(alice, original, exclude=False)
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions SET annotation_duration_seconds = 60,
                      annotation_duration_basis = 'legacy_audio_v1' WHERE id = %s""",
            (original["version_id"],),
        )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = repo.login("bob", str(uuid.uuid4()), 1800)
    secondary = repo.claim(bob["fence"])
    result, _ = submit(bob, secondary)
    assert result["cross_check"]["state"] == "awaiting_review"
    assert repo.dashboard()["stats"]["annotated_duration_seconds"] == 60
    assert repo.completed_list(bob["id"], include_submissions=True)["summary"]["duration_seconds"] == 10
    round_id = result["cross_check"]["round_id"]
    with db.db_conn() as conn:
        revision = conn.execute("SELECT revision FROM cross_check_rounds WHERE id = %s",
                                (round_id,)).fetchone()[0]
    body = {
        "operation_id": str(uuid.uuid4()), "expected_revision": revision,
        "expected_original_version_id": original["version_id"],
        "expected_secondary_version_id": secondary["version_id"],
        "decision": decision, "reason": "check accounting",
    }
    if decision == "edited":
        segments = [dict(s, text="edited", exclude_from_training=i == 1)
                    for i, s in enumerate(original["segments"])]
        segments[0].update(end=13, duration=8)
        body.update(base="original", target_status="annotated", segments=segments)
    decide_cross_check(_admin_session()["id"], round_id, CrossCheckDecisionCommand.model_validate(body))
    assert repo.dashboard()["stats"]["annotated_duration_seconds"] == expected


def test_invalid_import_snapshot_is_reported_in_manifest(tmp_path):
    import manage_state as ms
    from tests.test_migration import write_legacy

    write_legacy(tmp_path, "invalid.wav", status="annotated", user="alice")
    path = tmp_path / "annotations" / "invalid.json"
    data = json.loads(path.read_text())
    data.update(annotation_duration_seconds=10)
    path.write_text(json.dumps(data))
    manifest = ms.build_manifest(tmp_path / "annotations", tmp_path / "audio")
    assert manifest["errors"][0]["type"] == "invalid_annotation"


@pytest.mark.parametrize("seconds,basis", [(None, None), (1, None), (None, "legacy_audio_v1"),
                                        (-1, "legacy_audio_v1"), (float("nan"), "legacy_audio_v1")])
def test_database_rejects_incomplete_or_invalid_completed_credit(database, seconds, basis):
    task_id = seed_audio()
    with pytest.raises(psycopg.errors.CheckViolation), db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions SET lifecycle = 'published',
                      annotation_duration_seconds = %s, annotation_duration_basis = %s
               WHERE task_id = %s AND lifecycle = 'draft'""", (seconds, basis, task_id),
        )


def test_json_export_import_preserves_new_credit(database, tmp_path):
    import manage_state as ms

    seed_audio()
    user = repo.login("duration", str(uuid.uuid4()), 1800)
    submit(user, repo.claim(user["fence"]))
    output = tmp_path / "export"
    ms.export_json(output)
    data = json.loads((output / "credit.json").read_text())
    assert (data["duration"], data["annotation_duration_seconds"], data["annotation_duration_basis"]) == (
        60, 10, "trainable_segments_v1")
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "credit.wav").write_bytes(b"test")
    manifest = ms.build_manifest(output, audio)
    assert manifest["summary"]["users"]["duration"]["duration_seconds"] == 10
    item = manifest["items"][0]
    # A second path exercises the real importer without replacing the source task.
    item = {**item, "rel_path": "restored.wav", "order": 100}
    with db.db_conn() as conn, conn.cursor() as cur:
        ms.insert_task(cur, item, data, ms.normalize_legacy(data, "restored.wav"), {})
    assert repo.dashboard()["stats"]["annotated_duration_seconds"] == 20
