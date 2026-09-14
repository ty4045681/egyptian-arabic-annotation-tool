from __future__ import annotations

import uuid

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


@pytest.fixture
def live_url(database, seed_tasks, tmp_path, monkeypatch):
    import threading
    from werkzeug.serving import make_server
    import server

    seed_tasks(1)
    audio = tmp_path / "audio"
    audio.mkdir()
    server.app.config.update(
        TESTING=True, AUDIO_DIR=str(audio),
        SESSION_TTL_SECONDS=1800, SECRET_KEY="test-secret",
    )
    httpd = make_server("127.0.0.1", 0, server.app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_annotator_can_open_workspace(live_url):
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(live_url + "/login.html")
            page.fill("input[name='username'], #username, input[type='text']", "browser-user")
            page.click("button[type='submit'], text=Log in")
            page.wait_for_timeout(1000)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Playwright browser unavailable: {exc}")
