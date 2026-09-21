"""Unicode highlighting, XSS-as-text, large details, and player cleanup."""
from __future__ import annotations

from playwright.sync_api import expect, sync_playwright

from tests.browser.cross_check_helpers import (
    login_admin,
    open_cross_checks,
    queue_awaiting,
    screenshot,
)
from tests.test_cross_check_submit import words


def test_unicode_and_emoji_marks_use_code_point_spans(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    original = "e\u0301 😀 cat extra"
    secondary = "é 😀 dog extra"
    queue_awaiting(
        client, cross_check_site["seed_tasks"], 1,
        original=original, secondary=secondary,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login_admin(page, url)
        open_cross_checks(page)
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        expect(page.locator(".cc-transcript")).to_have_count(4, timeout=15000)
        original_text = page.locator('[data-cc-side="original"] .cc-transcript').first.inner_text()
        secondary_text = page.locator('[data-cc-side="secondary"] .cc-transcript').first.inner_text()
        assert "cat" in original_text
        assert "dog" in secondary_text
        marks = page.locator("mark.cc-mark")
        expect(marks.first).to_be_visible()
        mark_texts = marks.all_text_contents()
        assert any("cat" in text for text in mark_texts)
        assert any("dog" in text for text in mark_texts)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 8")
        screenshot(page, "admin-unicode-arabic-mobile.png")
        page.set_viewport_size({"width": 1440, "height": 1000})
        screenshot(page, "admin-unicode-compare.png")
        browser.close()


def test_xss_sample_is_plain_text(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    payload = "<script>alert(1)</script> hello"
    queue_awaiting(
        client, cross_check_site["seed_tasks"], 1,
        original=payload, secondary=payload + " world",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        dialogs = []
        page.on("dialog", lambda dialog: dialogs.append(dialog.message) or dialog.dismiss())
        login_admin(page, url)
        open_cross_checks(page)
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=15000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
        expect(page.locator(".cc-transcript").first).to_contain_text("<script>alert(1)</script>")
        assert page.locator(".cc-transcript script").count() == 0
        assert not dialogs
        browser.close()


def test_large_detail_and_difference_navigation(cross_check_site):
    client = cross_check_site["client"]
    url = cross_check_site["url"]
    original = words(3000)
    secondary = " ".join(f"x{i}" if i < 400 else f"w{i}" for i in range(3000))
    queue_awaiting(
        client, cross_check_site["seed_tasks"], 1,
        original=original, secondary=secondary,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        login_admin(page, url)
        open_cross_checks(page)
        expect(page.locator("[data-cc-round]")).to_be_visible(timeout=20000)
        page.locator("[data-cc-round]").first.click()
        expect(page.locator("#ccReviewTitle")).to_be_visible(timeout=20000)
        expect(page.locator("#ccNextDiff")).to_be_visible(timeout=20000)
        audio = page.locator("#ccAudio")
        expect(audio).to_have_js_property("readyState", 4, timeout=10000)
        expect(audio).to_have_js_property("error", None)
        page.locator("#ccNextDiff").click()
        expect(audio).to_have_js_property("paused", False)
        expect(audio).not_to_have_js_property("currentTime", 0)
        expect(page.locator("mark.cc-mark").first).to_be_visible()
        page.locator("#ccBackToList").click()
        expect(page.locator("#ccListPanel")).to_be_visible()
        expect(audio).to_have_js_property("paused", True)
        expect(audio).not_to_have_attribute("src")
        browser.close()


def test_sampling_percent_parser_in_browser(cross_check_site):
    url = cross_check_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        login_admin(page, url)
        values = page.evaluate(
            """() => ({
              ten: CrossCheck.samplingBpsFromPercent('10'),
              tiny: CrossCheck.samplingBpsFromPercent('0.01'),
              full: CrossCheck.samplingBpsFromPercent('100'),
              zero: CrossCheck.samplingBpsFromPercent('0'),
              rate: CrossCheck.formatWordDifferenceRate(0.11),
              missing: CrossCheck.formatWordDifferenceRate(null)
            })"""
        )
        browser.close()
    assert values == {
        "ten": 1000,
        "tiny": 1,
        "full": 10000,
        "zero": 0,
        "rate": "11%",
        "missing": "Not available",
    }
