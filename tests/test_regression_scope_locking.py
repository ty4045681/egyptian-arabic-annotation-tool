"""Force scope/deactivation interleaving through actual PostgreSQL locks."""
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import annotation_repository as repo
import db


def test_scope_change_and_deactivation_same_admin_session_do_not_deadlock(database, monkeypatch):
    username = "scope-lock-acceptance"
    user = repo.login(username, str(uuid.uuid4()), 1800)
    admin = repo.create_admin_session(
        "primary", hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        hashlib.sha256(uuid.uuid4().bytes).hexdigest(), 1800, 3600,
    )
    scope_at_action = threading.Event()
    deactivate_has_admin_lock = threading.Event()
    original = repo._begin_admin_action

    def begin(cur, **kwargs):
        if kwargs["action_type"] == "set_scene_scope":
            scope_at_action.set()
            assert deactivate_has_admin_lock.wait(5), "deactivation did not enter its admin action"
        result = original(cur, **kwargs)
        if kwargs["action_type"] == "deactivate_annotator":
            deactivate_has_admin_lock.set()
        return result

    monkeypatch.setattr(repo, "_begin_admin_action", begin)

    def change_scope():
        try:
            return repo.admin_set_scene_scope(admin["id"], str(user["id"]), {
                "operation_id": str(uuid.uuid4()), "expected_revision": 0,
                "mode": "restricted", "scene_codes": ["airport"],
                "allow_unknown": False, "reason": "concurrent scope acceptance",
            })
        except repo.ConflictError as error:
            return {"expected_conflict": str(error)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        scope_future = pool.submit(change_scope)
        assert scope_at_action.wait(5)
        deactivate_future = pool.submit(
            repo.admin_deactivate, admin_session_id=admin["id"],
            operation_id=str(uuid.uuid4()), annotator_id=str(user["id"]),
            reason="concurrent deactivation acceptance", confirm=True,
            confirm_username=username,
        )
        # Raw DeadlockDetected/500 is never an acceptable business result.
        scope_result = scope_future.result(timeout=10)
        assert deactivate_future.result(timeout=10)["success"] is True
    assert scope_result.get("success") or scope_result.get("expected_conflict")
    with db.db_conn() as conn:
        assert conn.execute("SELECT status FROM annotators WHERE id=%s", (user["id"],)).fetchone() == ("deactivated",)
