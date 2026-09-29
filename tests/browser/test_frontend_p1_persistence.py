"""P1 minimal save-service verification (work package F, section 8.4).

Real PATCH traffic against the live Flask app in the production build:
single in-flight write with B-after-A coalescing, lost-response replay of
the identical operation id/body (server idempotency), offline edit with
reload + reconnect recovery, review-only saves, 401/409 freeze semantics
with draft retention and export, one-PATCH-per-save, legacy-to-new draft
restore through the real `AnnotationOffline` global, and terminal-outbox
preservation. Storage-quota and legacy-roundtrip-back paths stay P4 items
and are recorded as such in the acceptance report.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

import frontend_delivery as delivery
import server
from tests.browser.conftest import configure_admin, start_app_server, stop_app_server
from tests.browser.conftest import write_silence_wav

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "docs" / "plans" / "frontend-rebuild-p1" / "screenshots"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def p1_persist_site(client, seed_tasks, tmp_path, monkeypatch):
    configure_admin(monkeypatch)
    for dist in (delivery.DIST_STANDARD, delivery.DIST_P1):
        problems = delivery.verify_build_complete(dist)
        assert not problems, f"preview build incomplete in {dist}: {problems}"
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    seed_tasks(2)
    httpd, thread, url = start_app_server()
    yield {"url": url, "client": client}
    stop_app_server(httpd, thread)
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", False)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")


def _claim(client, username):
    login = client.post("/api/login", json={"username": username})
    assert login.status_code == 200, login.get_json()
    claimed = client.post("/api/assignment/claim", json={})
    assert claimed.status_code == 200, claimed.get_json()
    assignment = claimed.get_json()
    assert assignment.get("assigned") is True, assignment
    session_cookie = client.get_cookie("session")
    assert session_cookie is not None
    audio_dir = Path(server.app.config["AUDIO_DIR"])
    target = audio_dir / assignment["rel_path"]
    write_silence_wav(target, seconds=10.0)
    return assignment, session_cookie.value


def _open(playwright, url, session_value):
    browser = playwright.chromium.launch(headless=True, args=["--mute-audio"])
    context = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    context.add_cookies(
        [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
    )
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    response = page.goto(url + "/frontend-preview/workspace")
    assert response is not None and response.status == 200
    expect(page.get_by_test_id("workspace-taskbar")).to_be_visible(timeout=20000)
    return browser, page, page_errors


def _patch_bodies(patches):
    bodies = []
    for entry in patches:
        try:
            bodies.append(json.loads(entry))
        except Exception:
            bodies.append({})
    return bodies


def test_p1_persist_inflight_then_new_input(p1_persist_site):
    """A in flight + input B: A confirms without clearing B; B saves next."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-inflight")
    rev0 = int(assignment["revision"])
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            patch_posts: list[str] = []
            first_seen = {"done": False}

            def handle_first(route):
                # Hold the first PATCH in flight so a newer input (B) can
                # arrive while A is unconfirmed.
                if not first_seen["done"]:
                    first_seen["done"] = True
                    time.sleep(1.5)
                route.continue_()

            page.on("request", lambda req: patch_posts.append(req.post_data or "")
                    if req.method == "PATCH" and "assignment/current" in req.url else None)
            page.route("**/api/assignment/current", handle_first)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.get_by_label("Segment 1 transcript").fill("inflight input A")
            page.get_by_test_id("workspace-save").click()
            page.wait_for_timeout(500)
            # B arrives while A is still in flight (server delayed 1.5s).
            page.get_by_label("Segment 1 transcript").fill("inflight input A plus B")
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 2}", timeout=25000
            )
            bodies = _patch_bodies(patch_posts)
            assert len(bodies) == 2, bodies
            assert bodies[0].get("operation_id") != bodies[1].get("operation_id")
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "inflight input A plus B", timeout=5000
            )
            page.reload()
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "inflight input A plus B", timeout=20000
            )
        finally:
            browser.close()


def test_p1_persist_lost_response_replays_identical_op(p1_persist_site):
    """Server applies, response lost: the same id/body replays exactly once."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-lost")
    rev0 = int(assignment["revision"])
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            patch_posts: list[str] = []
            dropped = {"done": False}

            def handle(route):
                if not dropped["done"]:
                    dropped["done"] = True
                    route.fetch()
                    route.abort()
                else:
                    route.continue_()

            page.on("request", lambda req: patch_posts.append(req.post_data or "")
                    if req.method == "PATCH" and "assignment/current" in req.url else None)
            page.route("**/api/assignment/current", handle)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.get_by_label("Segment 1 transcript").fill("lost response probe")
            page.get_by_test_id("workspace-save").click()
            # Retry of the identical operation converges; the server applies
            # it once, so the revision advances by exactly one.
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=25000
            )
            bodies = _patch_bodies(patch_posts)
            assert len(bodies) >= 2, bodies
            assert bodies[0].get("operation_id") == bodies[1].get("operation_id"), bodies
            assert bodies[0] == bodies[1]
            page.reload()
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "lost response probe", timeout=20000
            )
        finally:
            browser.close()


def test_p1_persist_offline_reload_reconnect(p1_persist_site):
    """Offline edit survives reload; reconnect flushes the stored draft."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-offline")
    rev0 = int(assignment["revision"])
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            offline = {"active": False}

            def handle(route):
                if offline["active"]:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/api/assignment/current", handle)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            offline["active"] = True
            page.get_by_label("Segment 1 transcript").fill("offline edit kept")
            page.get_by_test_id("workspace-save").click()
            expect(page.get_by_test_id("save-state")).to_be_visible(timeout=15000)
            page.reload()
            # Same user/task: the local draft restores while still offline.
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "offline edit kept", timeout=20000
            )
            offline["active"] = False
            page.reload()
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=25000
            )
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "offline edit kept", timeout=10000
            )
        finally:
            browser.close()


def test_p1_persist_review_only_save(p1_persist_site):
    """A scene-review-only change saves and survives reload."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-review")
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            expect(page.get_by_test_id("workspace-taskbar")).to_be_visible(timeout=15000)
            boxes = page.locator('[data-theme-section], section').locator('input[type="checkbox"]')
            page.get_by_role("button", name="Show scene verification").click()
            checkboxes = page.locator('input[type="checkbox"]')
            expect(checkboxes.first).to_be_visible(timeout=10000)
            assert checkboxes.count() > 0, "review taxonomy is empty"
            page.get_by_role("radio", name="Confirm").check()
            checkboxes.first.check()
            page.get_by_test_id("workspace-save").click()
            page.wait_for_timeout(3000)
            page.reload()
            page.get_by_role("button", name="Show scene verification").click()
            expect(page.get_by_role("radio", name="Confirm")).to_be_checked(timeout=20000)
        finally:
            browser.close()


def test_p1_persist_unauthorized_freezes_without_retry(p1_persist_site):
    """401 stops writes immediately; the draft stays put."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-401")
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            count = {"n": 0}

            def handle(route):
                count["n"] += 1
                route.fulfill(
                    status=401,
                    body='{"error": "expired", "code": "not_authenticated"}',
                    content_type="application/json",
                )

            page.route("**/api/assignment/current", handle)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.get_by_label("Segment 1 transcript").fill("kept despite 401")
            page.get_by_test_id("workspace-save").click()
            page.wait_for_timeout(4000)
            # Exactly one attempt: no retry storm on auth failures.
            assert count["n"] == 1, count
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "kept despite 401", timeout=5000
            )
        finally:
            browser.close()


def test_p1_persist_conflict_keeps_draft_and_exports(p1_persist_site):
    """409 freezes with the draft intact and an export path.

    The conflict itself is injected at the HTTP layer so the client-side
    freeze/retain/export flow is deterministic; the server-side revision
    contract stays covered by the repository regression tests.
    """
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-409")
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            def handle(route):
                route.fulfill(
                    status=409,
                    body='{"error": "revision conflict", "current_revision": 99, "conflict": "revision"}',
                    content_type="application/json",
                )

            page.route("**/api/assignment/current", handle)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.get_by_label("Segment 1 transcript").fill("conflict draft kept")
            page.get_by_test_id("workspace-save").click()
            expect(page.get_by_test_id("workspace-conflict")).to_be_visible(timeout=20000)
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "conflict draft kept", timeout=5000
            )
            page.get_by_role("button", name="Copy local draft as text").click()
            export_box = page.get_by_test_id("workspace-export")
            expect(export_box).to_be_visible(timeout=10000)
            expect(export_box).to_have_value(re.compile("conflict draft kept"), timeout=5000)
            assert "round_id" not in (export_box.input_value() or "")
        finally:
            browser.close()


def test_p1_persist_back_to_legacy_keeps_latest_input(p1_persist_site):
    """R02: leaving for the legacy page persists the newest keystrokes first."""
    site = p1_persist_site
    client = site["client"]
    assignment, session_value = _claim(client, "p1-ps-backnav")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(
            [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
        )
        page = context.new_page()
        try:
            response = page.goto(site["url"] + "/frontend-preview/workspace")
            assert response is not None and response.status == 200
            expect(page.get_by_test_id("workspace-taskbar")).to_be_visible(timeout=20000)
            marker = "must survive immediate navigation"
            area = page.get_by_label("Segment 1 transcript")
            expect(area).to_be_editable(timeout=15000)
            area.fill(marker)
            # No waiting for debounce/autosave: the link itself must persist.
            # Navigation completes only after persistLocal() resolves, so
            # wait for the legacy URL before reading the restored value.
            page.get_by_role("link", name="Back to classic workspace").first.click()
            page.wait_for_url(f"{site['url']}/", timeout=20000)
            legacy_area = page.locator('textarea[data-text="0"]')
            expect(legacy_area).to_be_visible(timeout=15000)
            # Legacy restores the draft asynchronously after loading the
            # assignment; poll for the value instead of reading once.
            expect(legacy_area).to_have_value(marker, timeout=15000)
            stored = page.evaluate(
                "(u) => window.AnnotationOffline.listWorkingDrafts(u)", "p1-ps-backnav"
            )
            assert len(stored) >= 1
            assert marker in json.dumps(stored)
        finally:
            browser.close()


def test_p1_persist_discard_recovers_full_save_cycle(p1_persist_site):
    """R04: 409 → discard → refetch → edit → save → reload stays consistent."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-discard-full")
    rev0 = int(assignment["revision"])
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            patch_count = {"n": 0}
            fail = {"active": True}

            def handle(route):
                if fail["active"]:
                    route.fulfill(
                        status=409,
                        body='{"error": "revision conflict", "current_revision": 99, "conflict": "revision"}',
                        content_type="application/json",
                    )
                else:
                    patch_count["n"] += 1
                    route.continue_()

            page.route("**/api/assignment/current", handle)
            page.get_by_label("Segment 1 transcript").fill("before conflict")
            page.get_by_test_id("workspace-save").click()
            expect(page.get_by_test_id("workspace-conflict")).to_be_visible(timeout=20000)
            page.get_by_test_id("workspace-discard").click()
            # W13: discard requires an explicit confirmation now.
            page.get_by_role("button", name="Discard", exact=True).click()
            expect(page.get_by_test_id("workspace-conflict")).to_be_hidden(timeout=20000)
            fail["active"] = False
            page.get_by_label("Segment 1 transcript").fill("after discard should save")
            page.get_by_test_id("workspace-save").click()
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=25000
            )
            assert patch_count["n"] >= 1
            page.reload()
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "after discard should save", timeout=20000
            )
        finally:
            browser.close()


def test_p1_persist_legacy_draft_restores_in_preview(p1_persist_site):
    """Old page writes a dirty draft via the real global; preview resumes it."""
    site = p1_persist_site
    client = site["client"]
    assignment, session_value = _claim(client, "p1-ps-legacy")
    task_id = assignment["task_id"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(
            [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
        )
        page = context.new_page()
        try:
            page.goto(site["url"] + "/")
            page.wait_for_timeout(1000)
            saved = page.evaluate(
                """(args) => {
                  const draft = {
                    schema_version: 1,
                    updated_at: new Date().toISOString(),
                    status: 'dirty',
                    username: args.username,
                    task_id: args.taskId,
                    version_id: args.versionId,
                    lease_token: args.leaseToken,
                    server_revision: args.revision,
                    segments: args.segments.map((s) => ({...s})),
                    scene_review: null,
                    dirty_segment_ids: [args.segments[0].id],
                    review_dirty: false,
                    mode: 'annotation',
                    round_id: '',
                  };
                  draft.segments[0] = {...draft.segments[0], text: 'legacy draft marker'};
                  return window.AnnotationOffline.saveWorkingDraft(draft).then(() => true);
                }""",
                {
                    "username": "p1-ps-legacy",
                    "taskId": task_id,
                    "versionId": assignment["version_id"],
                    "leaseToken": assignment["lease_token"],
                    "revision": int(assignment["revision"]),
                    "segments": assignment["segments"],
                },
            )
            assert saved is True
            response = page.goto(site["url"] + "/frontend-preview/workspace")
            assert response is not None and response.status == 200
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "legacy draft marker", timeout=20000
            )
        finally:
            browser.close()


def test_p1_persist_terminal_outbox_preserved(p1_persist_site):
    """A legacy finish request is retained, never replayed as PATCH."""
    site = p1_persist_site
    client = site["client"]
    assignment, session_value = _claim(client, "p1-ps-terminal")
    task_id = assignment["task_id"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(
            [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
        )
        page = context.new_page()
        try:
            patches = []
            page.on("request", lambda req: patches.append(1)
                    if req.method == "PATCH" and "assignment/current" in req.url else None)
            page.goto(site["url"] + "/")
            page.wait_for_timeout(1000)
            operation_id = str(uuid.uuid4())
            saved = page.evaluate(
                """(args) => window.AnnotationOffline.putOutbox({
                  operation_id: args.operationId,
                  username: args.username,
                  task_id: args.taskId,
                  route: '/api/assignment/current/complete',
                  method: 'POST',
                  body: {
                    lease_token: args.leaseToken,
                    expected_revision: args.revision,
                    operation_id: args.operationId,
                    target_status: 'completed',
                    segments: [],
                  },
                  created_at: new Date().toISOString(),
                  attempts: 0,
                }).then(() => true);""",
                {
                    "operationId": operation_id,
                    "username": "p1-ps-terminal",
                    "taskId": task_id,
                    "leaseToken": assignment["lease_token"],
                    "revision": int(assignment["revision"]),
                },
            )
            assert saved is True
            page.goto(site["url"] + "/frontend-preview/workspace")
            expect(page.get_by_test_id("workspace-terminal")).to_be_visible(timeout=20000)
            page.wait_for_timeout(3000)
            assert patches == [], "terminal outbox must never replay as PATCH"
            remaining = page.evaluate(
                """(args) => window.AnnotationOffline.listOutbox(args.username)
                     .then((rows) => rows.map((r) => r.operation_id));""",
                {"username": "p1-ps-terminal"},
            )
            assert operation_id in remaining
        finally:
            browser.close()


def test_p1_persist_new_to_legacy_roundtrip(p1_persist_site):
    """New page writes a draft; the legacy page resumes it (reverse R02 trip).

    The companion test proves legacy->new; together they are the two-way
    interoperability the plan requires for ordinary PATCH drafts.
    """
    site = p1_persist_site
    client = site["client"]
    assignment, session_value = _claim(client, "p1-ps-new2legacy")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(
            [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
        )
        page = context.new_page()
        try:
            response = page.goto(site["url"] + "/frontend-preview/workspace")
            assert response is not None and response.status == 200
            expect(page.get_by_test_id("workspace-taskbar")).to_be_visible(timeout=20000)
            marker = "new page draft for legacy"
            page.get_by_label("Segment 1 transcript").fill(marker)
            page.get_by_role("link", name="Back to classic workspace").first.click()
            legacy_area = page.locator('textarea[data-text="0"]')
            expect(legacy_area).to_be_visible(timeout=15000)
            expect(legacy_area).to_have_value(marker, timeout=15000)
        finally:
            browser.close()


def test_p1_persist_account_deactivated_forbidden(p1_persist_site):
    """A deactivated account 403 freezes writes and keeps the draft."""
    site = p1_persist_site
    client = site["client"]
    assignment, session_value = _claim(client, "p1-ps-deactivated")
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            attempts = {"n": 0}

            def handle(route):
                attempts["n"] += 1
                route.fulfill(
                    status=403,
                    body='{"error": "Annotator account is deactivated", "code": "account_deactivated"}',
                    content_type="application/json",
                )

            page.route("**/api/assignment/current", handle)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.get_by_label("Segment 1 transcript").fill("kept after deactivation")
            page.get_by_test_id("workspace-save").click()
            page.wait_for_timeout(4000)
            assert attempts["n"] == 1, attempts
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "kept after deactivation", timeout=5000
            )
        finally:
            browser.close()


def test_p1_persist_same_page_online_recovery(p1_persist_site):
    """Offline edit → online event on the SAME page replays and syncs."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-online")
    rev0 = int(assignment["revision"])
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            offline = {"active": True}

            def handle(route):
                if offline["active"]:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/api/assignment/current", handle)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.get_by_label("Segment 1 transcript").fill("same page online recovery")
            page.get_by_test_id("workspace-save").click()
            page.wait_for_timeout(1500)
            offline["active"] = False
            page.evaluate("() => window.dispatchEvent(new Event('online'))")
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=25000
            )
        finally:
            browser.close()


def test_p1_persist_storage_failure_reported(p1_persist_site):
    """IndexedDB failure surfaces storage-error; never a fake 'saved'."""
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-storagefail")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(
            [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
        )
        page = context.new_page()
        try:
            # Break the legacy IndexedDB-backed store before app code runs.
            # `indexedDB` is a read-only accessor, so replace the property.
            page.add_init_script(
                "Object.defineProperty(window, 'indexedDB', {"
                "  configurable: true,"
                "  get() { throw new Error('storage disabled'); }"
                "});"
            )
            response = page.goto(site["url"] + "/frontend-preview/workspace")
            assert response is not None and response.status == 200
            expect(page.get_by_test_id("workspace-taskbar")).to_be_visible(timeout=20000)
            # A local write is what surfaces the storage failure.
            page.get_by_label("Segment 1 transcript").fill("typed into broken storage")
            expect(page.get_by_test_id("workspace-error")).to_be_visible(timeout=15000)
            state = page.get_by_test_id("save-state")
            expect(state).to_contain_text("Local storage failed", timeout=10000)
            # The newest input is never dropped just because storage failed.
            expect(page.get_by_label("Segment 1 transcript")).to_have_value(
                "typed into broken storage", timeout=5000
            )
        finally:
            browser.close()


def test_p1_persist_full_reload_single_queue(p1_persist_site):
    """A full page reload still results in exactly one PATCH per save.

    This is a *reload* test, not a same-document remount test: it proves no
    server-visible duplicate write across a reload. The React mount/unmount
    lifecycle (one queue/heartbeat per instance, subscriptions cleared on
    dispose) is covered by `frontend/src/test/controller-lifecycle.test.ts`
    instead, because page.reload destroys the JS environment and cannot
    detect leaked listeners.
    """
    site = p1_persist_site
    assignment, session_value = _claim(site["client"], "p1-ps-remount")
    rev0 = int(assignment["revision"])
    with sync_playwright() as playwright:
        browser, page, _ = _open(playwright, site["url"], session_value)
        try:
            patches = []
            page.on("request", lambda req: patches.append(1)
                    if req.method == "PATCH" and "assignment/current" in req.url else None)
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=15000)
            page.reload()
            expect(page.get_by_label("Segment 1 transcript")).to_be_visible(timeout=20000)
            page.get_by_label("Segment 1 transcript").fill("single queue after reload")
            page.get_by_test_id("workspace-save").click()
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=25000
            )
            # Give any hypothetical duplicate queue a chance to fire.
            page.wait_for_timeout(1500)
            assert len(patches) == 1, f"expected exactly one PATCH, saw {len(patches)}"
        finally:
            browser.close()
