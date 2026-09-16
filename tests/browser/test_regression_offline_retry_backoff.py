"""A persistent connection failure must not create a hot save retry loop."""
from playwright.sync_api import expect, sync_playwright

from tests.browser.test_regression_review_only_save import live_site
from tests.browser.test_regression_review_save_queue import open_assignment


def test_persistent_offline_save_uses_bounded_retry_backoff(live_site):
    url, _ = live_site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = open_assignment(browser, url, "browser-persistent-offline")
            requests = []

            def offline(route):
                if route.request.method == "PATCH":
                    requests.append(route.request.post_data_json)
                    route.abort("connectionreset")
                else:
                    route.continue_()

            page.route("**/api/assignment/current", offline)
            page.locator("textarea[data-text='0']").fill("keep this unsaved text during an outage")
            page.locator("#saveButton").click()
            page.wait_for_timeout(1100)
            assert 1 <= len(requests) <= 2, "Persistent failure must back off instead of immediately repeating saves: " + str(len(requests))
            expect(page.locator("textarea[data-text='0']")).to_have_value("keep this unsaved text during an outage")
            expect(page.locator("#saveState")).to_have_attribute("data-state", "error")
        finally:
            browser.close()
