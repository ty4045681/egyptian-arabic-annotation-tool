"""P1 Corpus representative page (work package E, section 9.1).

Real Flask production build (standard) + isolated database fixtures over
the actual admin API: matched_count/duration, date-exclusive upper bound,
cursor paging, filter reset, late-response attribution and append-failure
retention. No production data, no skips when dist is missing.
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

import frontend_delivery as delivery
import server
from tests.browser.conftest import configure_admin, start_app_server, stop_app_server
from tests.browser.conftest import write_silence_wav
from tests.browser.cross_check_helpers import login_admin

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "docs" / "plans" / "frontend-rebuild-p1" / "screenshots"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

VIOLATION_HOOK = (
    "window.__p1csp = [];"
    "document.addEventListener('securitypolicyviolation', (e) => {"
    "  window.__p1csp.push(e.violatedDirective + '|' + e.blockedURI);"
    "});"
)


@pytest.fixture
def p1_corpus_site(client, seed_tasks, tmp_path, monkeypatch):
    configure_admin(monkeypatch)
    for dist in (delivery.DIST_STANDARD, delivery.DIST_P1):
        problems = delivery.verify_build_complete(dist)
        assert not problems, f"preview build incomplete in {dist}: {problems}"
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    seed_tasks(55, folder="corpus-p1")
    audio_dir = Path(server.app.config["AUDIO_DIR"])
    write_silence_wav(audio_dir / "corpus-p1" / "audio-000.wav")
    httpd, thread, url = start_app_server()
    yield {"url": url, "client": client}
    stop_app_server(httpd, thread)
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", False)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")


def _open_corpus(playwright, url, *, width=1440, height=900):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script(VIOLATION_HOOK)
    return browser, page, console_errors, page_errors


def _login_and_open(page, url):
    login_admin(page, url)
    page.add_init_script(VIOLATION_HOOK)
    response = page.goto(url + "/admin/preview?view=corpus")
    assert response is not None and response.status == 200
    expect(page.get_by_test_id("corpus-page")).to_be_visible(timeout=20000)
    return response


def test_p1_corpus_real_data_cursor_and_reset(p1_corpus_site):
    url = p1_corpus_site["url"]
    with sync_playwright() as playwright:
        browser, page, console_errors, page_errors = _open_corpus(playwright, url)
        try:
            _login_and_open(page, url)
            # Matched totals come from the response, not the loaded rows.
            expect(page.get_by_test_id("corpus-matched")).to_contain_text("55 tasks", timeout=15000)
            expect(page.locator('[data-testid="corpus-tbody"] tr.ant-table-row')).to_have_count(50, timeout=15000)
            page.get_by_test_id("corpus-load-more").click()
            expect(page.locator('[data-testid="corpus-tbody"] tr.ant-table-row')).to_have_count(55, timeout=15000)
            expect(page.get_by_test_id("corpus-matched")).to_contain_text("55 tasks")
            # Filter to nothing, then reset back to the full list.
            page.get_by_test_id("corpus-q").fill("no-such-file-xyz-123")
            expect(page.get_by_test_id("corpus-state")).to_contain_text(
                "No tasks match these filters.", timeout=15000
            )
            page.get_by_role("button", name="Reset filters").first.click()
            # Reset restores the default filter set; the cached pages for
            # that key come back (possibly both appended pages), so assert
            # the totals and a full first page rather than an exact count.
            expect(page.get_by_test_id("corpus-matched")).to_contain_text("55 tasks", timeout=15000)
            page.wait_for_function(
                "document.querySelectorAll('[data-testid=\"corpus-tbody\"] tr.ant-table-row').length >= 50",
                timeout=15000,
            )
            overflow = page.evaluate(
                "({doc: document.documentElement.scrollWidth, win: window.innerWidth})"
            )
            assert overflow["doc"] <= overflow["win"] + 1, overflow
            page.screenshot(path=str(EVIDENCE_DIR / "corpus-1440x900.png"))
            assert page.evaluate("window.__p1csp || []") == []
            assert page_errors == []
        finally:
            browser.close()


def test_p1_corpus_date_exclusive_upper_bound(p1_corpus_site):
    url = p1_corpus_site["url"]
    seen: list[str] = []
    with sync_playwright() as playwright:
        browser, page, _, _ = _open_corpus(playwright, url)
        try:
            page.on("response", lambda response: seen.append(response.url)
                    if "/api/admin/tasks" in response.url else None)
            _login_and_open(page, url)
            expect(page.get_by_test_id("corpus-matched")).to_contain_text("tasks", timeout=15000)
            assert seen, "no /api/admin/tasks request observed"
            first = seen[0]
            today = datetime.date.today()
            # UI end date is inclusive; the API `to` is the +1-day exclusive
            # bound in Asia/Shanghai.
            assert f"to={today + datetime.timedelta(days=1)}" in first, first
            assert "timezone=Asia%2FShanghai" in first or "timezone=Asia/Shanghai" in first, first
        finally:
            browser.close()


def test_p1_corpus_late_response_does_not_overwrite(p1_corpus_site):
    url = p1_corpus_site["url"]
    with sync_playwright() as playwright:
        browser, page, _, _ = _open_corpus(playwright, url)
        try:
            delayed = {"once": True}

            def handle(route):
                if delayed["once"] and "cursor=" not in route.request.url:
                    delayed["once"] = False
                    time.sleep(1.5)
                route.continue_()

            page.route("**/api/admin/tasks*", handle)
            _login_and_open(page, url)
            # While the first response is still in flight, narrow the list;
            # the late arrival must not overwrite the current results.
            page.get_by_test_id("corpus-q").fill("no-such-file-xyz-123")
            expect(page.get_by_test_id("corpus-state")).to_contain_text(
                "No tasks match these filters.", timeout=15000
            )
            page.wait_for_timeout(2000)
            expect(page.get_by_test_id("corpus-state")).to_contain_text(
                "No tasks match these filters.", timeout=5000
            )
            assert page.locator('[data-testid="corpus-tbody"] tr.ant-table-row').count() == 0
        finally:
            browser.close()


def test_p1_corpus_append_failure_keeps_rows(p1_corpus_site):
    url = p1_corpus_site["url"]
    with sync_playwright() as playwright:
        browser, page, _, _ = _open_corpus(playwright, url)
        try:
            failing = {"active": False}

            def handle(route):
                if failing["active"] and "cursor=" in route.request.url:
                    route.fulfill(
                        status=500,
                        body='{"error": "injected append failure"}',
                        content_type="application/json",
                    )
                else:
                    route.continue_()

            page.route("**/api/admin/tasks*", handle)
            _login_and_open(page, url)
            expect(page.locator('[data-testid="corpus-tbody"] tr.ant-table-row')).to_have_count(50, timeout=15000)
            failing["active"] = True
            page.get_by_test_id("corpus-load-more").click()
            expect(page.get_by_test_id("corpus-append-error")).to_be_visible(timeout=20000)
            # Loaded rows survive the append failure.
            assert page.locator('[data-testid="corpus-tbody"] tr.ant-table-row').count() == 50
            failing["active"] = False
            page.get_by_test_id("corpus-append-retry").click()
            expect(page.locator('[data-testid="corpus-tbody"] tr.ant-table-row')).to_have_count(55, timeout=20000)
        finally:
            browser.close()


def test_p1_corpus_small_screen(p1_corpus_site):
    url = p1_corpus_site["url"]
    with sync_playwright() as playwright:
        browser, page, _, page_errors = _open_corpus(playwright, url, width=390, height=844)
        try:
            _login_and_open(page, url)
            expect(page.locator('[data-testid="corpus-tbody"] tr.ant-table-row').first).to_be_visible(timeout=15000)
            overflow = page.evaluate(
                "({doc: document.documentElement.scrollWidth, win: window.innerWidth})"
            )
            assert overflow["doc"] <= overflow["win"] + 1, overflow
            page.screenshot(path=str(EVIDENCE_DIR / "corpus-390x844.png"))
            assert page_errors == []
        finally:
            browser.close()
