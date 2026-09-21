"""Annotator workspace: claim, blind render, submit, abandon, outbox replay."""
from __future__ import annotations

import pytest

from playwright.sync_api import expect, sync_playwright

from tests.browser.cross_check_helpers import (
    SECRET_ORIGINAL,
    assert_annotator_blind,
    assignment_leak_watch,
    claim_next,
    console_errors,
    fill_first_segment,
    login_annotator,
    screenshot,
    seed_originals,
    select_airport,
    write_wavs_for_tasks,
)
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import words
from tests.test_scene_claims import source


def prepare_task(site, cross_check):
    if cross_check:
        seed_originals(site["client"], site["seed_tasks"], 1, words(12))
    else:
        task_id = site["seed_tasks"](1)[0]
        source(task_id, "airport", "high")
        write_wavs_for_tasks([task_id])
    enable_cross_check(enabled=cross_check, sampling_rate_bps=10000)


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
        assert_annotator_blind(page)
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
        expect(page.locator(".state h1")).to_have_text("Task completed", timeout=15000)
        assert_annotator_blind(page)
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
        assert_annotator_blind(page)
        fill_first_segment(page, text)
        page.locator("#completeButton").click()
        expect(page.locator(".state h1")).to_have_text("Task completed", timeout=15000)
        assert_annotator_blind(page)
        screenshot(page, "workspace-submitted-passed.png")
        browser.close()


@pytest.mark.parametrize("cross_check", [False, True])
def test_task_abandon_uses_standard_copy(cross_check_site, cross_check):
    url = cross_check_site["url"]
    prepare_task(cross_check_site, cross_check)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        dialogs = []
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        login_annotator(page, url, "bob-abandon")
        select_airport(page)
        claim_next(page)
        page.locator("#abandonButton").click()
        expect(page.locator(".state h1")).to_contain_text("Task released", timeout=15000)
        assert dialogs
        assert dialogs[0] == "Release this task? Your current assignment will end without submitting it."
        assert_annotator_blind(page)
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
        page.locator("textarea[data-text='0']").fill("saved transcript draft")
        with page.expect_response(
            lambda response: response.url.endswith("/api/assignment/current") and response.request.method == "PATCH",
            timeout=8000,
        ):
            page.locator("#saveButton").click()
        expect(page.locator("#saveState")).to_have_text("Saved", timeout=10000)
        page.reload()
        assert_annotator_blind(page)
        expect(page.locator("textarea[data-text='0']")).to_have_value("saved transcript draft", timeout=10000)
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
        expect(page.locator(".state h1")).to_have_text("Task completed", timeout=20000)
        assert_annotator_blind(page)
        assert seen["count"] >= 2
        assert len(set(seen["ids"])) == 1
        browser.close()


@pytest.mark.parametrize("status", ["annotated", "skipped"])
def test_old_submission_flash_has_only_standard_feedback(cross_check_site, status):
    site = cross_check_site
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, site["url"], "old-flash")
        page.evaluate("""status => sessionStorage.setItem('annotator.crossCheckFlash',
            JSON.stringify({state:'awaiting_review',round_id:'old-round',status}))""", status)
        page.reload()
        expect(page.locator(".state h1")).to_have_text(
            "Task skipped" if status == "skipped" else "Task completed", timeout=10000,
        )
        assert_annotator_blind(page)
        assert page.evaluate("sessionStorage.getItem('annotator.crossCheckFlash')") is None
        browser.close()


@pytest.mark.parametrize("status", [400, 409])
def test_save_error_and_exported_draft_do_not_reveal_task_mode(cross_check_site, status):
    site = cross_check_site
    seed_originals(site["client"], site["seed_tasks"], 1, words(12))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, site["url"], "save-error")
        select_airport(page)
        claim_next(page)
        page.route("**/api/assignment/current", lambda route: route.fulfill(
            status=status, json={"error": "Cross-check round is no longer in progress"},
        ) if route.request.method == "PATCH" else route.continue_())
        page.locator('textarea[data-text="0"]').fill("My recoverable transcript")
        page.locator("#saveButton").click()
        if status == 409:
            expect(page.locator("#localConflictOverlay")).to_be_visible(timeout=10000)
            assert "cross_check" not in page.locator("#localConflictText").input_value()
        else:
            expect(page.locator(".toast")).to_contain_text("This task has changed", timeout=10000)
        exported = page.evaluate("""async () => {
            const draft = await AnnotationOffline.getWorkingDraft(state.user, state.assignment.task_id);
            return {storedMode:draft.mode, text:AnnotationOffline.exportText(draft)};
        }""")
        assert exported["storedMode"] == "cross_check"
        assert "My recoverable transcript" in exported["text"]
        assert "cross_check" not in exported["text"]
        assert "round_id" not in exported["text"]
        assert_annotator_blind(page)
        browser.close()


@pytest.mark.parametrize("cross_check", [False, True])
def test_skipping_uses_standard_feedback_and_history(cross_check_site, cross_check):
    site = cross_check_site
    prepare_task(site, cross_check)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login_annotator(page, site["url"], "skip-task")
        select_airport(page)
        claim_next(page)
        page.locator('.reason[data-reason="noisy"]').click()
        page.locator("#skipButton").click()
        expect(page.locator(".state h1")).to_have_text("Task skipped", timeout=15000)
        assert_annotator_blind(page)
        page.goto(site["url"] + "/completed.html")
        expect(page.locator("#skipCount")).to_have_text("1", timeout=10000)
        expect(page.locator("#records .status")).to_have_text("Skipped")
        assert_annotator_blind(page)
        browser.close()
