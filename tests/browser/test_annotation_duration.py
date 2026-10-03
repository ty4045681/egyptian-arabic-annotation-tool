"""Completion displays trainable credit while retaining the audio duration."""

from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.test_scene_workflow import _login_annotator, _select_airport_and_claim


def test_completed_page_shows_credit_after_vad_and_bad_quality(live_site):
    url, task_id = live_site
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE segments SET end_s = start_s + 3, duration = 3
               WHERE version_id IN (SELECT id FROM annotation_versions WHERE task_id = %s)""",
            (task_id,),
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login_annotator(page, url, "duration-browser")
            _select_airport_and_claim(page)
            page.locator('textarea[data-text="0"]').fill("Trainable speech")
            page.locator('button[data-quality="1"]').click()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/assignment/current/complete"),
            ) as completed:
                page.locator("#completeButton").click()
            assert completed.value.status == 200, completed.value.text()
            expect(page.get_by_role("heading", name="Task completed", exact=True)).to_be_visible()
            page.goto(url + "/completed.html")
            expect(page.get_by_role("columnheader", name="Credited time")).to_be_visible()
            row = page.locator("#records tbody tr").first
            expect(row.locator("td").nth(3)).to_have_text("3.0 s")
            row.get_by_role("button", name="View", exact=True).click()
            expect(page.locator(".detail-meta")).to_contain_text("3.0 s credited")
            expect(page.locator(".detail-meta")).to_contain_text("10.0 s audio")
        finally:
            browser.close()
