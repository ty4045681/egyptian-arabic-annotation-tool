"""Repeatable browser acceptance for the attachment's admin filter interactions."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

import annotation_repository as repo
from tests.browser.admin_helpers import choose, navigate
from tests.browser.conftest import configure_admin, start_app_server, stop_app_server
from tests.browser.cross_check_helpers import login_admin
from tests.test_admin_repository import _make_user, _complete_next
from tests.test_regression_admin_metadata import source


@pytest.fixture
def filter_site(client, seed_tasks, monkeypatch):
    configure_admin(monkeypatch)
    tasks = seed_tasks(55, folder="filters-demo")
    for index, task in enumerate(tasks):
        source(task, "airport" if index < 30 else "clinic", "high" if index % 2 == 0 else "medium", "filter-batch")
    bob, _ = _make_user("filter-bob")
    _complete_next(bob)
    alice, _ = _make_user("filter-alice")
    repo.claim(alice["fence"], source_confidence="high")
    httpd, thread, url = start_app_server()
    yield url
    stop_app_server(httpd, thread)


def choice(page, group, name):
    return page.get_by_role("group", name=group, exact=True).get_by_role("button", name=re.compile(rf"^{re.escape(name)}(?:\s|$)"))


def reset(page):
    page.get_by_role("button", name="Reset filters", exact=True).click()


def test_tasks_multi_scene_assignee_summary_cursor_reset_and_dates(filter_site):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        login_admin(page, filter_site)
        navigate(page, "corpus")
        summary = page.get_by_test_id("corpus-matched")
        rows = page.get_by_test_id("corpus-tbody").locator("tr.ant-table-row")
        expect(summary).to_contain_text("55 tasks")
        expect(rows).to_have_count(50)
        choice(page, "Scene", "Airport").click()
        expect(summary).to_contain_text("30 tasks")
        choice(page, "Scene", "Clinic").click()
        expect(summary).to_contain_text("55 tasks")
        expect(rows).to_have_count(50)
        page.get_by_test_id("corpus-load-more").click()
        expect(rows).to_have_count(55)
        choice(page, "Confidence", "High").click()
        expect(summary).to_contain_text("28 tasks")
        expect(rows).to_have_count(28)
        choose(page, '[aria-label="Assignee"]', "Unassigned")
        expect(summary).to_contain_text("27 tasks")
        page.get_by_test_id("corpus-q").fill("audio-052")
        expect(summary).to_contain_text("1 tasks")
        expect(rows).to_contain_text("audio-052.wav")
        page.get_by_role("button", name="Remove Search: audio-052", exact=True).click()
        expect(summary).to_contain_text("27 tasks")
        page.get_by_role("button", name="Remove Airport", exact=True).click()
        expect(summary).to_contain_text("13 tasks")
        expect(choice(page, "Scene", "Airport")).to_have_attribute("aria-pressed", "false")
        page.reload()
        expect(summary).to_contain_text("13 tasks")
        expect(choice(page, "Scene", "Clinic")).to_have_attribute("aria-pressed", "true")
        with page.expect_download() as download:
            page.get_by_role("button", name="Export visible CSV").click()
        csv = Path(download.value.path()).read_text(encoding="utf-8-sig")
        assert len(csv.splitlines()) == 14
        assert "audio-052.wav" in csv
        reset(page)
        expect(summary).to_contain_text("55 tasks")
        expect(rows).to_have_count(50)
        choose(page, '[data-testid="corpus-range"]', "Custom range")
        page.get_by_test_id("corpus-from").fill("2030-01-02")
        page.get_by_test_id("corpus-to").fill("2030-01-01")
        page.get_by_test_id("corpus-apply-dates").click()
        expect(page.get_by_role("alert")).to_contain_text("Choose a valid start and end date")
        expect(summary).to_contain_text("55 tasks")
        page.get_by_test_id("corpus-to").fill("2030-01-03")
        with page.expect_request(lambda req: "/api/admin/tasks?" in req.url and "to=2030-01-04" in req.url):
            page.get_by_test_id("corpus-apply-dates").click()
        expect(summary).to_contain_text("0 tasks")
        expect(page.get_by_test_id("corpus-state")).to_contain_text("No tasks match")
        reset(page)
        expect(summary).to_contain_text("55 tasks")
        page.get_by_test_id("corpus-q").fill("must-not-apply-after-reset")
        reset(page)
        page.wait_for_timeout(400)
        expect(page.get_by_test_id("corpus-q")).to_have_value("")
        expect(summary).to_contain_text("55 tasks")
        assert not errors
        browser.close()


def test_overview_annotators_quality_and_activity_use_live_removable_filters(filter_site):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        login_admin(page, filter_site)
        choice(page, "Scene", "Airport").click()
        expect(page.locator("#kpiTotalAudio")).to_have_text("30")
        choice(page, "Scene", "Clinic").click()
        expect(page.locator("#kpiTotalAudio")).to_have_text("55")
        page.get_by_role("button", name="Remove Airport", exact=True).click()
        expect(page.locator("#kpiTotalAudio")).to_have_text("25")
        reset(page)
        expect(page.locator("#kpiTotalAudio")).to_have_text("55")
        navigate(page, "annotators")
        people = page.get_by_test_id("annotator-table").locator("tr.ant-table-row")
        expect(people).to_have_count(2)
        choice(page, "Activity", "Active (7d)").click()
        expect(people).to_have_count(1)
        expect(people).to_contain_text("filter-bob")
        page.go_back()
        expect(people).to_have_count(2)
        page.get_by_role("textbox", name="Search annotators").fill("filter-alice")
        expect(people).to_have_count(1)
        people.get_by_role("button", name="View", exact=True).click()
        expect(page.locator(".admin-annotator-drawer")).to_be_visible()
        page.locator(".admin-annotator-drawer").get_by_role("button", name="Close", exact=True).click()
        expect(page.get_by_role("textbox", name="Search annotators")).to_have_value("filter-alice")
        expect(people).to_have_count(1)
        reset(page)
        expect(people).to_have_count(2)
        navigate(page, "quality")
        rows = page.locator("#qualityView tr.ant-table-row")
        expect(rows).to_have_count(1)
        choose(page, '[aria-label="Annotator"]', "filter-alice")
        expect(rows).to_have_count(0)
        page.get_by_role("button", name="Remove Annotator: filter-alice").click()
        expect(rows).to_have_count(1)
        choice(page, "Signal", "Long-held assignment").click()
        expect(rows).to_have_count(0)
        reset(page)
        expect(rows).to_have_count(1)
        navigate(page, "activity")
        choose(page, '[aria-label="Action type"]', "Admin auth success")
        expect(page.locator("#activityView tr.ant-table-row")).to_have_count(1)
        page.get_by_role("textbox", name="Search activity").fill("no-such-admin-action")
        expect(page.locator("#activityView tr.ant-table-row")).to_have_count(0)
        page.get_by_role("button", name="Remove Search: no-such-admin-action").click()
        expect(page.locator("#activityView tr.ant-table-row")).to_have_count(1)
        reset(page)
        expect(page.get_by_role("button", name="Remove Admin auth success")).to_have_count(0)
        browser.close()


def test_filter_cards_fit_mobile_and_search_shortcut_respects_editing(filter_site):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        login_admin(page, filter_site)
        for view in ["overview", "annotators", "corpus", "quality", "cross-checks", "activity"]:
            navigate(page, view)
            card = page.locator(".admin-filter-card")
            expect(card).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
            search = card.locator(".af-search input")
            page.locator(".admin-topbar").click(position={"x": 160, "y": 20})
            page.keyboard.press("/")
            expect(search).to_be_focused()
            page.keyboard.type("/")
            expect(search).to_have_value("/")
            reset(page)
            expect(search).to_have_value("")
        browser.close()
