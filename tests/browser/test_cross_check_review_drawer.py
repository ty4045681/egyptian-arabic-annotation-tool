"""Segment review, draft recovery, audio controls, and next-round navigation."""

import json
import re
from playwright.sync_api import expect, sync_playwright
import db
from tests.browser.cross_check_helpers import (
    login_admin,
    open_cross_checks,
    queue_awaiting,
    screenshot,
)
from tests.browser.admin_helpers import choose


def open_round(page, site):
    login_admin(page, site["url"])
    open_cross_checks(page)
    page.locator("[data-cc-round]").first.click()
    expect(page.locator(".cc-review-drawer")).to_be_visible()
    expect(page.locator("[data-review-segment]")).to_have_count(2)


def test_segment_choices_drafts_and_full_publication(cross_check_site):
    site = cross_check_site
    queued = queue_awaiting(site["client"], site["seed_tasks"], 1)
    sent = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "**/api/admin/cross-checks/*/decision",
            lambda route: (
                sent.append(route.request.post_data_json),
                route.continue_(),
            ),
        )
        open_round(page, site)
        drawer = page.locator(".cc-review-drawer")
        expect(drawer.get_by_text("Whole transcript decision", exact=True)).to_have_count(0)
        expect(drawer.locator('[id^="ccDecision-"]')).to_have_count(0)
        first = drawer.locator('[data-review-segment="1"]')
        second = drawer.locator('[data-review-segment="2"]')
        expect(first).to_contain_text("Unresolved")
        first.get_by_role("button", name="Use B", exact=False).click()
        expect(first.locator(".cc-segment-final")).to_contain_text("x0 x1")
        expect(first.locator('[data-cc-side="original"]')).to_contain_text("w0 w1")
        first.get_by_role("button", name="Undo", exact=True).click()
        expect(first.locator(".cc-segment-final")).to_contain_text("Unresolved")
        first.get_by_role("button", name="Use B", exact=False).click()
        second.get_by_role("button", name="Edit", exact=False).click()
        expect(drawer.get_by_role("radio", name="Start from original", exact=True)).to_have_count(0)
        second.get_by_role("textbox").fill("Final edited second segment")
        second.get_by_role("checkbox", name="Bad quality").uncheck()
        page.locator("#ccDecisionReason").fill(
            "Reviewed both segments against the audio"
        )
        page.locator("#ccSaveDraft").click()
        expect(page.locator(".ant-message")).to_contain_text(
            "saved in this browser tab"
        )
        page.get_by_role("button", name="Refresh review", exact=True).click()
        expect(second.get_by_role("textbox")).to_have_value(
            "Final edited second segment"
        )
        assert sent == []
        page.locator("#ccBackToList").click()
        expect(drawer).to_have_count(0)
        page.locator("[data-cc-round]").first.click()
        expect(second.get_by_role("textbox")).to_have_value(
            "Final edited second segment"
        )
        page.reload()
        expect(second.get_by_role("textbox")).to_have_value(
            "Final edited second segment"
        )
        expect(first.locator(".cc-segment-final")).to_contain_text("x0 x1")
        second.get_by_role("textbox").fill("Saved on close")
        page.locator("#ccBackToList").click()
        expect(
            page.get_by_role("dialog", name="Discard unsaved changes?")
        ).to_be_visible()
        page.get_by_role("button", name="Save draft and leave").click()
        expect(drawer).to_have_count(0)
        page.locator("[data-cc-round]").first.click()
        expect(second.get_by_role("textbox")).to_have_value("Saved on close")
        screenshot(page, "review-drawer-mixed-draft.png")
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionSummary")).to_contain_text("Publish 2 segments")
        assert sent == []
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccReviewBody")).to_contain_text(
            "Adjudication is complete", timeout=15000
        )
        assert len(sent) == 1
        assert sent[0]["decision"] == "edited"
        assert sent[0]["base"] == "original"
        assert sent[0]["segments"][0]["text"] == queued[0]["secondary"]
        assert sent[0]["segments"][1]["text"] == "Saved on close"
        assert sent[0]["segments"][1]["exclude_from_training"] is False
        assert (
            page.evaluate(
                "Object.keys(sessionStorage).filter(key => key.startsWith('admin-crosscheck-review:')).length"
            )
            == 0
        )
        assert not errors
        browser.close()
    with db.db_conn() as conn:
        actual = conn.execute(
            "SELECT s.segment_id, s.text, s.exclude_from_training FROM segments s JOIN annotation_tasks t ON t.current_published_version_id=s.version_id WHERE t.id=%s ORDER BY s.segment_id",
            (queued[0]["task_id"],),
        ).fetchall()
    assert actual == [(1, queued[0]["secondary"], False), (2, "Saved on close", False)]


def test_audio_segment_navigation_filtering_and_keyboard(cross_check_site):
    queue_awaiting(cross_check_site["client"], cross_check_site["seed_tasks"], 1)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        open_round(page, cross_check_site)
        drawer = page.locator(".cc-review-drawer")
        audio = page.locator("#ccAudio")
        expect(audio).to_have_js_property("readyState", 4)
        drawer.get_by_role("checkbox", name="Only differences", exact=True).check()
        expect(drawer.locator("[data-review-segment]")).to_have_count(1)
        drawer.get_by_role("checkbox", name="Only differences", exact=True).uncheck()
        expect(drawer.locator("[data-review-segment]")).to_have_count(2)
        choose(page, '[aria-label="Playback speed"]', "1.5×")
        expect(audio).to_have_js_property("playbackRate", 1.5)
        drawer.get_by_role("button", name="Play segment 1", exact=True).click()
        expect(audio).to_have_js_property("paused", False)
        audio.evaluate("player => player.currentTime = 4.99")
        expect(audio).to_have_js_property("paused", True)
        expect(audio).to_have_js_property("currentTime", 5)
        page.locator("#ccReviewSegments").click(position={"x": 2, "y": 2})
        page.keyboard.press("j")
        expect(drawer.locator('[data-review-segment="2"]')).to_have_class(
            re.compile("cc-review-active")
        )
        page.keyboard.press("k")
        page.keyboard.press("2")
        expect(
            drawer.locator('[data-review-segment="1"] .cc-segment-final')
        ).to_contain_text("x0 x1")
        page.keyboard.press("e")
        expect(drawer.locator('[data-review-segment="1"] textarea')).to_be_focused()
        drawer.locator('[data-review-segment="1"] textarea').fill(
            "Typing 12 j k does not pick a different manuscript"
        )
        expect(drawer.locator('[data-review-segment="1"] textarea')).to_have_value(
            "Typing 12 j k does not pick a different manuscript"
        )
        page.locator("#ccSaveDraft").click()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert drawer.evaluate("el => el.scrollWidth <= el.clientWidth")
        page.emulate_media(color_scheme="dark")
        expect(page.locator("html")).to_have_attribute("data-admin-mode", "dark")
        screenshot(page, "review-drawer-mobile-dark.png")
        audio.evaluate("player => window.reviewPlayer = player")
        page.locator("#ccBackToList").click()
        expect(audio).to_have_count(0)
        assert page.evaluate(
            "window.reviewPlayer.paused && window.reviewPlayer.getAttribute('src') === null"
        )
        browser.close()


def test_confirm_can_open_next_awaiting_round(cross_check_site):
    queued = queue_awaiting(
        cross_check_site["client"], cross_check_site["seed_tasks"], 2
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        open_round(page, cross_check_site)
        current = next(
            item["round_id"] for item in queued if item["round_id"] in page.url
        )
        next_id = next(
            item["round_id"] for item in queued if item["round_id"] != current
        )
        page.locator('[data-review-segment="1"]').get_by_role("button", name="Use B", exact=False).click()
        page.locator("#ccDecisionReason").fill(
            "Use the second transcript after listening"
        )
        page.get_by_role("checkbox", name="Open next after saving").check()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionSummary")).to_be_visible()
        page.locator("#ccConfirmDecision").click()
        expect(page).to_have_url(re.compile(next_id), timeout=15000)
        expect(page.locator("#ccDecisionReason")).to_have_value("")
        expect(
            page.get_by_role("checkbox", name="Open next after saving")
        ).to_be_checked()
        expect(page.locator(".cc-review-progress")).to_contain_text("1 unresolved")
        expect(page.locator('[data-review-segment="1"] .cc-segment-final')).to_contain_text("Unresolved")
        browser.close()


def test_saved_review_drafts_are_cleared_on_logout(cross_check_site):
    queue_awaiting(cross_check_site["client"], cross_check_site["seed_tasks"], 1)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        open_round(page, cross_check_site)
        page.locator('[data-review-segment="1"]').get_by_role(
            "button", name="Use B", exact=False
        ).click()
        page.locator("#ccSaveDraft").click()
        expect(page.locator(".ant-message")).to_contain_text(
            "saved in this browser tab"
        )
        assert (
            page.evaluate(
                "Object.keys(sessionStorage).filter(key => key.startsWith('admin-crosscheck-review:')).length"
            )
            == 1
        )
        page.locator("#ccBackToList").click()
        expect(page.locator(".cc-review-drawer")).to_have_count(0)
        page.get_by_role("button", name="Log out", exact=True).click()
        expect(page.locator("#adminKey")).to_be_visible()
        assert (
            page.evaluate(
                "Object.keys(sessionStorage).filter(key => key.startsWith('admin-crosscheck-review:')).length"
            )
            == 0
        )
        browser.close()
