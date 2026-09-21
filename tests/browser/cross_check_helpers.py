"""Shared Playwright and seeding helpers for cross-check browser tests."""
from __future__ import annotations

from pathlib import Path
import re
import uuid

from playwright.sync_api import expect

import db
import server
from tests.browser.conftest import ADMIN_KEY, write_silence_wav

from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import complete, text_segments, words
from tests.test_api import login
from tests.test_scene_claims import source

SECRET_ORIGINAL = "ALPHA-ORIGINAL-TRANSCRIPT-SECRET"
ACCEPT_DIR = Path("docs/plans/cross-annotation-quality-frontend-acceptance")


def write_wavs_for_tasks(task_ids, seconds=10.0):
    audio_dir = Path(server.app.config["AUDIO_DIR"])
    with db.db_conn() as conn:
        for task_id in task_ids:
            rel_path = conn.execute(
                "SELECT rel_path FROM annotation_tasks WHERE id = %s",
                (task_id,),
            ).fetchone()[0]
            write_silence_wav(audio_dir / rel_path, seconds=seconds)


def screenshot(page, name: str, directory: Path | None = None):
    target = directory or ACCEPT_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    page.screenshot(path=str(path), full_page=True)
    return path


def login_annotator(page, url, username):
    page.goto(url + "/login.html")
    page.locator("#username").fill(username)
    page.locator("#joinBtn").click()
    expect(page.locator("#currentUser")).not_to_have_text("—", timeout=15000)
    expect(page.locator("#assignmentLine")).not_to_have_text("Checking your task…", timeout=15000)


def select_airport(page):
    picker = page.locator("#idleScenePicker")
    expect(picker).to_be_visible(timeout=10000)
    picker.get_by_role("button", name="Airport").click()
    expect(page.locator("#idleScenePicker button[aria-pressed='true']")).to_contain_text("Airport", timeout=10000)


def claim_next(page):
    page.locator("#claimButton").click()
    expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=15000)


def fill_first_segment(page, text, *, bad_quality_rest=True):
    areas = page.locator("textarea[data-text]")
    areas.nth(0).fill(text)
    if bad_quality_rest:
        count = areas.count()
        for index in range(1, count):
            page.locator(f"[data-quality='{index}']").click()


def login_admin(page, url, key=ADMIN_KEY):
    page.goto(url + "/admin")
    page.locator("#adminKey").fill(key)
    page.locator("#loginButton").click()
    expect(page.locator("#adminApp")).to_be_visible(timeout=15000)


def open_cross_checks(page, state="awaiting_review"):
    page.locator('[data-view="cross-checks"]').click()
    expect(page.locator("#crossChecksView")).to_be_visible(timeout=15000)
    if state:
        page.locator("#ccState").select_option(state)


def seed_originals(client, seed_tasks, count, text, *, duration=10.0):
    task_ids = seed_tasks(count, folder=uuid.uuid4().hex[:10], duration=duration)
    for task_id in task_ids:
        source(task_id, "airport", "high")
    write_wavs_for_tasks(task_ids, seconds=duration)
    login(client, "alice")
    completed = []
    for _ in task_ids:
        claimed = client.post("/api/assignment/claim", json={"source_scene": "airport"})
        assert claimed.status_code == 200, claimed.json
        assignment = claimed.json
        response, _ = complete(client, assignment, segments=text_segments(assignment, text))
        assert response.status_code == 200, response.json
        completed.append(assignment)
    client.post("/api/logout", json={})
    return task_ids, completed


def queue_awaiting(client, seed_tasks, count, *, original=None, secondary=None, duration=10.0):
    original = original or words(100)
    secondary = secondary or " ".join(f"x{i}" if i < 11 else f"w{i}" for i in range(100))
    task_ids, _ = seed_originals(client, seed_tasks, count, original, duration=duration)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    rounds = []
    for _ in range(count):
        claimed = client.post("/api/assignment/claim", json={"source_scene": "airport"})
        assert claimed.status_code == 200, claimed.json
        assignment = claimed.json
        response, _ = complete(client, assignment, segments=text_segments(assignment, secondary))
        assert response.status_code == 200, response.json
        rounds.append({
            "round_id": response.json["cross_check"]["round_id"],
            "task_id": assignment["task_id"],
            "state": response.json["cross_check"]["state"],
            "assignment": assignment,
            "original": original,
            "secondary": secondary,
        })
    client.post("/api/logout", json={})
    return rounds


def console_errors(page):
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    return failures


def assert_annotator_blind(page):
    expect(page.locator("#crossCheckBadge, #tabCrossChecks, #crossCheckPanel")).to_have_count(0)
    assert not re.search(
        r"cross[- ]?check|independent annotation|second, independent|"
        r"awaiting (?:admin )?review|\badjudicated\b|original published",
        page.locator("body").inner_text(), re.IGNORECASE,
    )
    expect(page.locator('a[href*="tab=cross-checks"]')).to_have_count(0)


def assignment_leak_watch(page, bucket):
    def on_response(response):
        if "/api/assignment" not in response.url:
            return
        if response.request.method not in {"GET", "POST", "PATCH"}:
            return
        try:
            if response.ok:
                bucket.append(response.json())
        except Exception:
            return
    page.on("response", on_response)
    return bucket
