"""Real HTTP/browser regressions for read-only review flags and ambiguous saves."""
from playwright.sync_api import expect, sync_playwright

import db


def open_assignment(browser, url, username):
    page = browser.new_page(viewport={"width": 1366, "height": 1000})
    page.goto(url + "/login.html")
    page.locator("#username").fill(username)
    page.locator("#joinBtn").click()
    expect(page.locator("#claimButton")).to_be_visible(timeout=15000)
    page.locator("#claimButton").click()
    expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=10000)
    return page


def test_review_write_disabled_still_saves_transcription(live_site, monkeypatch):
    monkeypatch.setenv("ANNOTATION_SCENE_REVIEW_WRITE", "0")
    url, task = live_site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = open_assignment(browser, url, "browser-review-disabled")
            page.locator("textarea[data-text='0']").fill("human text while scene review is read-only")
            with page.expect_response(lambda r: r.url.endswith("/api/assignment/current") and r.request.method == "PATCH", timeout=8000) as saved:
                page.locator("#saveButton").click()
            response = saved.value
            assert response.status == 200, response.text()
            assert "scene_review" not in response.request.post_data_json
            page.reload()
            expect(page.locator("textarea[data-text='0']")).to_have_value("human text while scene review is read-only", timeout=10000)
            with db.db_conn() as conn:
                assert conn.execute("SELECT count(*) FROM scene_reviews r JOIN annotation_versions v ON v.id=r.version_id WHERE v.task_id=%s", (task,)).fetchone()[0] == 0
        finally:
            browser.close()


def test_claim_scope_does_not_restrict_truthful_review_labels(live_site):
    url, task = live_site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url + "/login.html")
            page.locator("#username").fill("browser-airport-only-review")
            page.locator("#joinBtn").click()
            expect(page.locator("#claimButton")).to_be_visible(timeout=15000)
            with db.db_conn() as conn:
                user = conn.execute("SELECT id FROM annotators WHERE username='browser-airport-only-review'").fetchone()[0]
                conn.execute("INSERT INTO annotator_scene_scopes(user_id,mode) VALUES(%s,'restricted') ON CONFLICT(user_id) DO UPDATE SET mode='restricted'", (user,))
                conn.execute("INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'airport')", (user,))
            page.reload()
            expect(page.locator("#claimButton")).to_be_visible(timeout=10000)
            expect(page.locator("#idleScenePicker")).to_contain_text("机场")
            expect(page.locator("#idleScenePicker")).not_to_contain_text("购物")
            page.locator("#claimButton").click()
            panel = page.locator("#sceneReviewPanel")
            panel.get_by_role("button", name="确认", exact=True).click()
            expect(panel.locator('input[value="shopping"]')).to_have_count(1, timeout=3000)
            panel.locator('input[value="shopping"]').check()
            page.locator("textarea[data-text='0']").fill("actual shopping content")
            with page.expect_response(lambda r: r.url.endswith("/api/assignment/current") and r.request.method == "PATCH", timeout=8000) as saved:
                page.locator("#saveButton").click()
            assert saved.value.status == 200, saved.value.text()
            with db.db_conn() as conn:
                labels = conn.execute("SELECT l.scene_code FROM scene_review_labels l JOIN scene_reviews r ON r.id=l.review_id JOIN annotation_versions v ON v.id=r.version_id WHERE v.task_id=%s AND NOT r.superseded ORDER BY l.scene_code", (task,)).fetchall()
                assert labels == [("shopping",)]
                assert conn.execute("SELECT scene_code FROM task_sources WHERE task_id=%s", (task,)).fetchall() == [("airport",)]
        finally:
            browser.close()


def test_lost_save_response_retries_identical_body_then_saves_new_review(live_site):
    url, task = live_site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = open_assignment(browser, url, "browser-review-retry")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            bodies = []
            server_responses = []

            def intercept(route):
                if route.request.method != "PATCH":
                    route.continue_()
                    return
                bodies.append(route.request.post_data_json)
                if len(bodies) == 1:
                    # The DB commit succeeds; only the browser loses the response.
                    server_responses.append(route.fetch().status)
                    route.abort("connectionreset")
                else:
                    route.continue_()

            page.route("**/api/assignment/current", intercept)
            panel = page.locator("#sceneReviewPanel")
            panel.get_by_role("button", name="确认", exact=True).click()
            panel.locator('input[value="airport"]').check()
            page.locator("textarea[data-text='0']").fill("first committed text")
            page.locator("#saveButton").click()
            expect(page.locator("#saveState")).to_have_attribute("data-state", "error", timeout=8000)
            assert server_responses == [200]

            panel.get_by_role("button", name="调整/多场景", exact=True).click()
            panel.locator('input[value="shopping"]').check()
            with page.expect_response(lambda r: r.url.endswith("/api/assignment/current") and r.request.method == "PATCH", timeout=8000) as retried:
                page.locator("#saveButton").click()
            assert retried.value.status == 200, retried.value.text()
            assert len(bodies) >= 2
            assert bodies[1] == bodies[0], "An ambiguous prior save must retry with its exact original operation ID and payload"
            expect(page.locator("#saveState")).to_have_attribute("data-state", "saved", timeout=15000)
            page.reload()
            expect(page.locator('#sceneReviewPanel input[value="shopping"]')).to_be_checked(timeout=10000)
            expect(page.locator('#sceneReviewPanel input[value="airport"]')).to_be_checked()
            with db.db_conn() as conn:
                latest = conn.execute("SELECT r.status FROM scene_reviews r JOIN annotation_versions v ON v.id=r.version_id WHERE v.task_id=%s ORDER BY r.review_no DESC LIMIT 1", (task,)).fetchone()
                assert latest == ("mixed",)
            assert not errors, errors
        finally:
            browser.close()
