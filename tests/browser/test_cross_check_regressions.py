"""Cross-check regressions for uncertain writes, conflicts, and stale reads."""
from __future__ import annotations

import json
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.cross_check_helpers import (
    login_admin, login_annotator, open_cross_checks, queue_awaiting,
)
from tests.test_admin_api import _admin_login
from tests.test_cross_check_admin import decision_body


def open_editor(page, site):
    login_admin(page, site["url"])
    open_cross_checks(page)
    expect(page.locator("[data-cc-round]").first).to_be_visible(timeout=15000)
    page.locator("[data-cc-round]").first.click()
    expect(page.locator("#ccDecisionForm")).to_be_visible(timeout=15000)
    page.locator("#ccDecision-edited").check()
    page.locator('input[name="ccEditBase"][value="original"]').check()
    page.locator("[data-editor-index='0']").fill("Local corrected manuscript")
    page.locator("#ccDecisionReason").fill("My local adjudication reason")


def published_text(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT s.text FROM segments s JOIN annotation_tasks t
               ON t.current_published_version_id = s.version_id
               WHERE t.id = %s ORDER BY s.segment_id""", (task_id,),
        ).fetchone()[0]


@pytest.mark.parametrize("server_accepted", [False, True], ids=["request-lost", "response-lost"])
def test_uncertain_decision_locks_form_and_retries_same_body(cross_check_site, server_accepted):
    site = cross_check_site
    task_id = queue_awaiting(site["client"], site["seed_tasks"], 1)[0]["task_id"]
    posted = []
    with db.db_conn() as conn:
        before = conn.execute("SELECT count(*) FROM annotation_versions WHERE task_id = %s", (task_id,)).fetchone()[0]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def drop_first(route):
            posted.append(route.request.post_data_json)
            if len(posted) == 1:
                if server_accepted:
                    assert route.fetch().status == 200
                route.abort("connectionreset")
            else:
                route.continue_()

        page.route("**/api/admin/cross-checks/*/decision", drop_first)
        open_editor(page, site)
        page.locator("#ccConfirmDecision").click()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionError")).to_contain_text("Retry will resend", timeout=10000)
        for selector in ["[data-editor-index='0']", "#ccDecisionReason", "#ccDecision-secondary", "#ccSceneOverride"]:
            expect(page.locator(selector)).to_be_disabled()
        expect(page.locator("#ccCopyDraft")).to_be_enabled()
        expect(page.locator("#ccConfirmDecision")).to_have_text("Retry same decision")
        page.locator("#ccBackToList").click()
        expect(page.locator("#ccReviewPanel")).to_be_visible()
        page.locator('[data-view="overview"]').click()
        expect(page.locator("#crossChecksView")).to_be_visible()
        page.locator("#refreshButton").click()
        expect(page.locator("[data-editor-index='0']")).to_have_value("Local corrected manuscript")
        expect(page.locator("[data-editor-index='0']")).to_be_disabled()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccReviewBody")).to_contain_text("Adjudication is complete", timeout=15000)
        browser.close()
    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert published_text(task_id) == "Local corrected manuscript"
    with db.db_conn() as conn:
        after = conn.execute("SELECT count(*) FROM annotation_versions WHERE task_id = %s", (task_id,)).fetchone()[0]
    assert after == before + 1


def test_rejected_decision_can_be_corrected_as_new_operation(cross_check_site):
    site = cross_check_site
    task_id = queue_awaiting(site["client"], site["seed_tasks"], 1)[0]["task_id"]
    posted = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def reject_first(route):
            posted.append(route.request.post_data_json)
            if len(posted) == 1:
                route.fulfill(status=400, json={"error": "Please correct this manuscript"})
            else:
                route.continue_()

        page.route("**/api/admin/cross-checks/*/decision", reject_first)
        open_editor(page, site)
        page.locator("#ccConfirmDecision").click()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionError")).to_contain_text("Please correct")
        expect(page.locator("[data-editor-index='0']")).to_be_enabled()
        page.locator("[data-editor-index='0']").fill("Corrected after rejection")
        page.locator("#ccConfirmDecision").click()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccReviewBody")).to_contain_text("Adjudication is complete", timeout=15000)
        browser.close()
    assert len(posted) == 2
    assert posted[0]["operation_id"] != posted[1]["operation_id"]
    assert published_text(task_id) == "Corrected after rejection"


@pytest.mark.parametrize("refresh_fails", [False, True], ids=["latest-result", "reload-fails"])
def test_conflicting_edited_draft_remains_copyable(cross_check_site, refresh_fails):
    site = cross_check_site
    round_id = queue_awaiting(site["client"], site["seed_tasks"], 1)[0]["round_id"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        open_editor(page, site)
        _, headers = _admin_login(site["client"])
        detail = site["client"].get(f"/api/admin/cross-checks/{round_id}", headers=headers).json
        other = site["client"].post(
            f"/api/admin/cross-checks/{round_id}/decision", headers=headers,
            json=decision_body(detail, decision="secondary", reason="Another admin resolved first"),
        )
        assert other.status_code == 200
        if refresh_fails:
            page.route(f"**/api/admin/cross-checks/{round_id}", lambda route: route.fulfill(status=500, json={"error": "Read temporarily failed"}), times=1)
        page.locator("#ccConfirmDecision").click()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccRetainedDraft")).to_contain_text("Local corrected manuscript", timeout=15000)
        expect(page.locator("#ccRetainedDraft")).to_contain_text("My local adjudication reason")
        page.locator("#ccCopyRetainedDraft").click()
        expect(page.locator("#toastRegion")).to_contain_text("draft copied")
        copied = json.loads(page.evaluate("() => navigator.clipboard.readText()"))
        assert copied["round_id"] == round_id
        assert copied["decision"] == "edited"
        assert copied["base"] == "original"
        assert copied["reason"] == "My local adjudication reason"
        assert copied["segments"][0]["text"] == "Local corrected manuscript"
        assert len(copied["segments"]) == 2
        if refresh_fails:
            page.locator("[data-cc-retry-round]").click()
        expect(page.locator("#ccReviewBody")).to_contain_text("Adjudication is complete", timeout=10000)
        expect(page.locator("#ccRetainedDraft")).to_be_visible()
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.locator("#ccBackToList").click()
        expect(page.locator("#ccRetainedDraft")).to_be_visible()
        browser.close()


def test_declining_refresh_discard_keeps_editable_draft(cross_check_site):
    site = cross_check_site
    queue_awaiting(site["client"], site["seed_tasks"], 1)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        open_editor(page, site)
        dialogs = []
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.locator("#refreshButton").click()
        expect(page.locator("#refreshButton")).to_be_enabled(timeout=10000)
        assert dialogs
        expect(page.locator("[data-editor-index='0']")).to_have_value("Local corrected manuscript")
        expect(page.locator("[data-editor-index='0']")).to_be_enabled()
        expect(page.locator("#ccDecisionReason")).to_have_value("My local adjudication reason")
        page.locator("#ccDecision-original").click()
        expect(page.locator("#ccDecision-edited")).to_be_checked()
        expect(page.locator("[data-editor-index='0']")).to_have_value("Local corrected manuscript")
        browser.close()


@pytest.mark.parametrize("refresh_fails", [False, True], ids=["latest-settings", "reload-fails"])
def test_settings_conflict_preserves_inputs_until_explicit_resave(cross_check_site, refresh_fails):
    site = cross_check_site
    posted = []
    expected_percent = "12.50" if refresh_fails else "10.00"
    expected_reason = "Updated while reads failed" if refresh_fails else "Keep my sampling reason"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login_admin(page, site["url"])
        open_cross_checks(page)
        page.locator("#ccSamplingButton").click()
        expect(page.locator("#ccSettingsSave")).to_be_enabled(timeout=10000)
        page.locator("#ccSamplingEnabled").check()
        page.locator("#ccSamplingPercent").fill("10.00")
        page.locator("#ccSamplingReason").fill("Keep my sampling reason")
        _, headers = _admin_login(site["client"])
        settings = site["client"].get("/api/admin/cross-check-settings", headers=headers).json
        changed = site["client"].put("/api/admin/cross-check-settings", headers=headers, json={
            "operation_id": str(uuid.uuid4()), "expected_revision": settings["revision"],
            "enabled": False, "sampling_rate_bps": 2500, "reason": "Another admin changed settings",
        })
        assert changed.status_code == 200
        fail_next_read = refresh_fails

        def observe(route):
            nonlocal fail_next_read
            if route.request.method == "PUT":
                posted.append(route.request.post_data_json)
            elif fail_next_read:
                fail_next_read = False
                route.fulfill(status=500, json={"error": "Settings read failed"})
                return
            route.continue_()

        page.route("**/api/admin/cross-check-settings", observe)
        page.locator("#ccSettingsSave").click()
        expect(page.locator("#ccSettingsError")).to_contain_text("changed elsewhere")
        if refresh_fails:
            expect(page.locator("#ccSettingsSave")).to_be_disabled()
            page.locator("#ccSamplingPercent").fill(expected_percent)
            page.locator("#ccSamplingReason").fill(expected_reason)
            page.locator('#ccSettingsDialog [data-close-dialog]').first.click()
            page.locator("#ccSamplingButton").click()
        expect(page.locator("#ccSettingsStale")).to_contain_text("Server: disabled, 25%")
        expect(page.locator("#ccSettingsStale")).to_contain_text(f"Your draft: enabled, {expected_percent}%")
        expect(page.locator("#ccSamplingPercent")).to_have_value(expected_percent)
        expect(page.locator("#ccSamplingReason")).to_have_value(expected_reason)
        expect(page.locator("#ccSamplingEnabled")).to_be_checked()
        assert len(posted) == 1
        page.locator("#ccSettingsSave").click()
        expect(page.locator("#ccSettingsDialog")).not_to_be_visible(timeout=10000)
        browser.close()
    assert len(posted) == 2
    assert posted[0]["operation_id"] != posted[1]["operation_id"]
    assert posted[1]["expected_revision"] == changed.json["revision"]
    current = site["client"].get("/api/admin/cross-check-settings", headers=headers).json
    assert current["enabled"] is True
    assert current["sampling_rate_bps"] == (1250 if refresh_fails else 1000)


def test_history_tab_switch_allows_reload_and_ignores_old_response(cross_check_site):
    site = cross_check_site
    queue_awaiting(site["client"], site["seed_tasks"], 1)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, site["url"], "bob")
        page.goto(site["url"] + "/completed.html")
        expect(page.locator("#resultCount")).to_contain_text("shown", timeout=10000)
        page.evaluate("""() => {
            const originalFetch = window.fetch;
            let count = 0;
            window.fetch = (...args) => {
                const promise = originalFetch(...args);
                if (String(args[0]).includes('/api/cross-checks/mine') && ++count === 1) {
                    return promise.then(() => new Promise(resolve => {
                        window.releaseOldCrossChecks = () => resolve(new Response(
                            JSON.stringify({items: [], next_cursor: null}),
                            {status: 200, headers: {'Content-Type': 'application/json'}}
                        ));
                    }));
                }
                return promise;
            };
        }""")
        page.locator("#tabCrossChecks").click()
        page.wait_for_function("() => typeof window.releaseOldCrossChecks === 'function'")
        page.locator("#tabCompleted").click()
        page.locator("#tabCrossChecks").click()
        expect(page.locator("[data-cross-check-round]")).to_have_count(1, timeout=10000)
        page.evaluate("() => window.releaseOldCrossChecks()")
        expect(page.locator("#ccResultCount")).to_have_text("1 loaded")
        expect(page.locator("[data-cross-check-round]")).to_have_count(1)
        page.locator("#tabCompleted").click()
        page.locator("#tabCrossChecks").click()
        expect(page.locator("[data-cross-check-round]")).to_have_count(1, timeout=10000)
        browser.close()
