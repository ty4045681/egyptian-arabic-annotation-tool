"""Annotator workspace: claim, blind render, submit, abandon, outbox replay."""
from __future__ import annotations

from playwright.sync_api import expect, sync_playwright

from tests.browser.cross_check_helpers import (
    SECRET_ORIGINAL,
    assignment_leak_watch,
    claim_next,
    console_errors,
    fill_first_segment,
    login_annotator,
    screenshot,
    seed_originals,
    select_airport,
)
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import words


def test_cross_check_claim_is_blind_and_submits_awaiting_review(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    seed_originals(client, cross_check_site["seed_tasks"], 1, SECRET_ORIGINAL)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    leaks = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        errors = console_errors(page)
        assignment_leak_watch(page, leaks)
        login_annotator(page, url, "bob-workspace")
        select_airport(page)
        claim_next(page)
        expect(page.locator("#crossCheckBadge")).to_be_visible(timeout=10000)
        expect(page.locator("#crossCheckBadge")).to_contain_text("Independent annotation")
        expect(page.locator("#audio")).to_have_js_property("readyState", 4, timeout=10000)
        expect(page.locator("#audio")).to_have_js_property("error", None)
        body = page.locator("body").inner_text()
        assert SECRET_ORIGINAL not in body
        assigned = [payload for payload in leaks if payload.get("assigned") and payload.get("mode") == "cross_check"]
        assert assigned
        for payload in assigned:
            blob = str(payload)
            assert SECRET_ORIGINAL not in blob
            assert "original_annotator" not in payload
            assert "diff_ops" not in payload
            assert "word_difference_rate" not in payload
        fill_first_segment(page, " ".join(f"x{i}" if i < 11 else f"w{i}" for i in range(100)))
        page.locator("#completeButton").click()
        expect(page.locator("body")).to_contain_text("awaiting admin review", timeout=15000)
        expect(page.locator("body")).not_to_contain_text("published as a new")
        screenshot(page, "workspace-submitted-awaiting.png")
        assert not errors, errors
        browser.close()


def test_identical_cross_check_is_submitted_passed(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    text = words(20)
    seed_originals(client, cross_check_site["seed_tasks"], 1, text)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login_annotator(page, url, "bob-pass")
        select_airport(page)
        claim_next(page)
        expect(page.locator("#crossCheckBadge")).to_be_visible()
        fill_first_segment(page, text)
        page.locator("#completeButton").click()
        expect(page.locator("body")).to_contain_text("Cross-check submitted — passed.", timeout=15000)
        expect(page.locator(".state.success")).not_to_contain_text("Task completed")
        screenshot(page, "workspace-submitted-passed.png")
        browser.close()


def test_cross_check_abandon_uses_dedicated_copy(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    seed_originals(client, cross_check_site["seed_tasks"], 1, words(12))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        dialogs = []
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        login_annotator(page, url, "bob-abandon")
        select_airport(page)
        claim_next(page)
        page.locator("#abandonButton").click()
        expect(page.locator(".state h1")).to_contain_text("Cross-check released", timeout=15000)
        assert dialogs
        assert "Release this cross-check?" in dialogs[0]
        assert "saved draft will remain" not in dialogs[0]
        browser.close()


def test_refresh_restores_nonempty_cross_check_draft(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    seed_originals(client, cross_check_site["seed_tasks"], 1, words(12))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, url, "bob-restore")
        select_airport(page)
        claim_next(page)
        page.locator("textarea[data-text='0']").fill("saved independent draft")
        with page.expect_response(
            lambda response: response.url.endswith("/api/assignment/current") and response.request.method == "PATCH",
            timeout=8000,
        ):
            page.locator("#saveButton").click()
        expect(page.locator("#saveState")).to_have_text("Saved", timeout=10000)
        page.reload()
        expect(page.locator("#crossCheckBadge")).to_be_visible(timeout=15000)
        expect(page.locator("textarea[data-text='0']")).to_have_value("saved independent draft", timeout=10000)
        screenshot(page, "workspace-cross-check-restored.png")
        browser.close()


def test_complete_response_loss_replays_same_operation(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    seed_originals(client, cross_check_site["seed_tasks"], 1, words(12))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    seen = {"count": 0, "ids": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, url, "bob-replay")
        select_airport(page)
        claim_next(page)
        fill_first_segment(page, words(12))

        def drop_first(route):
            if route.request.method == "POST" and route.request.url.endswith("/api/assignment/current/complete"):
                seen["count"] += 1
                body = route.request.post_data_json or {}
                seen["ids"].append(body.get("operation_id"))
                if seen["count"] == 1:
                    route.fetch()
                    route.abort("connectionreset")
                    return
            route.continue_()

        page.route("**/api/assignment/current/complete", drop_first)
        page.locator("#completeButton").click()
        expect(page.locator("body")).to_contain_text("Cross-check submitted", timeout=20000)
        assert seen["count"] >= 2
        assert len(set(seen["ids"])) == 1
        browser.close()
