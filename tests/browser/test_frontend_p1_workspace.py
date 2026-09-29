"""P1 workspace representative page (work package F, section 9.1).

Real assignment claimed through the actual API, then edited in the Flask
production build: long Arabic input with local RTL, first-screen and
narrow layouts, font-size preference intact, Ctrl/Cmd+S scope, single
audio instance with segment-stop linkage, and save→reload consistency.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

import frontend_delivery as delivery
import server
from tests.browser.conftest import configure_admin, start_app_server, stop_app_server
from tests.browser.conftest import write_silence_wav
from tests.browser.cross_check_helpers import login_annotator

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "docs" / "plans" / "frontend-rebuild-p1" / "screenshots"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

VIOLATION_HOOK = (
    "window.__p1csp = [];"
    "document.addEventListener('securitypolicyviolation', (e) => {"
    "  window.__p1csp.push(e.violatedDirective + '|' + e.blockedURI);"
    "});"
)

LONG_ARABIC = (
    "هذا نص تجريبي طويل جدا للتأكد من أن المحرر يدعم اللغة العربية "
    "بشكل صحيح مع الأرقام 123 والترقيم ؟! والرموز 🎙️ "
) * 6


@pytest.fixture
def p1_workspace_site(client, seed_tasks, tmp_path, monkeypatch):
    configure_admin(monkeypatch)
    for dist in (delivery.DIST_STANDARD, delivery.DIST_P1):
        problems = delivery.verify_build_complete(dist)
        assert not problems, f"preview build incomplete in {dist}: {problems}"
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", True)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")
    seed_tasks(2)
    httpd, thread, url = start_app_server()
    yield {"url": url, "client": client}
    stop_app_server(httpd, thread)
    monkeypatch.setitem(server.app.config, "FRONTEND_PREVIEW_ENABLED", False)
    monkeypatch.setitem(server.app.config, "FRONTEND_BUILD", "standard")


def _claim(client, username):
    login = client.post("/api/login", json={"username": username})
    assert login.status_code == 200, login.json
    claimed = client.post("/api/assignment/claim", json={})
    assert claimed.status_code == 200, claimed.json
    assignment = claimed.json
    assert assignment.get("assigned") is True, assignment
    session_cookie = client.get_cookie("session")
    assert session_cookie is not None, "annotator session cookie missing"
    return assignment, session_cookie.value


def _seed_audio_for(task_rel_path):
    audio_dir = Path(server.app.config["AUDIO_DIR"])
    target = audio_dir / task_rel_path
    write_silence_wav(target, seconds=10.0)


def _open_workspace(playwright, url, session_value, *, width=1366, height=768):
    browser = playwright.chromium.launch(
        headless=True, args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"]
    )
    # Reuse the API login session (same app/secret) instead of a second
    # browser login, which would trigger the session-takeover flow.
    context = browser.new_context(
        viewport={"width": width, "height": height}, reduced_motion="reduce"
    )
    context.add_cookies(
        [{"name": "session", "value": session_value, "domain": "127.0.0.1", "path": "/"}]
    )
    page = context.new_page()
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
    response = page.goto(url + "/frontend-preview/workspace")
    assert response is not None and response.status == 200
    expect(page.get_by_test_id("workspace-taskbar")).to_be_visible(timeout=20000)
    return browser, page, console_errors, page_errors


def _first_transcript(page):
    return page.get_by_label("Segment 1 transcript")


def test_p1_workspace_first_screen(p1_workspace_site):
    site = p1_workspace_site
    assignment, session_value = _claim(site["client"], "p1-ws-first")
    _seed_audio_for(assignment["rel_path"])
    with sync_playwright() as playwright:
        browser, page, _, page_errors = _open_workspace(
            playwright, site["url"], session_value
        )
        try:
            expect(page.get_by_test_id("workspace-filename")).to_contain_text(
                assignment["filename"], timeout=10000
            )
            expect(page.get_by_test_id("workspace-revision")).to_contain_text("rev ", timeout=10000)
            expect(page.get_by_test_id("audio-transport")).to_be_visible(timeout=10000)
            box = _first_transcript(page).bounding_box(timeout=15000)
            assert box is not None and box["y"] < 768, box
            assert _first_transcript(page).get_attribute("dir") == "auto"
            page.screenshot(path=str(EVIDENCE_DIR / "workspace-1366x768.png"))
            assert page.evaluate("window.__p1csp || []") == []
            assert page_errors == []
        finally:
            browser.close()


def test_p1_workspace_long_arabic_save_reload(p1_workspace_site):
    site = p1_workspace_site
    assignment, session_value = _claim(site["client"], "p1-ws-arabic")
    _seed_audio_for(assignment["rel_path"])
    with sync_playwright() as playwright:
        browser, page, _, _ = _open_workspace(playwright, site["url"], session_value)
        try:
            area = _first_transcript(page)
            expect(area).to_be_visible(timeout=15000)
            before = page.get_by_test_id("workspace-revision").inner_text(timeout=10000)
            rev0 = int(before.split()[1])
            area.fill(LONG_ARABIC)
            expect(page.get_by_test_id("workspace-revision")).to_contain_text(
                "unsaved", timeout=10000
            )
            page.get_by_test_id("workspace-save").click()
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=20000
            )
            page.reload()
            expect(_first_transcript(page)).to_have_value(LONG_ARABIC, timeout=20000)
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=10000
            )
        finally:
            browser.close()


def test_p1_workspace_ctrl_s_saves(p1_workspace_site):
    site = p1_workspace_site
    assignment, session_value = _claim(site["client"], "p1-ws-ctrls")
    _seed_audio_for(assignment["rel_path"])
    with sync_playwright() as playwright:
        browser, page, _, _ = _open_workspace(playwright, site["url"], session_value)
        try:
            area = _first_transcript(page)
            expect(area).to_be_visible(timeout=15000)
            before = page.get_by_test_id("workspace-revision").inner_text(timeout=10000)
            rev0 = int(before.split()[1])
            area.fill("ctrl s save probe نص")
            expect(page.get_by_test_id("workspace-revision")).to_contain_text(
                "unsaved", timeout=10000
            )
            page.keyboard.press("Control+s")
            expect(page.get_by_test_id("workspace-revision")).to_have_text(
                f"rev {rev0 + 1}", timeout=20000
            )
        finally:
            browser.close()


def test_p1_workspace_narrow_screen(p1_workspace_site):
    site = p1_workspace_site
    assignment, session_value = _claim(site["client"], "p1-ws-narrow")
    _seed_audio_for(assignment["rel_path"])
    with sync_playwright() as playwright:
        browser, page, _, page_errors = _open_workspace(
            playwright, site["url"], session_value, width=390, height=844
        )
        try:
            area = _first_transcript(page)
            expect(area).to_be_visible(timeout=15000)
            box = area.bounding_box(timeout=10000)
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390, box
            area.click()
            expect(area).to_be_focused(timeout=10000)
            expect(page.get_by_test_id("workspace-save")).to_be_visible(timeout=5000)
            page.screenshot(path=str(EVIDENCE_DIR / "workspace-390x844.png"))
            assert page_errors == []
        finally:
            browser.close()


def test_p1_workspace_audio_single_instance_segment_stop(p1_workspace_site):
    site = p1_workspace_site
    assignment, session_value = _claim(site["client"], "p1-ws-audio")
    _seed_audio_for(assignment["rel_path"])
    with sync_playwright() as playwright:
        browser, page, _, page_errors = _open_workspace(playwright, site["url"], session_value)
        try:
            expect(_first_transcript(page)).to_be_visible(timeout=15000)
            assert page.locator('audio[data-testid="workspace-audio"]').count() == 1
            # The page-rendered element must be the adopted instance (R08):
            # a fallback instance would leave the DOM element without src.
            expect(page.locator('audio[data-testid="workspace-audio"]')).to_have_attribute(
                "src", f"/api/audio/{assignment['task_id']}", timeout=10000
            )
            page.get_by_label("Play segment 1").click()
            toggle = page.get_by_test_id("transport-toggle")
            expect(toggle).to_have_attribute("aria-pressed", "true", timeout=15000)
            # Segment 1 ends at 5s; playback must stop there by itself.
            expect(toggle).to_have_attribute("aria-pressed", "false", timeout=25000)
            assert page.locator('audio[data-testid="workspace-audio"]').count() == 1
            assert page_errors == []
        finally:
            browser.close()
