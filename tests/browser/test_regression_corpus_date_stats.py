"""The corpus count must describe the list under the chosen date range."""
from playwright.sync_api import expect, sync_playwright

import db


def test_empty_date_filtered_corpus_does_not_show_unfiltered_stock_count(provenance_site):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(provenance_site["url"] + "/admin?view=corpus&range=custom&from=2000-01-01&to=2000-01-02")
            page.locator("#adminKey").fill(provenance_site["admin_key"])
            page.locator("#loginButton").click()
            expect(page.locator("#adminApp")).to_be_visible(timeout=10000)
            expect(page.locator("#corpusTaskState")).to_contain_text("No tasks", timeout=10000)
            expect(page.locator("#corpusTaskBody tr")).to_have_count(0)
            expect(page.locator("#corpusMatchedStats")).to_contain_text("0 tasks", timeout=5000)
        finally:
            browser.close()


def test_partial_date_filtered_total_uses_same_predicate_as_list(provenance_site):
    # The overview intentionally ignores activity dates for stock metrics.
    # Corpus match totals therefore need an exact shared list-filter aggregate,
    # not an empty-list exception or the length of the current page.
    with db.db_conn() as conn:
        conn.execute("UPDATE annotation_tasks SET created_at='2000-01-01 12:00:00+08' WHERE id=%s", (provenance_site["tasks"][0],))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(provenance_site["url"] + "/admin?view=corpus&range=custom&from=2000-01-01&to=2000-01-02")
            page.locator("#adminKey").fill(provenance_site["admin_key"])
            page.locator("#loginButton").click()
            expect(page.locator("#corpusTaskBody tr")).to_have_count(1, timeout=10000)
            expect(page.locator("#corpusMatchedStats strong")).to_have_text("1 tasks")
            expect(page.locator("#corpusMatchedStats")).to_contain_text("10s source audio")
        finally:
            browser.close()


def test_corpus_match_total_is_not_capped_at_first_page(provenance_site, seed_tasks):
    seed_tasks(49, folder="extra-matches")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(provenance_site["url"] + "/admin?view=corpus&range=all")
            page.locator("#adminKey").fill(provenance_site["admin_key"])
            page.locator("#loginButton").click()
            expect(page.locator("#corpusTaskBody tr")).to_have_count(50, timeout=10000)
            expect(page.locator("#corpusMatchedStats strong")).to_have_text("51 tasks")
            page.locator("#loadMoreCorpusTasks").click()
            expect(page.locator("#corpusTaskBody tr")).to_have_count(51, timeout=10000)
            expect(page.locator("#corpusMatchedStats strong")).to_have_text("51 tasks")
        finally:
            browser.close()
