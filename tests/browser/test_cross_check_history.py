"""My cross-checks history tab and read-only submission details."""
from __future__ import annotations

from playwright.sync_api import expect, sync_playwright

from tests.browser.cross_check_helpers import (
    login_annotator,
    queue_awaiting,
    screenshot,
    seed_originals,
)
from tests.test_admin_api import _admin_login
from tests.test_api import login
from tests.test_cross_check_admin import decision_body
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import words


def test_my_cross_checks_list_and_readonly_detail(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    rounds = queue_awaiting(client, cross_check_site["seed_tasks"], 1, original=words(20), secondary=words(20))
    round_id = rounds[0]["round_id"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        login_annotator(page, url, "bob")
        page.goto(url + "/completed.html?tab=cross-checks")
        expect(page.locator("[data-cross-check-round]")).to_be_visible(timeout=15000)
        expect(page.locator("#ccResultCount")).to_contain_text("loaded")
        expect(page.locator("#crossCheckPanel [data-correct]")).to_have_count(0)
        page.locator("[data-cross-check-round]").first.click()
        expect(page.locator("#detailDialog")).to_be_visible(timeout=10000)
        expect(page.locator("#detailBody audio")).to_have_js_property("readyState", 4, timeout=10000)
        expect(page.locator("#detailBody audio")).to_have_js_property("error", None)
        expect(page.locator("#correctButton")).to_be_hidden()
        expect(page.locator("#detailBody")).to_contain_text(words(20).split()[0])
        screenshot(page, "history-cross-check-detail.png")
        page.locator("#closeTopButton").click()
        page.goto(url + f"/completed.html?tab=cross-checks&round={round_id}")
        expect(page.locator("#detailDialog")).to_be_visible(timeout=15000)
        expect(page.locator("#correctButton")).to_be_hidden()
        browser.close()


def test_adjudicated_history_does_not_show_decision_direction(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    rounds = queue_awaiting(client, cross_check_site["seed_tasks"], 1)
    round_id = rounds[0]["round_id"]
    from tests.test_admin_api import _admin_login
    _, headers = _admin_login(client)
    detail = client.get(f"/api/admin/cross-checks/{round_id}", headers=headers)
    assert detail.status_code == 200, detail.json
    decided = client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        headers=headers,
        json=decision_body(detail.json, decision="secondary", reason="Use the second transcript"),
    )
    assert decided.status_code == 200, decided.json
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, url, "bob")
        page.goto(url + f"/completed.html?tab=cross-checks&round={round_id}")
        expect(page.locator("#detailDialog")).to_be_visible(timeout=15000)
        expect(page.locator("#detailBody")).to_contain_text("administrator has processed")
        expect(page.locator("#detailBody")).not_to_contain_text("Use the second transcript")
        expect(page.locator("#detailBody")).not_to_contain_text("your transcript was accepted")
        expect(page.locator("#correctButton")).to_be_hidden()
        browser.close()


def test_completed_correct_blocked_while_cross_check_open(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    seed_originals(client, cross_check_site["seed_tasks"], 1, words(12))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    claimed = client.post("/api/assignment/claim", json={"source_scene": "airport"})
    assert claimed.status_code == 200
    client.post("/api/logout", json={})
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        login_annotator(page, url, "alice")
        page.goto(url + "/completed.html")
        expect(page.locator("[data-correct]").first).to_be_visible(timeout=15000)
        page.locator("[data-correct]").first.click()
        expect(page.locator(".toast")).to_contain_text("under cross-check", timeout=10000)
        browser.close()
