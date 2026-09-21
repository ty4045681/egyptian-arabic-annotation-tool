"""Admin cross-check queue, sampling settings, and adjudication."""
from __future__ import annotations

from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.cross_check_helpers import (
    login_admin,
    open_cross_checks,
    queue_awaiting,
    screenshot,
    seed_originals,
)
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import words


def test_overview_opens_all_time_awaiting_queue(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    rounds = queue_awaiting(client, cross_check_site["seed_tasks"], 1)
    round_id = rounds[0]["round_id"]
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE cross_check_rounds SET created_at = now() - interval '40 days' WHERE id = %s",
            (round_id,),
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login_admin(page, url)
        expect(page.locator("#overviewCrossCheckPanel")).to_contain_text("All-time, all scenes", timeout=10000)
        expect(page.locator("#kpiAnnotatedDuration").locator("xpath=..")).to_contain_text("published annotated audio")
        page.locator('[data-open-cross-checks="awaiting_review"]').first.click()
        expect(page.locator("#crossChecksView")).to_be_visible(timeout=15000)
        expect(page.locator("#ccListBody")).to_contain_text(str(rounds[0]["task_id"])[:8], timeout=15000)
        expect(page.locator("#ccSummaryScope")).to_contain_text("All-time, all scenes")
        screenshot(page, "admin-awaiting-queue.png")
        browser.close()


def test_sampling_settings_percent_to_bps(cross_check_site):
    url = cross_check_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_admin(page, url)
        open_cross_checks(page)
        page.locator("#ccSamplingButton").click()
        expect(page.locator("#ccSettingsDialog")).to_be_visible()
        page.locator("#ccSamplingEnabled").check()
        page.locator("#ccSamplingPercent").fill("0.01")
        page.locator("#ccSamplingReason").fill("Enable one basis point for tests")
        page.locator("#ccSettingsSave").click()
        expect(page.locator("#toastRegion")).to_contain_text("Sampling settings saved", timeout=10000)
        page.locator("#ccSamplingButton").click()
        expect(page.locator("#ccSamplingPercent")).to_have_value("0.01", timeout=5000)
        page.locator("#ccSamplingPercent").fill("10")
        page.locator("#ccSamplingReason").fill("Set ten percent")
        page.locator("#ccSettingsSave").click()
        expect(page.locator("#toastRegion")).to_contain_text("Sampling settings saved", timeout=10000)
        browser.close()
    with db.db_conn() as conn:
        enabled, bps = conn.execute(
            "SELECT enabled, sampling_rate_bps FROM cross_check_settings WHERE id = 1"
        ).fetchone()
    assert enabled is True
    assert bps == 1000


def test_use_cross_check_decision(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    rounds = queue_awaiting(client, cross_check_site["seed_tasks"], 1)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        login_admin(page, url)
        open_cross_checks(page)
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        page.locator("#ccDecision-secondary").check()
        page.locator("#ccDecisionReason").fill("The second submission matches the audio")
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionSummary")).to_be_visible()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("body")).to_contain_text("Adjudicated", timeout=15000)
        screenshot(page, "admin-decision-secondary.png")
        browser.close()


def test_edit_and_publish_sends_full_segments(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    queue_awaiting(client, cross_check_site["seed_tasks"], 1)
    posted = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def capture(route):
            if route.request.method == "POST" and route.request.url.endswith("/decision"):
                posted["body"] = route.request.post_data_json
            route.continue_()

        page.route("**/api/admin/cross-checks/*/decision", capture)
        login_admin(page, url)
        open_cross_checks(page)
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        expect(page.locator("#ccDecisionForm")).to_be_visible(timeout=15000)
        page.locator("#ccDecision-edited").check()
        page.locator('input[name="ccEditBase"][value="original"]').check()
        expect(page.locator("#ccEditorFields")).to_be_visible()
        page.locator("[data-editor-index='0']").fill("Administrator corrected transcript")
        page.locator("#ccDecisionReason").fill("Corrected the wording after listening to the audio")
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionSummary")).to_contain_text("Edit and publish")
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("body")).to_contain_text("Adjudicated", timeout=15000)
        screenshot(page, "admin-decision-edited.png")
        browser.close()
    assert posted["body"]["decision"] == "edited"
    assert posted["body"]["base"] == "original"
    assert "admin_key" not in posted["body"]
    assert len(posted["body"]["segments"]) >= 2
    assert posted["body"]["segments"][0]["text"] == "Administrator corrected transcript"


def test_cancel_in_progress_and_not_awaiting(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    seed_originals(client, cross_check_site["seed_tasks"], 1, words(12))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    from tests.test_api import login
    login(client, "bob-cancel")
    claimed = client.post("/api/assignment/claim", json={"source_scene": "airport"})
    assert claimed.status_code == 200, claimed.json
    round_id = claimed.json["cross_check"]["round_id"]
    client.post("/api/logout", json={})
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        login_admin(page, url)
        open_cross_checks(page, state="in_progress")
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        expect(page.locator("#ccCancelForm")).to_be_visible(timeout=15000)
        expect(page.locator("#ccDecisionForm")).to_have_count(0)
        page.locator("#ccCancelReason").fill("Release an interrupted cross-check")
        page.locator("#ccCancelButton").click()
        expect(page.locator("body")).to_contain_text("Cancelled", timeout=15000)
        browser.close()
    queued = queue_awaiting(client, cross_check_site["seed_tasks"], 1)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_admin(page, url)
        open_cross_checks(page, state="awaiting_review")
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        expect(page.locator("#ccDecisionForm")).to_be_visible(timeout=15000)
        expect(page.locator("#ccCancelForm")).to_have_count(0)
        browser.close()
    assert queued[0]["round_id"] != round_id


def test_decision_conflict_keeps_local_draft(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    rounds = queue_awaiting(client, cross_check_site["seed_tasks"], 1)
    round_id = rounds[0]["round_id"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_admin(page, url)
        open_cross_checks(page)
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        expect(page.locator("#ccDecisionForm")).to_be_visible()
        page.locator("#ccDecision-original").check()
        page.locator("#ccDecisionReason").fill("Keep original after listening")
        from tests.test_admin_api import _admin_login
        from tests.test_cross_check_admin import decision_body
        _, headers = _admin_login(client)
        detail = client.get(f"/api/admin/cross-checks/{round_id}", headers=headers).json
        other = client.post(
            f"/api/admin/cross-checks/{round_id}/decision",
            headers=headers,
            json=decision_body(detail, decision="secondary", reason="Other admin finished first"),
        )
        assert other.status_code == 200
        page.locator("#ccConfirmDecision").click()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("body")).to_contain_text("Adjudicated", timeout=15000)
        expect(page.locator("#toastRegion")).to_contain_text("changed", timeout=8000)
        screenshot(page, "admin-decision-conflict.png")
        browser.close()
