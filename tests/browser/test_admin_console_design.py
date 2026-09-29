"""Administrator workflows in the React implementation of the reference layout."""

from __future__ import annotations

import os
import re
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
from tests.browser.cross_check_helpers import login_admin, queue_awaiting
from tests.browser.admin_helpers import choose, navigate
from tests.test_admin_repository import _complete_next, _make_user


def capture(page, tmp_path, name):
    directory = Path(os.environ.get("ADMIN_UI_ARTIFACTS", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name), full_page="drawer" not in name)


def test_directory_filters_sort_export_and_drawer(provenance_site, tmp_path):
    alice, _ = _make_user("Alice")
    _complete_next(alice)
    _make_user("Bob")
    _make_user("Carla")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        login_admin(page, provenance_site["url"])
        navigate(page, "annotators")
        table = page.get_by_test_id("annotator-table")
        rows = table.locator(".ant-table-row")
        expect(rows).to_have_count(3)
        table.get_by_role("columnheader", name="Current annotations").click()
        table.get_by_role("columnheader", name="Current annotations").click()
        expect(rows.first).to_contain_text("Alice")
        page.get_by_role("textbox", name="Search annotators").fill("bob")
        expect(rows).to_have_count(1)
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Export visible CSV").click()
        csv = Path(downloaded.value.path()).read_text(encoding="utf-8-sig")
        assert (
            csv == '"annotator","current","last_active","status"\n"Bob","0","","never"'
        )
        page.get_by_role("textbox", name="Search annotators").fill("no-such-annotator")
        expect(table).to_contain_text("No annotators match these filters.")
        page.get_by_role("textbox", name="Search annotators").fill("")
        page.get_by_role("group", name="Activity", exact=True).get_by_role("button", name=re.compile("^Never active")).click()
        expect(rows).to_have_count(2)
        expect(table).not_to_contain_text("Alice")
        page.get_by_role("group", name="Activity", exact=True).get_by_role("button", name=re.compile("^All")).click()
        capture(page, tmp_path, "annotators-desktop.png")
        page.get_by_role("button", name="View Alice", exact=True).click()
        expect(page.locator(".admin-annotator-drawer")).to_be_visible()
        expect(page.locator("#annotatorCurrent")).to_have_text("1")
        expect(page.locator("#scopeMode")).to_be_enabled()
        capture(page, tmp_path, "annotator-drawer.png")
        page.reload()
        expect(page.locator(".admin-annotator-drawer")).to_be_visible()
        expect(page.locator("#annotatorCurrent")).to_have_text("1")
        page.keyboard.press("Escape")
        expect(page.locator(".admin-annotator-drawer")).not_to_be_visible()
        assert "annotator=" not in page.url
        assert not errors
        browser.close()


def test_overview_alerts_range_and_theme(cross_check_site, tmp_path):
    queue_awaiting(cross_check_site["client"], cross_check_site["seed_tasks"], 2)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        login_admin(page, cross_check_site["url"])
        expect(page.locator("#kpiTotalAudio")).to_have_text("2")
        expect(page.locator("#queueTotal")).to_have_text("0")
        expect(page.locator("#overviewAlerts")).to_contain_text(
            "2 cross-check rounds awaiting review"
        )
        with page.expect_response(
            lambda response: "/api/admin/timeseries?" in response.url
            and "bucket=week" in response.url
        ) as response:
            choose(page, '[aria-label="Date range"]', "Last 90 days")
        assert response.value.status == 200
        expect(page.get_by_text("Submissions per week")).to_be_visible()
        choose(page, '[aria-label="Date range"]', "Last 30 days")
        expect(page.get_by_text("Submissions per day")).to_be_visible()
        capture(page, tmp_path, "overview-desktop.png")
        page.emulate_media(color_scheme="dark")
        expect(page.locator("html")).to_have_attribute("data-admin-mode", "dark")
        capture(page, tmp_path, "overview-dark.png")
        page.emulate_media(color_scheme="light")
        page.locator("#overviewAlerts").get_by_role(
            "button", name="Review queue"
        ).click()
        expect(page.locator("#crossChecksView")).to_be_visible()
        expect(page.locator("[data-cc-round]")).to_have_count(2)
        page.get_by_role("tab", name="Passed", exact=True).click()
        expect(page.locator("[data-cc-round]")).to_have_count(0)
        page.get_by_role("tab", name="Awaiting review", exact=True).click()
        expect(page.locator("[data-cc-round]")).to_have_count(2)
        button = page.locator("[data-cc-round]").first
        assert button.evaluate("el => getComputedStyle(el).borderRadius") == "8px"
        capture(page, tmp_path, "cross-checks-desktop.png")
        assert not errors
        browser.close()


def test_mobile_navigation_filters_and_task_drawer(provenance_site, tmp_path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        login_admin(page, provenance_site["url"])
        expect(page.locator("#kpiTotalAudio")).to_have_text("2")
        page.emulate_media(color_scheme="dark")
        page.emulate_media(color_scheme="light")
        expect(page.locator("#queueTotal")).to_have_text("2")
        capture(page, tmp_path, "overview-mobile.png")
        navigate(page, "corpus")
        expect(page.get_by_test_id("corpus-page")).to_be_visible()
        rows = page.get_by_test_id("corpus-tbody").locator(".ant-table-row")
        expect(rows).to_have_count(2)
        page.get_by_text("Advanced filters", exact=True).click()
        page.get_by_role("group", name="Scene", exact=True).get_by_role("button", name=re.compile("^Airport")).click()
        page.get_by_role("group", name="Confidence", exact=True).get_by_role("button", name=re.compile("^High")).click()
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text("audio-000.wav")
        page.get_by_role("button", name="Reset filters").click()
        expect(rows).to_have_count(2)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        rows.filter(has_text="audio-000.wav").get_by_role(
            "button", name="View", exact=True
        ).click()
        drawer = page.locator(".admin-task-drawer")
        expect(drawer).to_be_visible()
        expect(page.locator("#taskDetail")).to_contain_text("audio-000.wav")
        capture(page, tmp_path, "task-drawer-mobile.png")
        drawer.get_by_role("button", name="Close", exact=True).first.click()
        expect(drawer).not_to_be_visible()
        assert not errors
        browser.close()
