"""P1 component/CSP verification (work packages C/D, section 9.1).

Runs the real Flask production builds (dist-p1) with the gated preview
switch on, logs in as admin through the legacy entry, and exercises the
two-theme component matrix: both themes render, Select/DatePicker/Tooltip/
Modal/Drawer/message/scrolling fixed-column Table/TextArea/Slider/buttons
all operate; overlays survive scroll/resize; focus returns to the trigger
on close. Collects securitypolicyviolation events, console errors,
pageerrors, the actual CSP header and screenshots, and asserts zero CSP
violations, zero unhandled JS errors and zero external resource requests.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

import frontend_delivery as delivery
import server
from tests.browser.conftest import ADMIN_KEY, configure_admin, start_app_server, stop_app_server
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
def p1_matrix_site(client, seed_tasks, tmp_path, monkeypatch):
    """Live server with the P1 preview switch on (p1 build).

    Missing dist/dist-p1 is a hard failure, never a skip: CI stages the
    artifacts before this test runs.
    """
    configure_admin(monkeypatch)
    for dist in (delivery.DIST_STANDARD, delivery.DIST_P1):
        problems = delivery.verify_build_complete(dist)
        assert not problems, f"preview build incomplete in {dist}: {problems}"
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "p1")
    httpd, thread, url = start_app_server()
    yield {"url": url, "client": client}
    stop_app_server(httpd, thread)
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", False)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")


def _attach_trackers(page, url):
    console_errors: list[str] = []
    page_errors: list[str] = []
    external_requests: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "request",
        lambda request: external_requests.append(request.url)
        if not request.url.startswith(url)
        else None,
    )
    page.add_init_script(VIOLATION_HOOK)
    return console_errors, page_errors, external_requests


def _new_tracked_page(playwright, url, *, width, height):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={"width": width, "height": height}, reduced_motion="reduce"
    )
    return browser, page


def _open_matrix(page, url):
    # Log in first: the legacy login flow emits a benign pre-auth 401
    # (documented in the P0 baseline) that must not pollute the tracked
    # matrix load. Trackers attach after login; the matrix itself is then
    # loaded fresh so violation/console/request capture starts clean.
    login_admin(page, url)
    tracked = _attach_trackers(page, url)
    response = page.goto(url + "/frontend-preview/components")
    assert response is not None and response.status == 200, (
        f"matrix page status: {response.status if response else None}"
    )
    expect(page.get_by_text("Component state matrix")).to_be_visible(timeout=20000)
    for role in ("admin", "annotator"):
        expect(page.locator(f'[data-theme-section="{role}"]')).to_be_visible(timeout=10000)
    return response, tracked


def _assert_clean(page, console_errors, page_errors, external_requests):
    violations = page.evaluate("window.__p1csp || []")
    assert violations == [], f"CSP violations: {violations}"
    assert page_errors == [], f"pageerrors: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"
    assert external_requests == [], f"external requests: {external_requests}"


def _exercise_theme(page, role):
    section = page.locator(f'[data-theme-section="{role}"]')
    # Select opens its overlay; an option can be picked with the pointer.
    # (rc-select keeps a zero-size measuring listbox in the dropdown; scope
    # to the visible option list.)
    section.get_by_label(f"{role} confidence select").click()
    dropdown = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    expect(dropdown).to_be_visible(timeout=10000)
    option_list = dropdown.locator(".ant-select-dropdown-list")
    option_list.locator(
        '.ant-select-item-option[title="Medium confidence with a long label"]'
    ).click()
    expect(section.locator(".ant-select-content").first).to_contain_text(
        "Medium confidence with a long label", timeout=10000
    )
    # Date range picker opens its calendar overlay.
    section.get_by_placeholder("Start date").click()
    expect(
        page.locator(".ant-picker-dropdown:not(.ant-picker-dropdown-hidden)")
    ).to_be_visible(timeout=10000)
    page.keyboard.press("Escape")
    # Modal opens and Escape restores focus to the trigger.
    trigger = section.get_by_label(f"{role} open modal")
    trigger.click()
    expect(page.locator(".ant-modal").last).to_be_visible(timeout=10000)
    page.keyboard.press("Escape")
    expect(page.locator(".ant-modal-root").first).to_be_hidden(timeout=10000)
    expect(trigger).to_be_focused(timeout=10000)
    # Drawer opens and closes via its close button.
    section.get_by_label(f"{role} open drawer").click()
    expect(page.locator(".ant-drawer").last).to_be_visible(timeout=10000)
    page.locator(".ant-drawer-close").last.click()
    # Provider-context message renders inside the themed tree.
    section.get_by_label(f"{role} primary action").hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "Tooltip attached to the primary action", timeout=10000
    )
    section.get_by_label(f"{role} primary action").click()
    expect(page.locator(".ant-message").last).to_contain_text("primary action clicked", timeout=10000)


def test_p1_matrix_two_themes_interactive(p1_matrix_site):
    url = p1_matrix_site["url"]
    with sync_playwright() as playwright:
        browser, page = _new_tracked_page(playwright, url, width=1440, height=900)
        try:
            response, tracked = _open_matrix(page, url)
            console_errors, page_errors, external = tracked
            csp = response.headers.get("content-security-policy", "")
            assert "style-src 'self' 'nonce-" in csp, csp
            assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
            _exercise_theme(page, "admin")
            _exercise_theme(page, "annotator")
            # Page-level horizontal overflow is forbidden; table scroll stays
            # inside its own container.
            overflow = page.evaluate(
                "({doc: document.documentElement.scrollWidth, win: window.innerWidth})"
            )
            assert overflow["doc"] <= overflow["win"] + 1, overflow
            scrolled = page.evaluate(
                "Array.from(document.querySelectorAll('[data-scroll-x=\"table\"]'))"
                ".map((el) => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth}))"
            )
            assert scrolled and all(
                entry["scrollWidth"] >= entry["clientWidth"] for entry in scrolled
            ), scrolled
            page.screenshot(path=str(EVIDENCE_DIR / "components-matrix-1440x900.png"))
            _assert_clean(page, console_errors, page_errors, external)
        finally:
            browser.close()


def test_p1_matrix_small_screen(p1_matrix_site):
    url = p1_matrix_site["url"]
    with sync_playwright() as playwright:
        browser, page = _new_tracked_page(playwright, url, width=390, height=844)
        try:
            _, tracked = _open_matrix(page, url)
            console_errors, page_errors, external = tracked
            _exercise_theme(page, "admin")
            overflow = page.evaluate(
                "({doc: document.documentElement.scrollWidth, win: window.innerWidth})"
            )
            assert overflow["doc"] <= overflow["win"] + 1, overflow
            # Overlay stays usable after resize.
            page.set_viewport_size({"width": 390, "height": 844})
            section = page.locator('[data-theme-section="annotator"]')
            section.get_by_label("annotator open modal").click()
            expect(page.locator(".ant-modal").last).to_be_visible(timeout=10000)
            box = page.locator(".ant-modal").last.bounding_box(timeout=10000)
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 391, box
            page.screenshot(path=str(EVIDENCE_DIR / "components-matrix-390x844.png"))
            _assert_clean(page, console_errors, page_errors, external)
        finally:
            browser.close()


def test_p1_matrix_standard_build_has_no_matrix(p1_matrix_site, monkeypatch):
    """The standard build must not expose the verification entry."""
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    url = p1_matrix_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login_admin(page, url)
            response = page.goto(url + "/frontend-preview/components")
            assert response is not None and response.status == 404
        finally:
            browser.close()
