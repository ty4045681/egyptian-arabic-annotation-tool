"""Administrator scene-scope contracts, lock order, and resume behavior."""
from __future__ import annotations

import concurrent.futures
import threading
import uuid

import pytest

import annotation_repository as repo
from tests.test_admin_repository import (
    _admin_session, _complete_next, _make_user, _revoke, _revoke_items,
)
from tests.test_scene_claims import source


def _set_scope(admin: dict, annotator: dict, *, mode: str,
               scene_codes: list[str] | None = None, revision: int = 0,
               operation_id: str | None = None, reason: str = "scope update",
               allow_unknown: bool = False) -> dict:
    return repo.admin_set_scene_scope(
        admin["id"], annotator["id"],
        {
            "operation_id": operation_id or str(uuid.uuid4()),
            "expected_revision": revision,
            "mode": mode,
            "scene_codes": scene_codes or [],
            "allow_unknown": allow_unknown,
            "reason": reason,
        },
    )


def test_scene_scope_is_idempotent_revisioned_and_audited(database, seed_tasks):
    import db

    seed_tasks(1)
    alice, _ = _make_user("alice")
    admin = _admin_session()
    operation_id = str(uuid.uuid4())
    first = _set_scope(
        admin, alice, mode="restricted", scene_codes=["airport"],
        operation_id=operation_id, reason="limit airport",
    )
    assert first["success"] is True
    assert first["scope"]["mode"] == "restricted"
    assert first["scope"]["scene_codes"] == ["airport"]
    assert first["scope"]["revision"] == 1
    replay = _set_scope(
        admin, alice, mode="restricted", scene_codes=["airport"],
        operation_id=operation_id, reason="limit airport",
    )
    assert replay["idempotent_replay"] is True
    assert replay["action_id"] == first["action_id"]
    with pytest.raises(repo.ConflictError):
        _set_scope(
            admin, alice, mode="none", revision=0, reason="stale revision",
        )
    with pytest.raises(repo.ValidationError):
        _set_scope(admin, alice, mode="none", revision=1, reason="   ")
    second = _set_scope(
        admin, alice, mode="none", revision=1, reason="close pool",
    )
    assert second["scope"]["revision"] == 2
    with db.db_conn() as conn:
        actions = conn.execute(
            """SELECT action_type, reason, status FROM admin_actions
               WHERE id = %s""",
            (first["action_id"],),
        ).fetchone()
        items = conn.execute(
            """SELECT result FROM admin_action_items
               WHERE admin_action_id = %s""",
            (first["action_id"],),
        ).fetchone()
    assert actions == ("set_scene_scope", "limit airport", "completed")
    assert items[0] == "updated"


def test_existing_assignment_resumes_after_scope_none(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "shopping", "high", "scope-resume-shopping")
    alice, _ = _make_user("alice")
    claimed = repo.claim(alice["id"], source_scene="shopping")
    admin = _admin_session()
    _set_scope(admin, alice, mode="none", reason="close after claim")
    resumed = repo.claim(alice["id"], source_scene="airport")
    assert resumed["resumed"] is True
    assert resumed["task_id"] == claimed["task_id"]
    assert resumed["lease_token"] == claimed["lease_token"]


def test_scope_and_deactivate_same_admin_session_do_not_deadlock(
        database, seed_tasks):
    seed_tasks(1)
    alice, _ = _make_user("alice")
    _complete_next(alice, prefix="alice")
    admin = _admin_session()
    barrier = threading.Barrier(2)

    def set_scope() -> dict:
        barrier.wait(timeout=10)
        return _set_scope(
            admin, alice, mode="restricted", scene_codes=["airport"],
            reason="concurrent scope",
        )

    def deactivate() -> dict:
        barrier.wait(timeout=10)
        return repo.admin_deactivate(
            admin_session_id=admin["id"],
            operation_id=str(uuid.uuid4()),
            annotator_id=alice["id"],
            reason="concurrent deactivate",
            confirm=True,
            confirm_username="alice",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        scope_future = executor.submit(set_scope)
        deactivate_future = executor.submit(deactivate)
        scope_result = scope_future.result(timeout=20)
        deactivate_result = deactivate_future.result(timeout=20)

    assert scope_result["success"] is True
    assert deactivate_result["success"] is True
    assert deactivate_result["summary"]["revoked"] == 1
    detail = repo.admin_annotator_detail(alice["id"], {})
    assert detail["status"] == "deactivated"
    assert detail["scene_scope"]["mode"] == "restricted"


def test_scope_and_revoke_same_admin_session_do_not_deadlock(
        database, seed_tasks):
    seed_tasks(1)
    alice, _ = _make_user("alice")
    completed = _complete_next(alice, prefix="alice")
    admin = _admin_session()
    barrier = threading.Barrier(2)

    def set_scope() -> dict:
        barrier.wait(timeout=10)
        return _set_scope(
            admin, alice, mode="restricted", scene_codes=["shopping"],
            reason="concurrent scope revoke",
        )

    def revoke() -> dict:
        barrier.wait(timeout=10)
        return _revoke(admin, alice, _revoke_items(completed),
                       reason="concurrent revoke")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        scope_future = executor.submit(set_scope)
        revoke_future = executor.submit(revoke)
        scope_result = scope_future.result(timeout=20)
        revoke_result = revoke_future.result(timeout=20)

    assert scope_result["success"] is True
    assert revoke_result["success"] is True
    assert revoke_result["summary"]["revoked"] == 1
    assert repo.admin_annotator_detail(alice["id"], {})["scene_scope"]["mode"] == "restricted"


def test_scope_and_claim_do_not_deadlock_and_resume_if_assigned(
        database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "shopping", "high", "scope-claim-shopping")
    alice, _ = _make_user("alice")
    admin = _admin_session()
    barrier = threading.Barrier(2)

    def set_scope() -> dict:
        barrier.wait(timeout=10)
        return _set_scope(admin, alice, mode="none", reason="concurrent claim")

    def claim() -> dict | str:
        barrier.wait(timeout=10)
        try:
            return repo.claim(alice["id"])
        except (repo.NoTaskAvailable, repo.ForbiddenError) as error:
            return type(error).__name__

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        scope_future = executor.submit(set_scope)
        claim_future = executor.submit(claim)
        scope_result = scope_future.result(timeout=20)
        claim_result = claim_future.result(timeout=20)

    assert scope_result["success"] is True
    if isinstance(claim_result, dict):
        resumed = repo.claim(alice["id"], source_scene="airport")
        assert resumed["resumed"] is True
        assert resumed["task_id"] == task
        assert resumed["lease_token"] == claim_result["lease_token"]
    else:
        assert claim_result in {"NoTaskAvailable", "ForbiddenError"}
        with pytest.raises((repo.NoTaskAvailable, repo.ForbiddenError)):
            repo.claim(alice["id"])
