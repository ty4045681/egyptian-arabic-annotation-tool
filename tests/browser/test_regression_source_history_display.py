"""Historical claim evidence must not hide other current source scenes."""
from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.test_regression_review_only_save import live_site


def test_historical_claim_still_displays_all_current_source_scenes(live_site):
    url, task = live_site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url + "/login.html")
            page.locator("#username").fill("browser-source-history")
            page.locator("#joinBtn").click()
            expect(page.locator("#claimButton")).to_be_visible(timeout=15000)
            page.locator("#claimButton").click()
            expect(page.locator("#metadataBanner")).to_contain_text("Airport", timeout=10000)
            with db.db_conn() as conn:
                batch = conn.execute("SELECT batch_id FROM task_sources WHERE task_id=%s", (task,)).fetchone()[0]
                conn.execute("UPDATE task_sources SET is_current=false WHERE task_id=%s", (task,))
                for key, scene, confidence in [("revised-airport", "airport", "low"), ("other-shopping", "shopping", "high")]:
                    conn.execute("INSERT INTO task_sources(task_id,batch_id,record_key,scene_code,confidence,confidence_basis,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                                 (task, batch, key, scene, confidence, "current evidence " + key, "1" * 64))
            page.reload()
            details = page.locator("#metadataDisclosure")
            expect(details).to_be_visible(timeout=10000)
            details.locator("summary").click()
            expect(details).to_contain_text("Source at claim time")
            expect(details).to_contain_text("current evidence revised-airport")
            expect(details).to_contain_text("current evidence other-shopping")
        finally:
            browser.close()
