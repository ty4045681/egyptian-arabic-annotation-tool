from __future__ import annotations

import hashlib
import uuid

import pytest

import annotation_repository as repo
import db
from annotation_metadata.claiming import parse_claim_filters
from annotation_metadata.queries import load_scope
from annotation_quality.claiming import count_cross_check_candidates
from tests.test_repository import full_segments
from tests.test_scene_claims import source, user


class FailRng:
    def randrange(self, n):
        raise AssertionError(f"claim type rng should not run (n={n})")


class ScriptedRng:
    """First randrange(10000) is the type draw; later calls are offsets."""

    def __init__(self, type_draw, *offsets):
        self.type_draw = type_draw
        self.offsets = list(offsets)
        self.type_calls = 0

    def randrange(self, n):
        if n == 10000:
            self.type_calls += 1
            if self.type_calls > 1:
                raise AssertionError("type random was redrawn")
            return self.type_draw
        if not self.offsets:
            raise AssertionError(f"unexpected offset randrange({n})")
        value = self.offsets.pop(0)
        if not (0 <= value < n):
            raise AssertionError(f"scripted offset {value} not in [0, {n})")
        return value


def enable_cross_check(*, enabled=True, sampling_rate_bps=10000):
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE cross_check_settings
               SET enabled = %s, sampling_rate_bps = %s
               WHERE id = 1""",
            (enabled, sampling_rate_bps),
        )


def fence_of(uid):
    return repo.load_session_fence(uid)


def annotate_all(username, task_ids, *, prefix="orig", **claim_filters):
    uid = user(username)
    fence = fence_of(uid)
    completed = []
    for _ in task_ids:
        assignment = repo.claim(fence, **claim_filters)
        repo.complete(
            fence, assignment["lease_token"], assignment["revision"],
            "annotated", [], full_segments(assignment, prefix),
            str(uuid.uuid4()), f"complete-{uuid.uuid4()}",
        )
        completed.append(assignment)
    return uid, completed


def release_cross_check(uid):
    with db.db_conn() as conn:
        row = conn.execute(
            """SELECT task_id, working_version_id, cross_check_round_id
               FROM assignments WHERE user_id = %s""",
            (uid,),
        ).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM assignments WHERE user_id = %s", (uid,))
        conn.execute(
            """UPDATE cross_check_rounds
               SET state = 'cancelled', termination_reason = 'test reset',
                   updated_at = now()
               WHERE id = %s""",
            (row[2],),
        )
        conn.execute(
            """UPDATE annotation_versions
               SET lifecycle = 'abandoned', updated_at = now()
               WHERE id = %s""",
            (row[1],),
        )
        conn.execute(
            """DELETE FROM task_annotation_participants
               WHERE task_id = %s AND user_id = %s""",
            (row[0], uid),
        )


def ordered_task_ids(task_ids):
    with db.db_conn() as conn:
        rows = conn.execute(
            """SELECT id FROM annotation_tasks
               WHERE id = ANY(%s)
               ORDER BY allocation_order, id""",
            (list(task_ids),),
        ).fetchall()
    return [str(row[0]) for row in rows]


def test_disabled_and_rate_zero_never_create_rounds(database, seed_tasks):
    tasks = seed_tasks(2, folder="done")
    source(tasks[0], "airport", "high")
    source(tasks[1], "airport", "high")
    annotate_all("alice-off", tasks, prefix="alice", source_scene="airport")

    enable_cross_check(enabled=False, sampling_rate_bps=10000)
    pending = seed_tasks(1, folder="pending")[0]
    source(pending, "airport", "high")
    bob = user("bob-off")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=FailRng(),
    )
    assert assignment["mode"] == "annotation"
    assert assignment["task_id"] == pending
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM cross_check_rounds").fetchone()[0] == 0

    repo.complete(
        fence_of(bob), assignment["lease_token"], assignment["revision"],
        "annotated", [], full_segments(assignment, "bob"),
        str(uuid.uuid4()), "bob-complete",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=0)
    carol = user("carol-off")
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(fence_of(carol), source_scene="airport", rng=FailRng())
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM cross_check_rounds").fetchone()[0] == 0


def test_ten_percent_and_hundred_percent_hit_and_miss(database, seed_tasks):
    annotated = seed_tasks(1, folder="ann")[0]
    source(annotated, "airport", "high")
    annotate_all("alice-rate", [annotated], prefix="alice", source_scene="airport")
    pending = seed_tasks(1, folder="pend")[0]
    source(pending, "airport", "high")
    enable_cross_check(enabled=True, sampling_rate_bps=1000)

    hit = repo.claim(
        fence_of(user("bob-hit")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    assert hit["mode"] == "cross_check"
    assert hit["task_id"] == annotated
    assert hit["status"] == "annotated"
    assert hit["resumed"] is False
    assert hit["cross_check"]["state"] == "in_progress"
    assert all(seg["text"] == "" for seg in hit["segments"])
    assert all(seg["exclude_from_training"] is False for seg in hit["segments"])

    miss = repo.claim(
        fence_of(user("carol-miss")), source_scene="airport",
        rng=ScriptedRng(1000),
    )
    assert miss["mode"] == "annotation"
    assert miss["task_id"] == pending


def test_historical_and_new_annotated_are_claimable(database, seed_tasks):
    old_task, new_task = seed_tasks(2)
    source(old_task, "airport", "high")
    source(new_task, "airport", "high")
    alice, completed = annotate_all(
        "alice-hist", [old_task, new_task], prefix="alice-secret",
        source_scene="airport",
    )
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions
               SET submitted_at = now() - interval '1 day'
               WHERE id = (
                   SELECT current_published_version_id
                   FROM annotation_tasks WHERE id = %s)""",
            (old_task,),
        )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    first = repo.claim(
        fence_of(user("bob-hist")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    second = repo.claim(
        fence_of(user("carol-hist")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    assert first["mode"] == second["mode"] == "cross_check"
    assert {first["task_id"], second["task_id"]} == {old_task, new_task}


def test_existing_assignment_resumes_without_rng_or_new_filters(database, seed_tasks):
    pending, annotated = seed_tasks(2)
    source(pending, "airport", "high")
    source(annotated, "hotel", "high")
    annotate_all("alice-resume", [annotated], prefix="alice", source_scene="hotel")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-resume")
    first = repo.claim(
        fence_of(bob), source_scene="hotel", rng=ScriptedRng(0, 0),
    )
    assert first["mode"] == "cross_check"
    enable_cross_check(enabled=False, sampling_rate_bps=0)
    resumed = repo.claim(
        fence_of(bob), source_scene="airport", rng=FailRng(),
    )
    assert resumed["resumed"] is True
    assert resumed["task_id"] == first["task_id"]
    assert resumed["lease_token"] == first["lease_token"]
    assert resumed["mode"] == "cross_check"
    assert resumed["version_id"] == first["version_id"]


def test_empty_pool_fallback_both_directions(database, seed_tasks):
    annotated = seed_tasks(1, folder="ann")[0]
    source(annotated, "airport", "high")
    annotate_all("alice-fb", [annotated], prefix="alice", source_scene="airport")
    pending = seed_tasks(1, folder="pend")[0]
    source(pending, "airport", "high")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)

    # Cross-check empty → normal. Consume the annotated candidate first.
    repo.claim(
        fence_of(user("bob-fb")), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    fallback_normal = repo.claim(
        fence_of(user("carol-fb")), source_scene="airport", rng=ScriptedRng(0),
    )
    assert fallback_normal["mode"] == "annotation"
    assert fallback_normal["task_id"] == pending

    # Normal empty + cross-check available on a miss still claims cross-check.
    extra = seed_tasks(1, folder="extra")[0]
    source(extra, "airport", "high")
    annotate_all("alice-fb2", [extra], prefix="alice2", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=1000)
    miss_fallback = repo.claim(
        fence_of(user("dave-fb")), source_scene="airport",
        rng=ScriptedRng(9999, 0),
    )
    assert miss_fallback["mode"] == "cross_check"
    assert miss_fallback["task_id"] == extra


def test_both_empty_and_lock_busy_are_distinct(database, seed_tasks):
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(fence_of(user("empty-user")), rng=ScriptedRng(0))

    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    annotate_all("alice-busy", [task], prefix="alice", source_scene="airport")
    with db.db_conn() as blocker:
        blocker.execute(
            "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
            (task,),
        )
        with pytest.raises(repo.TaskPoolBusy):
            repo.claim(
                fence_of(user("busy-user")), source_scene="airport",
                rng=ScriptedRng(0, 0, 0, 0, 0, 0, 0, 0, 0),
            )


def test_airport_filter_cannot_claim_hotel_only_audio(database, seed_tasks):
    hotel = seed_tasks(1)[0]
    source(hotel, "hotel", "high")
    annotate_all("alice-hotel-only", [hotel], prefix="alice", source_scene="hotel")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(
            fence_of(user("want-airport")), source_scene="airport",
            rng=ScriptedRng(0),
        )


def test_scene_scope_batch_confidence_and_unknown(database, seed_tasks):
    hotel, airport, legacy = seed_tasks(3)
    source(hotel, "hotel", "high")
    source(airport, "airport", "high")
    annotate_all(
        "alice-scene", [hotel, airport, legacy], prefix="alice",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)

    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(
            fence_of(user("emergencies-empty")), source_scene="emergencies",
            rng=ScriptedRng(0),
        )
    got = repo.claim(
        fence_of(user("airport-ok")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    assert got["task_id"] == airport

    restricted = user("restricted-hotel")
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO annotator_scene_scopes(user_id,mode) VALUES(%s,'restricted') "
            "ON CONFLICT(user_id) DO UPDATE SET mode='restricted'",
            (restricted,),
        )
        conn.execute(
            "INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'hotel')",
            (restricted,),
        )
    with pytest.raises(repo.ForbiddenError):
        repo.claim(fence_of(restricted), source_scene="airport")
    hotel_hit = repo.claim(
        fence_of(restricted), source_scene="hotel", rng=ScriptedRng(0, 0),
    )
    assert hotel_hit["task_id"] == hotel

    unknown = repo.claim(
        fence_of(user("unknown-scene")), source_scene="unknown",
        rng=ScriptedRng(0, 0),
    )
    assert unknown["task_id"] == legacy
    assert unknown["mode"] == "cross_check"


def test_batch_and_confidence_combo_does_not_mix_evidence(database, seed_tasks):
    mixed, other = seed_tasks(2)
    source(mixed, "airport", "high", "batch-alpha-2026")
    source(mixed, "hotel", "low", "batch-alpha-2026")
    source(other, "airport", "low", "batch-beta-2026")
    annotate_all("alice-combo", [mixed, other], prefix="alice")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)

    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(
            fence_of(user("airport-low")),
            source_scene="airport", source_confidence="low",
            batch_code="batch-alpha-2026",
            rng=ScriptedRng(0),
        )
    hit = repo.claim(
        fence_of(user("alpha-high")),
        batch_code="batch-alpha-2026", source_confidence="high",
        rng=ScriptedRng(0, 0),
    )
    assert hit["task_id"] == mixed


def test_multi_source_does_not_increase_weight(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    source(task, "hotel", "low")
    annotate_all("alice-weight", [task], prefix="alice")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-weight")
    with db.db_tx() as conn, conn.cursor() as cur:
        scope = load_scope(cur, bob)
        count = count_cross_check_candidates(
            cur, scope, parse_claim_filters(), bob,
        )
        airport_low = count_cross_check_candidates(
            cur, scope,
            parse_claim_filters(source_scene="airport", source_confidence="low"),
            bob,
        )
    assert count == 1
    assert airport_low == 0
    claimed = repo.claim(fence_of(bob), rng=ScriptedRng(0, 0))
    assert claimed["task_id"] == task


def test_offsets_select_head_middle_and_tail(database, seed_tasks):
    tasks = seed_tasks(3)
    for tid in tasks:
        source(tid, "airport", "high")
    annotate_all("alice-off", tasks, prefix="alice", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    expected = ordered_task_ids(tasks)
    bob = user("bob-off")
    for offset, want in enumerate(expected):
        got = repo.claim(
            fence_of(bob), source_scene="airport",
            rng=ScriptedRng(0, offset),
        )
        assert got["task_id"] == want
        release_cross_check(bob)


def test_reserved_migration_draft_keeps_priority(database, seed_tasks):
    annotated = seed_tasks(1, folder="ann")[0]
    source(annotated, "airport", "high")
    annotate_all("alice-rsv", [annotated], prefix="alice", source_scene="airport")
    reserved = seed_tasks(1, folder="rsv")[0]
    source(reserved, "airport", "high")
    bob = user("bob-rsv")
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET reserved_for_user_id = %s WHERE id = %s",
            (bob, reserved),
        )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=FailRng(),
    )
    assert assignment["mode"] == "annotation"
    assert assignment["task_id"] == reserved
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM cross_check_rounds").fetchone()[0] == 0


def test_covered_original_and_final_versions_are_not_redrawn(database, seed_tasks):
    original_task, later_task = seed_tasks(2)
    source(original_task, "airport", "high")
    source(later_task, "airport", "high")
    alice, completed = annotate_all(
        "alice-cov", [original_task, later_task], prefix="alice",
        source_scene="airport",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    other = user("cover-author")
    with db.db_conn() as conn:
        orig_pub, orig_base = conn.execute(
            """SELECT current_published_version_id, baseline_version_id
               FROM annotation_tasks WHERE id = %s""",
            (original_task,),
        ).fetchone()
        later_pub, later_base = conn.execute(
            """SELECT current_published_version_id, baseline_version_id
               FROM annotation_tasks WHERE id = %s""",
            (later_task,),
        ).fetchone()
        secondary = uuid.uuid4()
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status, purpose,
                    created_by_user_id, submitted_by_user_id, submitted_at)
               VALUES (%s, %s, 40, 'cross_check_submitted', 'annotated',
                       'cross_check', %s, %s, now())""",
            (secondary, original_task, other, other),
        )
        conn.execute(
            """INSERT INTO cross_check_rounds (
                   id, task_id, revision, original_version_id, original_annotator_id,
                   secondary_version_id, secondary_annotator_id,
                   baseline_version_id, baseline_quality, state,
                   settings_revision, sampling_rate_bps, claim_policy,
                   submitted_at, compared_at, original_word_count,
                   secondary_word_count, edit_distance
               ) VALUES (
                   %s, %s, 0, %s, %s, %s, %s, %s, 'exact', 'passed',
                   0, 1000, 'source_confidence', now(), now(), 2, 2, 0
               )""",
            (uuid.uuid4(), original_task, orig_pub, alice, secondary, other,
             orig_base),
        )
        new_pub = uuid.uuid4()
        conn.execute(
            """UPDATE annotation_versions
               SET lifecycle = 'superseded' WHERE id = %s""",
            (later_pub,),
        )
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status, purpose,
                    submitted_by_user_id, submitted_at)
               VALUES (%s, %s, 50, 'published', 'annotated', 'annotation',
                       %s, now())""",
            (new_pub, later_task, alice),
        )
        conn.execute(
            """UPDATE annotation_tasks
               SET current_published_version_id = %s WHERE id = %s""",
            (new_pub, later_task),
        )
        action = uuid.uuid4()
        conn.execute(
            """INSERT INTO admin_actions
                   (id, operation_id, action_type, reason, request_hash, status)
               VALUES (%s, %s, 'cross_check_decision', 'cover final', %s,
                       'completed')""",
            (action, uuid.uuid4(), "b" * 64),
        )
        secondary2 = uuid.uuid4()
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status, purpose,
                    created_by_user_id, submitted_by_user_id, submitted_at)
               VALUES (%s, %s, 41, 'cross_check_submitted', 'annotated',
                       'cross_check', %s, %s, now())""",
            (secondary2, later_task, other, other),
        )
        conn.execute(
            """INSERT INTO cross_check_rounds (
                   id, task_id, revision, original_version_id, original_annotator_id,
                   secondary_version_id, secondary_annotator_id,
                   baseline_version_id, baseline_quality, state,
                   settings_revision, sampling_rate_bps, claim_policy,
                   submitted_at, compared_at, original_word_count,
                   secondary_word_count, edit_distance, decision,
                   final_version_id, decided_by_admin_action_id, resolved_at
               ) VALUES (
                   %s, %s, 0, %s, %s, %s, %s, %s, 'exact', 'adjudicated',
                   0, 1000, 'source_confidence', now(), now(), 2, 2, 0,
                   'original', %s, %s, now()
               )""",
            (uuid.uuid4(), later_task, later_pub, alice, secondary2, other,
             later_base, new_pub, action),
        )

    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(
            fence_of(user("bob-cov")), source_scene="airport",
            rng=ScriptedRng(0),
        )

    uncovered = uuid.uuid4()
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions
               SET lifecycle = 'superseded' WHERE id = %s""",
            (new_pub,),
        )
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status, purpose,
                    submitted_by_user_id, submitted_at)
               VALUES (%s, %s, 51, 'published', 'annotated', 'annotation',
                       %s, now())""",
            (uncovered, later_task, alice),
        )
        conn.execute(
            """UPDATE annotation_tasks
               SET current_published_version_id = %s WHERE id = %s""",
            (uncovered, later_task),
        )
    redrawn = repo.claim(
        fence_of(user("carol-cov")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    assert redrawn["task_id"] == later_task
    assert redrawn["mode"] == "cross_check"


def test_missing_baseline_is_skipped_without_500(database, seed_tasks):
    broken, healthy = seed_tasks(2)
    source(broken, "airport", "high")
    source(healthy, "airport", "high")
    annotate_all(
        "alice-base", [broken, healthy], prefix="alice", source_scene="airport",
    )
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET baseline_version_id = NULL WHERE id = %s",
            (broken,),
        )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    got = repo.claim(
        fence_of(user("bob-base")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    assert got["task_id"] == healthy
    assert got["mode"] == "cross_check"


def test_original_author_cannot_claim_own_work(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    alice, _ = annotate_all(
        "alice-self", [task], prefix="alice", source_scene="airport",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(
            fence_of(alice), source_scene="airport", rng=ScriptedRng(0),
        )


def test_cancelled_round_save_is_rejected(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    annotate_all("alice-save", [task], prefix="alice", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-save")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE cross_check_rounds
               SET state = 'cancelled', termination_reason = 'cancelled',
                   updated_at = now()
               WHERE id = %s""",
            (assignment["cross_check"]["round_id"],),
        )
    with pytest.raises(repo.ConflictError, match="no longer in progress"):
        repo.save_draft(
            fence_of(bob), assignment["lease_token"], 0,
            full_segments(assignment, "bob"), str(uuid.uuid4()), "save-hash",
        )


def _assert_round_cancelled(task_id, round_id, draft_id, secondary_user,
                            published_id, *, termination_reason):
    with db.db_conn() as conn:
        round_row = conn.execute(
            """SELECT state, termination_reason FROM cross_check_rounds
               WHERE id = %s""",
            (round_id,),
        ).fetchone()
        draft_lifecycle = conn.execute(
            "SELECT lifecycle FROM annotation_versions WHERE id = %s",
            (draft_id,),
        ).fetchone()[0]
        live_drafts = conn.execute(
            """SELECT count(*) FROM annotation_versions
               WHERE task_id = %s AND lifecycle = 'draft'""",
            (task_id,),
        ).fetchone()[0]
        assignment_count = conn.execute(
            "SELECT count(*) FROM assignments WHERE task_id = %s",
            (task_id,),
        ).fetchone()[0]
        task = conn.execute(
            """SELECT status, current_published_version_id FROM annotation_tasks
               WHERE id = %s""",
            (task_id,),
        ).fetchone()
        participant = conn.execute(
            """SELECT 1 FROM task_annotation_participants
               WHERE task_id = %s AND user_id = %s""",
            (task_id, secondary_user),
        ).fetchone()
        cancelled = conn.execute(
            """SELECT count(*) FROM annotation_events
               WHERE event_type = 'cross_check_cancelled'
                 AND task_id = %s AND version_id = %s""",
            (task_id, draft_id),
        ).fetchone()[0]
    assert round_row == ("cancelled", termination_reason)
    assert draft_lifecycle == "abandoned"
    assert live_drafts == 0
    assert assignment_count == 0
    assert task[0] == "annotated"
    assert str(task[1]) == str(published_id)
    assert participant is not None
    assert cancelled >= 1


def test_abandon_cancels_in_progress_cross_check_round(database, seed_tasks):
    task = seed_tasks(1, folder="abn")[0]
    source(task, "airport", "high")
    annotate_all("alice-abn", [task], prefix="alice-keep", source_scene="airport")
    with db.db_conn() as conn:
        published, published_text = conn.execute(
            """SELECT t.current_published_version_id, s.text
               FROM annotation_tasks t
               JOIN segments s ON s.version_id = t.current_published_version_id
               WHERE t.id = %s ORDER BY s.segment_id LIMIT 1""",
            (task,),
        ).fetchone()
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-abn")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    result = repo.abandon(
        fence_of(bob), assignment["lease_token"], str(uuid.uuid4()), True,
    )
    assert result["abandoned"] is True
    _assert_round_cancelled(
        task, assignment["cross_check"]["round_id"], assignment["version_id"],
        bob, published, termination_reason="abandoned",
    )
    with db.db_conn() as conn:
        text = conn.execute(
            "SELECT text FROM segments WHERE version_id = %s ORDER BY segment_id LIMIT 1",
            (published,),
        ).fetchone()[0]
    assert text == published_text
    redrawn = repo.claim(
        fence_of(user("carol-abn")), source_scene="airport",
        rng=ScriptedRng(0, 0),
    )
    assert redrawn["mode"] == "cross_check"
    assert redrawn["task_id"] == task


def test_admin_release_cancels_in_progress_cross_check_round(database, seed_tasks):
    task = seed_tasks(1, folder="rel")[0]
    source(task, "airport", "high")
    annotate_all("alice-rel", [task], prefix="alice-keep", source_scene="airport")
    with db.db_conn() as conn:
        published = conn.execute(
            "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
            (task,),
        ).fetchone()[0]
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-rel")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    nonce = uuid.uuid4().hex
    admin = repo.create_admin_session(
        key_id="primary",
        token_digest=hashlib.sha256(f"token-{nonce}".encode()).hexdigest(),
        csrf_digest=hashlib.sha256(f"csrf-{nonce}".encode()).hexdigest(),
        idle_seconds=1800,
        absolute_seconds=28800,
        ip_hash="test-ip",
        user_agent="pytest",
    )
    released = repo.admin_release_assignment(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        task_id=assignment["task_id"],
        reason="operator release",
        confirm=True,
    )
    assert released["success"] is True
    _assert_round_cancelled(
        task, assignment["cross_check"]["round_id"], assignment["version_id"],
        bob, published, termination_reason="operator release",
    )
    assert repo.get_assignment(bob) is None
