"""Browser acceptance for session takeover, idle timeout, and IndexedDB drafts."""
from __future__ import annotations

import uuid

from playwright.sync_api import expect, sync_playwright

import annotation_repository as repo
import db
import server
from tests.browser.conftest import start_app_server, stop_app_server, write_silence_wav
from tests.browser.test_scene_workflow import _login_annotator


def _login_only(page, url, username):
    page.goto(url + "/login.html")
    page.locator("#username").fill(username)
    page.locator("#joinBtn").click()


def _login_and_wait_workspace(page, url, username):
    _login_only(page, url, username)
    expect(page.locator("#claimButton, textarea[data-text='0']").first).to_be_visible(timeout=15000)


def test_live_session_requires_takeover_and_freezes_old_page(provenance_site, tmp_path):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context_a = browser.new_context()
        context_b = browser.new_context()
        page_a = context_a.new_page()
        page_b = context_b.new_page()
        try:
            _login_annotator(page_a, url, "takeover-live")
            page_a.locator("#claimButton").click()
            expect(page_a.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            _login_only(page_b, url, "takeover-live")
            expect(page_b.locator("#conflictBox")).to_be_visible(timeout=10000)
            page_b.locator("#takeoverBtn").click()
            expect(page_b.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            page_a.locator("textarea[data-text='0']").fill("old page still editing")
            page_a.locator("#saveButton").click()
            expect(page_a.locator("#sessionOverlay.show")).to_be_visible(timeout=10000)
            expect(page_a).not_to_have_url("**/login.html")
            expect(page_a.locator("textarea[data-text='0']")).to_be_disabled()
            expect(page_a.locator("textarea[data-text='0']")).to_have_value("old page still editing")
            expect(page_a.locator("#sessionOverlay.show")).to_be_visible()
        finally:
            browser.close()


def test_stale_presence_allows_automatic_login(provenance_site):
    url = provenance_site["url"]
    user = repo.login("takeover-stale", str(uuid.uuid4()), 1800)
    repo.claim(user["fence"])
    with db.db_conn() as conn:
        stale = conn.execute(
            """UPDATE active_sessions AS session
                  SET last_seen_at = now() - interval '2 days'
                 FROM annotators AS annotator
                WHERE session.user_id = annotator.id
                  AND annotator.username = %s
            RETURNING session.last_seen_at < now() - interval '1 day'""",
            ("takeover-stale",),
        ).fetchone()
        conn.commit()
    assert stale == (True,)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context_b = browser.new_context()
            page_b = context_b.new_page()
            _login_and_wait_workspace(page_b, url, "takeover-stale")
            expect(page_b.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            page_b.close()
            context_b.close()
        finally:
            browser.close()


def test_offline_draft_restores_in_same_context_not_other_device(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context_a = browser.new_context()
        page_a = context_a.new_page()
        try:
            _login_annotator(page_a, url, "offline-restore")
            page_a.locator("#claimButton").click()
            expect(page_a.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            page_a.route("**/api/assignment/current", lambda route: route.abort("timedout") if route.request.method == "PATCH" else route.continue_())
            page_a.locator("textarea[data-text='0']").fill("device-a-only unsaved arabic text")
            page_a.wait_for_timeout(700)
            page_a.close()
            page_a2 = context_a.new_page()
            page_a2.goto(url + "/")
            expect(page_a2.locator("textarea[data-text='0']")).to_have_value(
                "device-a-only unsaved arabic text", timeout=15000
            )
            context_b = browser.new_context()
            page_b = context_b.new_page()
            _login_only(page_b, url, "offline-restore")
            expect(page_b.locator("#takeoverBtn, textarea[data-text='0']").first).to_be_visible(timeout=15000)
            if page_b.locator("#takeoverBtn").count() and page_b.locator("#takeoverBtn").is_visible():
                page_b.locator("#takeoverBtn").click()
            expect(page_b.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            expect(page_b.locator("textarea[data-text='0']")).not_to_have_value(
                "device-a-only unsaved arabic text"
            )
            page_b.close()
            context_b.close()
        finally:
            browser.close()


def test_heartbeat_only_still_idle_timeouts(client, seed_tasks, tmp_path, monkeypatch):
    from pathlib import Path
    from tests.browser.conftest import configure_admin, insert_source

    configure_admin(monkeypatch)
    server.app.config["SESSION_TTL_SECONDS"] = 3
    server.app.config["SESSION_PRESENCE_HEARTBEAT_SECONDS"] = 15
    task = seed_tasks(1)[0]
    write_silence_wav(Path(server.app.config["AUDIO_DIR"]) / "audio-000.wav")
    insert_source(task, scene="airport", confidence="high", batch="acceptance-browser-batch")
    httpd, thread, url = start_app_server()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                _login_annotator(page, url, "idle-heartbeat")
                with db.db_conn() as conn:
                    conn.execute(
                        """UPDATE active_sessions
                              SET expires_at = now() - interval '1 second',
                                  last_seen_at = now()"""
                    )
                    conn.commit()
                page.evaluate("() => window.AnnotatorSession && window.AnnotatorSession.beat()")
                expect(page.locator("#sessionOverlay.show")).to_be_visible(timeout=10000)
                expect(page).not_to_have_url("**/login.html")
            finally:
                browser.close()
    finally:
        stop_app_server(httpd, thread)


def test_activity_does_not_slide_absolute_deadline(client, seed_tasks, tmp_path, monkeypatch):
    from pathlib import Path
    from tests.browser.conftest import configure_admin, insert_source

    configure_admin(monkeypatch)
    server.app.config["SESSION_ABSOLUTE_SECONDS"] = 4
    server.app.config["SESSION_TTL_SECONDS"] = 30
    task = seed_tasks(1)[0]
    write_silence_wav(Path(server.app.config["AUDIO_DIR"]) / "audio-000.wav")
    insert_source(task, scene="airport", confidence="high", batch="acceptance-browser-batch")
    httpd, thread, url = start_app_server()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                _login_annotator(page, url, "absolute-cap")
                page.locator("#claimButton").click()
                expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
                page.locator("textarea[data-text='0']").fill("keep working")
                with db.db_conn() as conn:
                    conn.execute(
                        """UPDATE active_sessions
                              SET absolute_expires_at = now() - interval '1 second',
                                  expires_at = now() + interval '20 minutes'"""
                    )
                    conn.commit()
                page.evaluate("() => window.AnnotatorSession && window.AnnotatorSession.beat()")
                expect(page.locator("#sessionOverlayTitle")).to_contain_text("time limit", timeout=10000)
            finally:
                browser.close()
    finally:
        stop_app_server(httpd, thread)


def test_unauthorized_does_not_navigate_before_local_persist(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            _login_annotator(page, url, "no-nav-401")
            page.locator("#claimButton").click()
            expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            page.locator("textarea[data-text='0']").fill("keep this local text")
            page.wait_for_timeout(600)
            page.route("**/api/assignment/current", lambda route: route.fulfill(
                status=401,
                content_type="application/json",
                body='{"error":"Session was replaced by another login.","code":"session_replaced","redirect":"/login.html?reason=session_replaced"}',
            ) if route.request.method == "PATCH" else route.continue_())
            page.locator("#saveButton").click()
            expect(page.locator("#sessionOverlay.show")).to_be_visible(timeout=10000)
            expect(page).not_to_have_url("**/login.html")
            stored = page.evaluate(
                """async () => {
                  const draft = await window.AnnotationOffline.getWorkingDraft('no-nav-401',
                    (window.state && window.state.assignment && window.state.assignment.task_id) || '');
                  return draft && draft.segments && draft.segments[0] && draft.segments[0].text;
                }"""
            )
            # state is not on window; read any working draft for this user instead.
            stored = page.evaluate(
                """async () => {
                  const rows = await window.AnnotationOffline.listWorkingDrafts('no-nav-401');
                  return rows[0] && rows[0].segments && rows[0].segments[0] && rows[0].segments[0].text;
                }"""
            )
            assert stored == "keep this local text"
        finally:
            browser.close()


def test_failed_heartbeat_keeps_unreported_activity_and_export_hook(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            _login_annotator(page, url, "heartbeat-retry")
            page.evaluate("() => window.AnnotatorSession.stopHeartbeat()")
            page.route("**/api/session/heartbeat", lambda route: route.abort("failed"))
            result = page.evaluate(
                """async () => {
                  window.AnnotatorSession.markActivity();
                  const ok = await window.AnnotatorSession.beat();
                  return {
                    ok,
                    activity: window.AnnotatorSession.state.activitySinceHeartbeat,
                    exportHook: typeof window.AnnotatorSession.state.onExport
                  };
                }"""
            )
            assert result == {"ok": False, "activity": True, "exportHook": "function"}
            expect(page.locator("#networkFlag")).to_be_visible()
            immutable = page.evaluate(
                """async () => {
                  const operationId = crypto.randomUUID();
                  const base = {
                    operation_id: operationId,
                    username: 'heartbeat-retry',
                    task_id: 'immutability-check',
                    route: '/api/assignment/current',
                    method: 'PATCH',
                    body: {operation_id: operationId, segments: [{id: 1, text: 'first'}]}
                  };
                  await window.AnnotationOffline.putOutbox(base);
                  let rejected = false;
                  try {
                    await window.AnnotationOffline.putOutbox({
                      ...base,
                      body: {operation_id: operationId, segments: [{id: 1, text: 'changed'}]}
                    });
                  } catch (_) { rejected = true; }
                  const stored = await window.AnnotationOffline.getOutboxItem(operationId);
                  await window.AnnotationOffline.confirmOutbox(operationId, {});
                  return {rejected, text: stored.body.segments[0].text};
                }"""
            )
            assert immutable == {"rejected": True, "text": "first"}
        finally:
            browser.close()


def test_account_deactivated_403_freezes_dirty_workspace(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            _login_annotator(page, url, "deactivated-live")
            page.locator("#claimButton").click()
            area = page.locator("textarea[data-text='0']")
            expect(area).to_be_visible(timeout=15000)
            area.fill("local text before deactivation")
            page.wait_for_timeout(600)
            page.evaluate("() => window.AnnotatorSession.stopHeartbeat()")
            page.route(
                "**/api/session/heartbeat",
                lambda route: route.fulfill(
                    status=403,
                    content_type="application/json",
                    body='{"error":"Account deactivated","code":"account_deactivated"}',
                ),
            )
            assert page.evaluate("() => window.AnnotatorSession.beat()") is False
            expect(page.locator("#sessionOverlay.show")).to_be_visible(timeout=10000)
            expect(page.locator("#sessionOverlayTitle")).to_contain_text("deactivated")
            expect(page.locator("#sessionExportButton")).to_be_visible()
            expect(area).to_be_disabled()
        finally:
            browser.close()


def test_online_event_replays_save_and_preserves_newer_offline_edit(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            _login_annotator(page, url, "online-replay-save")
            page.locator("#claimButton").click()
            area = page.locator("textarea[data-text='0']")
            expect(area).to_be_visible(timeout=15000)
            area.fill("first offline snapshot")
            context.set_offline(True)
            page.locator("#saveButton").click()
            expect(page.locator("#saveState")).to_have_text("Save failed", timeout=10000)
            area.fill("newer edit made while offline")
            page.wait_for_timeout(700)
            stored = page.evaluate(
                """async () => {
                  const drafts = await window.AnnotationOffline.listWorkingDrafts('online-replay-save');
                  const outbox = await window.AnnotationOffline.listOutbox('online-replay-save');
                  return {draft: drafts[0], outbox: outbox[0]};
                }"""
            )
            assert stored["draft"]["segments"][0]["text"] == "newer edit made while offline"
            assert stored["draft"]["dirty_segment_ids"] == [1]
            assert stored["outbox"]["body"]["segments"][0]["text"] == "first offline snapshot"
            task_id = stored["draft"]["task_id"]

            context.set_offline(False)
            expect(page.locator("#saveState")).to_have_text("Saved", timeout=20000)
            expect(area).to_have_value("newer edit made while offline")
            remaining = page.evaluate(
                """async () => ({
                  outbox: (await window.AnnotationOffline.listOutbox('online-replay-save')).length,
                  draft: (await window.AnnotationOffline.listWorkingDrafts('online-replay-save'))[0]
                })"""
            )
            assert remaining["outbox"] == 0
            assert remaining["draft"]["dirty_segment_ids"] == []
            with db.db_conn() as conn:
                text = conn.execute(
                    """SELECT segment.text
                         FROM annotation_versions AS version
                         JOIN segments AS segment ON segment.version_id = version.id
                        WHERE version.task_id = %s
                          AND version.lifecycle = 'draft'
                          AND segment.segment_id = 1""",
                    (task_id,),
                ).fetchone()[0]
            assert text == "newer edit made while offline"
        finally:
            browser.close()


def test_response_lost_then_reload_does_not_ack_newer_local_edit(provenance_site):
    url = provenance_site["url"]
    dropped = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def commit_then_drop(route):
            if route.request.method == "PATCH" and not dropped:
                response = route.fetch()
                assert response.status == 200
                dropped.append(True)
                route.abort("timedout")
            else:
                route.continue_()

        try:
            _login_annotator(page, url, "response-lost")
            page.locator("#claimButton").click()
            area = page.locator("textarea[data-text='0']")
            expect(area).to_be_visible(timeout=15000)
            page.route("**/api/assignment/current", commit_then_drop)
            area.fill("server committed snapshot")
            page.locator("#saveButton").click()
            expect(page.locator("#saveState")).to_have_text("Save failed", timeout=10000)
            area.fill("newer local edit after lost response")
            page.wait_for_timeout(700)
            page.close()

            resumed = context.new_page()
            resumed.goto(url + "/")
            resumed_area = resumed.locator("textarea[data-text='0']")
            expect(resumed_area).to_have_value(
                "newer local edit after lost response", timeout=15000
            )
            resumed.locator("#saveButton").click()
            expect(resumed.locator("#saveState")).to_have_text("Saved", timeout=15000)
            expect(resumed_area).to_have_value("newer local edit after lost response")
            stored = resumed.evaluate(
                """async () => (await window.AnnotationOffline.listWorkingDrafts('response-lost'))[0]"""
            )
            with db.db_conn() as conn:
                text = conn.execute(
                    """SELECT segment.text
                         FROM annotation_versions AS version
                         JOIN segments AS segment ON segment.version_id = version.id
                        WHERE version.task_id = %s
                          AND version.lifecycle = 'draft'
                          AND segment.segment_id = 1""",
                    (stored["task_id"],),
                ).fetchone()[0]
            assert dropped == [True]
            assert text == "newer local edit after lost response"
        finally:
            browser.close()


def test_revision_mismatch_requires_copy_or_explicit_discard(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        try:
            _login_annotator(page, url, "revision-conflict-ui")
            page.locator("#claimButton").click()
            area = page.locator("textarea[data-text='0']")
            expect(area).to_be_visible(timeout=15000)
            area.fill("local conflict text")
            page.wait_for_timeout(700)
            draft = page.evaluate(
                """async () => (await window.AnnotationOffline.listWorkingDrafts('revision-conflict-ui'))[0]"""
            )
            with db.db_conn() as conn:
                conn.execute(
                    """UPDATE annotation_versions
                          SET revision = revision + 1
                        WHERE task_id = %s AND lifecycle = 'draft'""",
                    (draft["task_id"],),
                )
                conn.commit()

            page.reload()
            expect(page.locator("#localConflictOverlay.show")).to_be_visible(timeout=15000)
            assert "local conflict text" in page.locator("#localConflictText").input_value()
            expect(page.locator("#localConflictBody")).to_contain_text("server is now at revision")
            expect(page.locator("textarea[data-text='0']")).not_to_have_value("local conflict text")
            page.locator("#localConflictDiscard").click()
            expect(page.locator("#localConflictOverlay.show")).not_to_be_visible(timeout=15000)
            expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)
            assert page.evaluate(
                """async () => (await window.AnnotationOffline.listWorkingDrafts('revision-conflict-ui')).length"""
            ) == 0
        finally:
            browser.close()


def test_online_event_replays_pending_completion(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            _login_annotator(page, url, "online-replay-complete")
            page.locator("#claimButton").click()
            areas = page.locator("textarea[data-text]")
            expect(areas.first).to_be_visible(timeout=15000)
            for index in range(areas.count()):
                areas.nth(index).fill(f"completion text {index}")
            with page.expect_response(
                lambda response: response.url.endswith("/api/assignment/current")
                and response.request.method == "PATCH",
                timeout=10000,
            ):
                page.locator("#saveButton").click()
            expect(page.locator("#saveState")).to_have_text("Saved", timeout=10000)
            draft = page.evaluate(
                """async () => (await window.AnnotationOffline.listWorkingDrafts('online-replay-complete'))[0]"""
            )

            context.set_offline(True)
            page.locator("#completeButton").click()
            expect(page.locator("#saveState")).to_have_text("Waiting for connection", timeout=12000)
            expect(areas.first).to_be_disabled()
            pending = page.evaluate(
                """async () => await window.AnnotationOffline.listOutbox('online-replay-complete')"""
            )
            assert pending[-1]["route"].endswith("/complete")

            context.set_offline(False)
            expect(page.locator("#claimButton")).to_be_visible(timeout=20000)
            with db.db_conn() as conn:
                status = conn.execute(
                    "SELECT status FROM annotation_tasks WHERE id = %s",
                    (draft["task_id"],),
                ).fetchone()[0]
            assert status == "annotated"
        finally:
            browser.close()
