from __future__ import annotations

import concurrent.futures
import hashlib
import threading
import uuid

import pytest

import annotation_repository as repo


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


def _make_user(username: str) -> tuple[dict, str]:
    session_id = str(uuid.uuid4())
    return repo.login(username, session_id, 1800), session_id


def _complete_next(user: dict, *, prefix: str = "human",
                   status: str = "annotated") -> dict:
    assignment = repo.claim(user["id"])
    result = repo.complete(
        user["id"],
        assignment["lease_token"],
        assignment["revision"],
        status,
        ["noisy"] if status == "skipped" else [],
        _segments(assignment, prefix) if status == "annotated" else [],
        str(uuid.uuid4()),
        f"complete-{uuid.uuid4()}",
    )
    return {**result, "version_id": assignment["version_id"]}


def _admin_session() -> dict:
    nonce = uuid.uuid4().hex
    return repo.create_admin_session(
        key_id="primary",
        token_digest=hashlib.sha256(f"token-{nonce}".encode()).hexdigest(),
        csrf_digest=hashlib.sha256(f"csrf-{nonce}".encode()).hexdigest(),
        idle_seconds=1800,
        absolute_seconds=28800,
        ip_hash="test-ip",
        user_agent="pytest",
    )


def _revoke_items(*completed: dict) -> list[dict]:
    return [
        {
            "task_id": item["task_id"],
            "expected_version_id": item["version_id"],
        }
        for item in completed
    ]


def _revoke(admin: dict, annotator: dict, items: list[dict], *,
            operation_id: str | None = None, reason: str = "quality failure") -> dict:
    return repo.admin_revoke(
        admin_session_id=admin["id"],
        operation_id=operation_id or str(uuid.uuid4()),
        annotator_id=annotator["id"],
        items=items,
        reason=reason,
        block_reclaim=True,
        confirm=True,
        release_conflicts=False,
    )


def test_admin_session_digest_expiry_and_revocation(database):
    import db

    token_digest = hashlib.sha256(b"admin-session-token").hexdigest()
    csrf_digest = hashlib.sha256(b"admin-csrf-token").hexdigest()
    created = repo.create_admin_session(
        "primary", token_digest, csrf_digest, 1800, 28800,
        ip_hash="ip-hash", user_agent="pytest",
    )

    principal = repo.validate_admin_session(token_digest, 1800)
    assert principal is not None
    assert str(principal["id"]) == str(created["id"])
    assert principal["key_id"] == "primary"
    assert repo.validate_admin_session("0" * 64, 1800) is None

    with db.db_conn() as conn:
        stored = conn.execute(
            "SELECT token_digest, csrf_digest FROM admin_sessions WHERE id = %s",
            (created["id"],),
        ).fetchone()
        assert stored == (token_digest, csrf_digest)
        assert "admin-session-token" not in repr(stored)
        conn.execute(
            "UPDATE admin_sessions SET idle_expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (created["id"],),
        )
        conn.commit()
    assert repo.validate_admin_session(token_digest, 1800) is None

    second_digest = hashlib.sha256(b"second-token").hexdigest()
    repo.create_admin_session("primary", second_digest, csrf_digest, 1800, 28800)
    assert repo.revoke_admin_session(second_digest) is True
    assert repo.validate_admin_session(second_digest, 1800) is None
    assert repo.revoke_admin_session(second_digest) is False


def test_admin_overview_and_annotator_keyset_pagination(database, seed_tasks):
    seed_tasks(4)
    alice, _ = _make_user("alice")
    bob, _ = _make_user("bob")
    carol, _ = _make_user("carol")
    _complete_next(alice, prefix="alice")
    _complete_next(bob, prefix="bob", status="skipped")
    _complete_next(carol, prefix="carol")

    overview = repo.admin_overview({})
    assert overview["totals"]["total_audio_count"] == 4
    assert overview["totals"]["annotated_count"] == 2
    assert overview["totals"]["skipped_count"] == 1
    assert overview["totals"]["pending_count"] == 1
    assert overview["totals"]["total_audio_duration_seconds"] == 40.0
    assert overview["pending"]["available_count"] == 1

    series = repo.admin_timeseries({"bucket": "day", "timezone": "UTC"})
    assert sum(item["annotated"] for item in series["items"]) == 2
    assert sum(item["skipped"] for item in series["items"]) == 1
    assert sum(item["revoked"] for item in series["items"]) == 0

    first = repo.admin_annotators({}, limit=2)
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    second = repo.admin_annotators({}, limit=2, cursor=first["next_cursor"])
    all_ids = [item["id"] for item in first["items"] + second["items"]]
    assert len(all_ids) == len(set(all_ids)) == 3

    detail = repo.admin_annotator_detail(alice["id"], {})
    assert detail["username"] == "alice"
    assert detail["current"]["annotated_count"] == 1
    assert detail["history"]["completed_count"] == 1

    annotations = repo.admin_annotator_annotations(alice["id"], {}, limit=1)
    assert len(annotations["items"]) == 1
    assert annotations["items"][0]["annotator"]["username"] == "alice"


def test_admin_quality_filters_search_annotator_and_signal(database, seed_tasks):
    import db

    seed_tasks(3)
    alice, _ = _make_user("alice")
    bob, _ = _make_user("bob")
    completed = _complete_next(alice, prefix="alice")
    stale_assignment = repo.claim(bob["id"])
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE assignments SET last_activity_at = now() - interval '5 hours' "
            "WHERE task_id = %s",
            (stale_assignment["task_id"],),
        )
        conn.commit()

    fast = repo.admin_quality({"signal": "unusually_fast"})
    assert fast["items"]
    assert all(item["type"] == "unusually_fast" for item in fast["items"])
    assert completed["task_id"] in {item["task_id"] for item in fast["items"]}

    stale = repo.admin_quality({"signal": "stale_assignment"})
    assert [item["task_id"] for item in stale["items"]] == [
        stale_assignment["task_id"]
    ]

    combined = repo.admin_quality({
        "q": stale_assignment["filename"],
        "annotator_id": bob["id"],
        "signal": "stale_assignment",
    })
    assert [item["task_id"] for item in combined["items"]] == [
        stale_assignment["task_id"]
    ]
    assert combined["items"][0]["annotator_id"] == str(bob["id"])

    alice_only = repo.admin_quality({"annotator_id": alice["id"]})
    assert alice_only["items"]
    assert all(
        item["annotator_id"] == str(alice["id"])
        for item in alice_only["items"]
    )
    assert repo.admin_quality({"q": "does-not-exist"})["items"] == []
    with pytest.raises(repo.ValidationError, match="signal must be"):
        repo.admin_quality({"signal": "unknown"})


def test_revoke_preview_is_read_only_and_reports_conflicts(database, seed_tasks):
    import db

    task_id = seed_tasks(1)[0]
    alice, _ = _make_user("alice")
    completed = _complete_next(alice, prefix="secret")
    items = _revoke_items(completed)

    preview = repo.admin_revoke_preview(alice["id"], items, block_reclaim=True)
    assert preview["summary"]["requested"] == 1
    assert preview["summary"]["revokeable"] == 1
    assert preview["summary"]["conflicts"] == 0
    assert preview["summary"]["duration_seconds"] == 10.0

    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT status, current_published_version_id FROM annotation_tasks "
            "WHERE id = %s",
            (task_id,),
        ).fetchone()
        lifecycle = conn.execute(
            "SELECT lifecycle FROM annotation_versions WHERE id = %s",
            (completed["version_id"],),
        ).fetchone()[0]
    assert row[0] == "annotated"
    assert str(row[1]) == completed["version_id"]
    assert lifecycle == "published"

    stale = repo.admin_revoke_preview(
        alice["id"],
        [{"task_id": task_id, "expected_version_id": str(uuid.uuid4())}],
        block_reclaim=True,
    )
    assert stale["summary"]["revokeable"] == 0
    assert stale["summary"]["conflicts"] == 1


def test_single_revoke_is_idempotent_audited_and_clones_clean_baseline(
        database, seed_tasks):
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    completed = _complete_next(alice, prefix="must-not-leak")
    admin = _admin_session()
    operation_id = str(uuid.uuid4())
    items = _revoke_items(completed)

    first = _revoke(admin, alice, items, operation_id=operation_id)
    replay = _revoke(admin, alice, items, operation_id=operation_id)
    assert first["action_id"] == replay["action_id"]
    assert replay["idempotent_replay"] is True
    assert first["summary"]["revoked"] == 1

    with db.db_conn() as conn:
        task = conn.execute(
            """SELECT status, current_published_version_id, baseline_version_id,
                      baseline_quality
               FROM annotation_tasks WHERE id = %s""",
            (completed["task_id"],),
        ).fetchone()
        old = conn.execute(
            """SELECT lifecycle, revoked_reason, revoked_by_admin_action_id
               FROM annotation_versions WHERE id = %s""",
            (completed["version_id"],),
        ).fetchone()
        old_segments = conn.execute(
            """SELECT text, exclude_from_training FROM segments
               WHERE version_id = %s ORDER BY segment_id""",
            (completed["version_id"],),
        ).fetchall()
        clean = conn.execute(
            """SELECT v.id, v.skip_reasons, s.asr_text, s.text,
                      s.exclude_from_training
               FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.task_id = %s AND v.lifecycle = 'draft'
               ORDER BY s.segment_id""",
            (completed["task_id"],),
        ).fetchall()
        blocks = conn.execute(
            """SELECT count(*) FROM task_annotator_blocks
               WHERE task_id = %s AND user_id = %s""",
            (completed["task_id"], alice["id"]),
        ).fetchone()[0]
        audit = conn.execute(
            """SELECT count(*) FROM admin_actions
               WHERE operation_id = %s""",
            (operation_id,),
        ).fetchone()[0]
        audit_items = conn.execute(
            """SELECT count(*) FROM admin_action_items
               WHERE admin_action_id = %s""",
            (first["action_id"],),
        ).fetchone()[0]
        events = conn.execute(
            """SELECT count(*) FROM annotation_events
               WHERE admin_action_id = %s AND event_type = 'revoked_admin'""",
            (first["action_id"],),
        ).fetchone()[0]

    assert task[0] == "pending" and task[1] is None
    assert task[2] is not None and task[3] in {"exact", "reconstructed", "reprocessed"}
    assert old[0] == "revoked" and old[1] == "quality failure"
    assert str(old[2]) == first["action_id"]
    assert [row[0] for row in old_segments] == [
        "must-not-leak 1", "must-not-leak 2",
    ]
    assert clean
    assert all(row[1] == [] for row in clean)
    assert [row[2] for row in clean] == ["asr one", "asr two"]
    assert all(row[3] == "" and row[4] is False for row in clean)
    assert blocks == audit == audit_items == events == 1

    with pytest.raises(repo.ConflictError):
        _revoke(
            admin, alice, items, operation_id=operation_id,
            reason="same operation, different request",
        )


def test_batch_revoke_conflict_rolls_back_every_item(database, seed_tasks):
    import db

    seed_tasks(2)
    alice, _ = _make_user("alice")
    first = _complete_next(alice, prefix="first")
    second = _complete_next(alice, prefix="second")
    admin = _admin_session()
    operation_id = str(uuid.uuid4())
    items = _revoke_items(first, second)
    items[1]["expected_version_id"] = str(uuid.uuid4())

    with pytest.raises(repo.ConflictError):
        _revoke(admin, alice, items, operation_id=operation_id)

    with db.db_conn() as conn:
        tasks = conn.execute(
            """SELECT status, current_published_version_id
               FROM annotation_tasks WHERE id = ANY(%s)
               ORDER BY id""",
            ([first["task_id"], second["task_id"]],),
        ).fetchall()
        lifecycle = conn.execute(
            """SELECT lifecycle FROM annotation_versions WHERE id = ANY(%s)
               ORDER BY id""",
            ([first["version_id"], second["version_id"]],),
        ).fetchall()
        action_count = conn.execute(
            "SELECT count(*) FROM admin_actions WHERE operation_id = %s",
            (operation_id,),
        ).fetchone()[0]
    assert len(tasks) == 2
    assert all(row[0] == "annotated" and row[1] is not None for row in tasks)
    assert lifecycle == [("published",), ("published",)]
    assert action_count == 0


def test_batch_revoke_success_replays_without_duplicate_items(database, seed_tasks):
    import db

    seed_tasks(2)
    alice, _ = _make_user("alice")
    first = _complete_next(alice, prefix="first")
    second = _complete_next(alice, prefix="second")
    admin = _admin_session()
    operation_id = str(uuid.uuid4())
    items = _revoke_items(first, second)

    result = _revoke(admin, alice, items, operation_id=operation_id)
    replay = _revoke(admin, alice, items, operation_id=operation_id)
    assert result["summary"]["requested"] == 2
    assert result["summary"]["revoked"] == 2
    assert replay["action_id"] == result["action_id"]
    assert replay["idempotent_replay"] is True

    with db.db_conn() as conn:
        lifecycles = conn.execute(
            """SELECT lifecycle, count(*) FROM annotation_versions
               WHERE id = ANY(%s) GROUP BY lifecycle""",
            ([first["version_id"], second["version_id"]],),
        ).fetchall()
        action_items = conn.execute(
            """SELECT count(*) FROM admin_action_items
               WHERE admin_action_id = %s""",
            (result["action_id"],),
        ).fetchone()[0]
        events = conn.execute(
            """SELECT count(*) FROM annotation_events
               WHERE admin_action_id = %s AND event_type = 'revoked_admin'""",
            (result["action_id"],),
        ).fetchone()[0]
        clean_drafts = conn.execute(
            """SELECT count(*) FROM annotation_versions
               WHERE task_id = ANY(%s) AND lifecycle = 'draft'""",
            ([first["task_id"], second["task_id"]],),
        ).fetchone()[0]
    assert lifecycles == [("revoked", 2)]
    assert action_items == events == clean_drafts == 2


def test_revoked_task_blocks_submitter_but_is_claimable_by_another_user(
        database, seed_tasks):
    seed_tasks(1)
    alice, _ = _make_user("alice")
    completed = _complete_next(alice, prefix="alice-private")
    _revoke(_admin_session(), alice, _revoke_items(completed))

    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(alice["id"])

    bob, _ = _make_user("bob")
    claimed = repo.claim(bob["id"])
    assert claimed["task_id"] == completed["task_id"]
    assert [segment["text"] for segment in claimed["segments"]] == ["", ""]
    assert all(not segment["exclude_from_training"] for segment in claimed["segments"])


def test_deactivate_revokes_current_work_releases_session_and_is_audited(
        database, seed_tasks):
    import db

    seed_tasks(2)
    alice, alice_sid = _make_user("alice")
    completed = _complete_next(alice, prefix="published-secret")
    in_progress = repo.claim(alice["id"])
    repo.save_draft(
        alice["id"], in_progress["lease_token"], in_progress["revision"],
        _segments(in_progress, "draft-secret"), str(uuid.uuid4()), "save-draft",
    )
    admin = _admin_session()

    preview = repo.admin_deactivate_preview(alice["id"])
    assert preview["annotator"]["username"] == "alice"
    assert preview["summary"]["published_to_revoke"] == 1
    assert preview["summary"]["assignments_to_release"] == 1

    result = repo.admin_deactivate(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=alice["id"],
        reason="offboarding",
        confirm=True,
        confirm_username="alice",
    )
    assert result["success"] is True

    with db.db_conn() as conn:
        user = conn.execute(
            """SELECT status, deactivated_at, deactivated_reason
               FROM annotators WHERE id = %s""",
            (alice["id"],),
        ).fetchone()
        assignment_count = conn.execute(
            "SELECT count(*) FROM assignments WHERE user_id = %s",
            (alice["id"],),
        ).fetchone()[0]
        tasks = conn.execute(
            """SELECT id, status, current_published_version_id
               FROM annotation_tasks ORDER BY allocation_order"""
        ).fetchall()
        leaked = conn.execute(
            """SELECT count(*)
               FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.task_id = %s AND v.lifecycle = 'draft'
                 AND (s.text <> '' OR s.exclude_from_training)""",
            (in_progress["task_id"],),
        ).fetchone()[0]
        audit = conn.execute(
            """SELECT action_type, reason FROM admin_actions
               WHERE id = %s""",
            (result["action_id"],),
        ).fetchone()

    assert user[0] == "deactivated" and user[1] is not None
    assert user[2] == "offboarding"
    assert assignment_count == leaked == 0
    assert all(row[1] == "pending" and row[2] is None for row in tasks)
    assert audit == ("deactivate_annotator", "offboarding")
    assert repo.validate_session("alice", alice_sid) is None
    with pytest.raises(repo.ForbiddenError):
        repo.login("alice", str(uuid.uuid4()), 1800)
    with pytest.raises(repo.ForbiddenError):
        repo.claim(alice["id"])

    bob, _ = _make_user("bob")
    assert repo.claim(bob["id"])["task_id"] == completed["task_id"]


def test_deactivate_does_not_revoke_a_later_submitters_current_version(
        database, seed_tasks):
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    alice_submission = _complete_next(alice, prefix="alice")
    admin = _admin_session()
    _revoke(admin, alice, _revoke_items(alice_submission))

    bob, _ = _make_user("bob")
    bob_submission = _complete_next(bob, prefix="bob-current")
    preview = repo.admin_deactivate_preview(alice["id"])
    assert preview["summary"]["published_to_revoke"] == 0

    result = repo.admin_deactivate(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=alice["id"],
        reason="offboarding",
        confirm=True,
        confirm_username="alice",
    )
    assert result["summary"]["revoked"] == 0

    with db.db_conn() as conn:
        task = conn.execute(
            """SELECT status, current_published_version_id
               FROM annotation_tasks WHERE id = %s""",
            (alice_submission["task_id"],),
        ).fetchone()
        current = conn.execute(
            """SELECT lifecycle, submitted_by_user_id
               FROM annotation_versions WHERE id = %s""",
            (bob_submission["version_id"],),
        ).fetchone()
    assert task == ("annotated", uuid.UUID(bob_submission["version_id"]))
    assert current == ("published", bob["id"])


def test_admin_audit_and_regular_export_exclude_revoked(
        database, seed_tasks, tmp_path):
    from export import export_xlsx

    seed_tasks(1)
    alice, _ = _make_user("alice")
    completed = _complete_next(alice)
    result = _revoke(_admin_session(), alice, _revoke_items(completed))

    audit = repo.admin_audit({}, limit=10)
    matching = [
        item for item in audit["items"]
        if item["action_id"] == result["action_id"]
    ]
    assert len(matching) == 1
    assert matching[0]["action_type"] == "revoke_annotations"
    assert matching[0]["reason"] == "quality failure"

    output = tmp_path / "current.xlsx"
    assert export_xlsx(output) == 0
    assert output.exists()


def test_concurrent_revoke_replay_creates_one_action(database, seed_tasks):
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    completed = _complete_next(alice)
    admin = _admin_session()
    operation_id = str(uuid.uuid4())
    items = _revoke_items(completed)
    barrier = threading.Barrier(2)

    def revoke_once() -> dict:
        barrier.wait(timeout=10)
        return _revoke(admin, alice, items, operation_id=operation_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: revoke_once(), range(2)))

    assert results[0]["action_id"] == results[1]["action_id"]
    assert sum(bool(item.get("idempotent_replay")) for item in results) == 1
    with db.db_conn() as conn:
        actions = conn.execute(
            "SELECT count(*) FROM admin_actions WHERE operation_id = %s",
            (operation_id,),
        ).fetchone()[0]
        items_count = conn.execute(
            """SELECT count(*) FROM admin_action_items i
               JOIN admin_actions a ON a.id = i.admin_action_id
               WHERE a.operation_id = %s""",
            (operation_id,),
        ).fetchone()[0]
    assert actions == items_count == 1


def test_complete_revision_racing_revoke_never_loses_the_new_submission(
        database, seed_tasks):
    """The old expected version must not let Admin overwrite a newer publish."""
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    original = _complete_next(alice, prefix="original")
    repo.reopen_completed(alice["id"], original["task_id"], str(uuid.uuid4()))
    revision = repo.get_assignment(alice["id"])
    admin = _admin_session()
    admin_operation = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def complete_revision():
        barrier.wait(timeout=10)
        return repo.complete(
            alice["id"], revision["lease_token"], revision["revision"],
            "annotated", [], _segments(revision, "newer"),
            str(uuid.uuid4()), "concurrent-complete",
        )

    def revoke_old():
        barrier.wait(timeout=10)
        try:
            _revoke(
                admin, alice, _revoke_items(original),
                operation_id=admin_operation,
            )
        except repo.ConflictError:
            return "conflict"
        return "revoked"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        completed_future = executor.submit(complete_revision)
        revoke_future = executor.submit(revoke_old)
        completed = completed_future.result(timeout=15)
        revoke_result = revoke_future.result(timeout=15)

    assert completed["status"] == "annotated"
    assert revoke_result == "conflict"
    with db.db_conn() as conn:
        task = conn.execute(
            """SELECT status, current_published_version_id
               FROM annotation_tasks WHERE id = %s""",
            (original["task_id"],),
        ).fetchone()
        versions = conn.execute(
            """SELECT id, lifecycle FROM annotation_versions
               WHERE task_id = %s ORDER BY version_no""",
            (original["task_id"],),
        ).fetchall()
        action_count = conn.execute(
            "SELECT count(*) FROM admin_actions WHERE operation_id = %s",
            (admin_operation,),
        ).fetchone()[0]
    assert task[0] == "annotated"
    assert str(task[1]) == revision["version_id"]
    assert (uuid.UUID(original["version_id"]), "superseded") in versions
    assert (uuid.UUID(revision["version_id"]), "published") in versions
    assert action_count == 0


def test_restore_rejects_deactivate_source_and_inactive_revoke_submitter(
        database, seed_tasks):
    import db

    seed_tasks(2)
    alice, _ = _make_user("alice")
    bob, _ = _make_user("bob")
    alice_submission = _complete_next(alice, prefix="alice")
    bob_submission = _complete_next(bob, prefix="bob")
    admin = _admin_session()

    alice_deactivation = repo.admin_deactivate(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=alice["id"],
        reason="alice offboarding",
        confirm=True,
        confirm_username="alice",
    )
    with pytest.raises(repo.ConflictError, match="not a restorable revoke"):
        repo.admin_restore(
            admin_session_id=admin["id"],
            operation_id=str(uuid.uuid4()),
            admin_action_id=alice_deactivation["action_id"],
            task_ids=[alice_submission["task_id"]],
            reason="must not restore deactivation",
            confirm=True,
        )

    bob_revoke = _revoke(admin, bob, _revoke_items(bob_submission))
    bob_deactivation = repo.admin_deactivate(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=bob["id"],
        reason="bob offboarding",
        confirm=True,
        confirm_username="bob",
    )
    assert bob_deactivation["summary"]["revoked"] == 0
    with pytest.raises(repo.ConflictError, match="submitter is not active"):
        repo.admin_restore(
            admin_session_id=admin["id"],
            operation_id=str(uuid.uuid4()),
            admin_action_id=bob_revoke["action_id"],
            task_ids=[bob_submission["task_id"]],
            reason="must not restore inactive submitter",
            confirm=True,
        )

    with db.db_conn() as conn:
        tasks = conn.execute(
            """SELECT id, status, current_published_version_id
               FROM annotation_tasks ORDER BY allocation_order"""
        ).fetchall()
        old_versions = conn.execute(
            """SELECT id, lifecycle FROM annotation_versions
               WHERE id = ANY(%s) ORDER BY id""",
            ([alice_submission["version_id"], bob_submission["version_id"]],),
        ).fetchall()
        annotators = conn.execute(
            """SELECT username, status FROM annotators
               WHERE id = ANY(%s) ORDER BY username""",
            ([alice["id"], bob["id"]],),
        ).fetchall()
        restore_actions = conn.execute(
            """SELECT count(*) FROM admin_actions
               WHERE action_type = 'restore_annotations'"""
        ).fetchone()[0]
    assert all(row[1] == "pending" and row[2] is None for row in tasks)
    assert old_versions == sorted([
        (uuid.UUID(alice_submission["version_id"]), "revoked"),
        (uuid.UUID(bob_submission["version_id"]), "revoked"),
    ])
    assert annotators == [("alice", "deactivated"), ("bob", "deactivated")]
    assert restore_actions == 0


def test_release_assignment_racing_complete_has_one_consistent_winner(
        database, seed_tasks):
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    assignment = repo.claim(alice["id"])
    admin = _admin_session()
    complete_operation = str(uuid.uuid4())
    release_operation = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def complete_assignment() -> tuple[str, dict | str]:
        barrier.wait(timeout=10)
        try:
            result = repo.complete(
                alice["id"], assignment["lease_token"],
                assignment["revision"], "annotated", [],
                _segments(assignment, "completed"), complete_operation,
                "release-complete-race",
            )
            return "completed", result
        except repo.ConflictError as error:
            return "conflict", str(error)

    def release_assignment() -> tuple[str, dict | str]:
        barrier.wait(timeout=10)
        try:
            result = repo.admin_release_assignment(
                admin_session_id=admin["id"],
                operation_id=release_operation,
                task_id=assignment["task_id"],
                reason="operator release",
                confirm=True,
            )
            return "released", result
        except repo.ConflictError as error:
            return "conflict", str(error)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        complete_future = executor.submit(complete_assignment)
        release_future = executor.submit(release_assignment)
        complete_outcome = complete_future.result(timeout=15)
        release_outcome = release_future.result(timeout=15)

    assert (complete_outcome[0], release_outcome[0]) in {
        ("completed", "conflict"),
        ("conflict", "released"),
    }
    with db.db_conn() as conn:
        task = conn.execute(
            """SELECT status, current_published_version_id
               FROM annotation_tasks WHERE id = %s""",
            (assignment["task_id"],),
        ).fetchone()
        assignments = conn.execute(
            "SELECT count(*) FROM assignments WHERE task_id = %s",
            (assignment["task_id"],),
        ).fetchone()[0]
        versions = conn.execute(
            """SELECT id, lifecycle FROM annotation_versions
               WHERE task_id = %s ORDER BY version_no""",
            (assignment["task_id"],),
        ).fetchall()
        release_actions = conn.execute(
            "SELECT count(*) FROM admin_actions WHERE operation_id = %s",
            (release_operation,),
        ).fetchone()[0]
        complete_operations = conn.execute(
            "SELECT count(*) FROM operations WHERE operation_id = %s",
            (complete_operation,),
        ).fetchone()[0]
        leaked_clean_text = conn.execute(
            """SELECT count(*) FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.task_id = %s AND v.lifecycle = 'draft'
                 AND (s.text <> '' OR s.exclude_from_training)""",
            (assignment["task_id"],),
        ).fetchone()[0]

    assert assignments == 0
    if complete_outcome[0] == "completed":
        assert task == ("annotated", uuid.UUID(assignment["version_id"]))
        assert (uuid.UUID(assignment["version_id"]), "published") in versions
        assert all(lifecycle != "draft" for _version, lifecycle in versions)
        assert release_actions == 0
        assert complete_operations == 1
    else:
        assert task == ("pending", None)
        assert (uuid.UUID(assignment["version_id"]), "abandoned") in versions
        assert sum(lifecycle == "draft" for _version, lifecycle in versions) == 1
        assert release_actions == 1
        assert complete_operations == 0
        assert leaked_clean_text == 0


def test_restore_racing_deactivate_same_submitter_has_consistent_final_state(
        database, seed_tasks):
    """Two independent Admin sessions must not form task->annotator deadlock."""
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    submission = _complete_next(alice, prefix="alice")
    restore_admin = _admin_session()
    deactivate_admin = _admin_session()
    revoke = _revoke(restore_admin, alice, _revoke_items(submission))

    # Recreate a legitimate migration-era reservation. Before the lock-order
    # fix this made restore hold task->wait annotator while deactivate held
    # annotator->wait task.
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_tasks SET reserved_for_user_id = %s
               WHERE id = %s""",
            (alice["id"], submission["task_id"]),
        )
        conn.commit()

    restore_operation = str(uuid.uuid4())
    deactivate_operation = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def restore_annotation() -> tuple[str, dict | str]:
        barrier.wait(timeout=10)
        try:
            result = repo.admin_restore(
                admin_session_id=restore_admin["id"],
                operation_id=restore_operation,
                admin_action_id=revoke["action_id"],
                task_ids=[submission["task_id"]],
                reason="restore race",
                confirm=True,
            )
            return "restored", result
        except repo.ConflictError as error:
            return "conflict", str(error)

    def deactivate_submitter() -> dict:
        barrier.wait(timeout=10)
        return repo.admin_deactivate(
            admin_session_id=deactivate_admin["id"],
            operation_id=deactivate_operation,
            annotator_id=alice["id"],
            reason="deactivate race",
            confirm=True,
            confirm_username="alice",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        restore_future = executor.submit(restore_annotation)
        deactivate_future = executor.submit(deactivate_submitter)
        restore_outcome = restore_future.result(timeout=15)
        deactivation = deactivate_future.result(timeout=15)

    assert restore_outcome[0] in {"restored", "conflict"}
    assert deactivation["success"] is True
    restored_first = restore_outcome[0] == "restored"
    assert deactivation["summary"]["revoked"] == int(restored_first)

    with db.db_conn() as conn:
        annotator_status = conn.execute(
            "SELECT status FROM annotators WHERE id = %s", (alice["id"],),
        ).fetchone()[0]
        task = conn.execute(
            """SELECT status, current_published_version_id,
                      reserved_for_user_id
               FROM annotation_tasks WHERE id = %s""",
            (submission["task_id"],),
        ).fetchone()
        old_version = conn.execute(
            """SELECT lifecycle, revoked_by_admin_action_id
               FROM annotation_versions WHERE id = %s""",
            (submission["version_id"],),
        ).fetchone()
        live_drafts = conn.execute(
            """SELECT id FROM annotation_versions
               WHERE task_id = %s AND lifecycle = 'draft'""",
            (submission["task_id"],),
        ).fetchall()
        leaked_text = conn.execute(
            """SELECT count(*) FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.task_id = %s AND v.lifecycle = 'draft'
                 AND (s.text <> '' OR s.exclude_from_training)""",
            (submission["task_id"],),
        ).fetchone()[0]
        assignment_count = conn.execute(
            "SELECT count(*) FROM assignments WHERE task_id = %s",
            (submission["task_id"],),
        ).fetchone()[0]
        restore_action_count = conn.execute(
            "SELECT count(*) FROM admin_actions WHERE operation_id = %s",
            (restore_operation,),
        ).fetchone()[0]
        event_counts = dict(conn.execute(
            """SELECT event_type, count(*) FROM annotation_events
               WHERE task_id = %s
                 AND event_type IN ('revoked_admin', 'restored_admin')
               GROUP BY event_type""",
            (submission["task_id"],),
        ).fetchall())
        block_action = conn.execute(
            """SELECT admin_action_id FROM task_annotator_blocks
               WHERE task_id = %s AND user_id = %s""",
            (submission["task_id"], alice["id"]),
        ).fetchone()[0]

    assert annotator_status == "deactivated"
    assert task == ("pending", None, None)
    assert old_version[0] == "revoked"
    assert len(live_drafts) == 1
    assert leaked_text == assignment_count == 0
    assert restore_action_count == int(restored_first)
    if restored_first:
        assert old_version[1] == uuid.UUID(deactivation["action_id"])
        assert block_action == uuid.UUID(deactivation["action_id"])
        assert event_counts == {"restored_admin": 1, "revoked_admin": 2}
    else:
        assert old_version[1] == uuid.UUID(revoke["action_id"])
        assert block_action == uuid.UUID(revoke["action_id"])
        assert event_counts == {"revoked_admin": 1}
