"""Genuine Playwright acceptance for scene provenance annotator and admin UI."""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.conftest import insert_source


def _login_annotator(page, url, username):
    page.goto(url + "/login.html")
    page.locator("#username").fill(username)
    page.locator("#joinBtn").click()
    expect(page.locator("#claimButton")).to_be_visible(timeout=15000)


def _select_airport_and_claim(page):
    picker = page.locator("#idleScenePicker")
    expect(picker).to_be_visible(timeout=10000)
    picker.get_by_role("button", name="Airport").click()
    expect(page.locator("#claimButton")).to_be_visible(timeout=10000)
    expect(page.locator("#idleScenePicker button[aria-pressed='true']")).to_contain_text("Airport", timeout=10000)
    page.locator("#claimButton").click()
    expect(page.locator("#metadataBanner")).to_contain_text("Airport", timeout=10000)
    expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=10000)


def _fill_transcripts(page, prefix):
    areas = page.locator("textarea[data-text]")
    count = areas.count()
    assert count >= 1
    for index in range(count):
        areas.nth(index).fill(f"{prefix} {index}")


def _screenshot(page, directory: Path, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    page.screenshot(path=str(path), full_page=True)
    return path


def _console_failures(console, *, allow_substrings=()):
    unexpected = []
    for kind, text in console:
        if kind not in {"error", "assert"}:
            continue
        if any(token in text for token in allow_substrings):
            continue
        unexpected.append(f"{kind}: {text}")
    return unexpected


def _accept_dialogs(page):
    page.on("dialog", lambda dialog: dialog.accept())


@pytest.mark.parametrize("action", ["abandon", "complete", "skip"])
def test_scene_review_cleared_when_returning_to_claim_page(provenance_site, action):
    url = provenance_site["url"]
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        _accept_dialogs(page)
        try:
            _login_annotator(page, url, f"browser-idle-review-{action}")
            panel = page.locator("#sceneReviewPanel")
            expect(panel).to_be_hidden()
            _select_airport_and_claim(page)
            expect(panel).to_be_visible()
            expect(panel.get_by_role("button", name="Confirm", exact=True)).to_be_enabled()

            if action == "complete":
                _fill_transcripts(page, "completed task")
            elif action == "skip":
                page.locator('[data-reason="noisy"]').click()
            endpoint = "abandon" if action == "abandon" else "complete"
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith(f"/api/assignment/current/{endpoint}"),
                timeout=10000,
            ) as released:
                page.locator(f"#{action}Button").click()
            assert released.value.status == 200, released.value.text()
            expect(page.locator("#claimButton")).to_be_visible(timeout=10000)
            expect(panel).to_be_hidden()
            expect(panel.locator("button, input, textarea, select")).to_have_count(0)

            page.locator("#claimButton").click()
            expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=10000)
            expect(panel).to_be_visible()
            expect(panel.get_by_role("button", name="Confirm", exact=True)).to_be_enabled()
            assert not errors, errors
        finally:
            browser.close()


def test_annotator_admin_scene_provenance_workflow(provenance_site, tmp_path):
    site = provenance_site
    url, tasks = site["url"], site["tasks"]
    artifacts = tmp_path / "artifacts"
    errors = []
    console = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console.append((message.type, message.text)))
        _accept_dialogs(page)
        try:
            _login_annotator(page, url, "browser-workflow")
            expect(page.locator("#idleScenePicker")).to_contain_text("Claim scope")
            _select_airport_and_claim(page)
            banner = page.locator("#metadataBanner")
            expect(banner).to_contain_text("Source confidence High")
            expect(banner).to_contain_text("Scene review pending")
            disclosure = page.locator("#metadataDisclosure")
            expect(disclosure).to_be_visible()
            assert disclosure.evaluate("el => el.open") is False
            disclosure.locator("summary").click()
            assert disclosure.evaluate("el => el.open") is True
            expect(disclosure).to_contain_text("Source classification is not a human review")
            expect(disclosure).to_contain_text("<script>alert('xss-basis')</script>")
            expect(disclosure).to_contain_text("<img src=x onerror=alert(1)>")
            expect(disclosure.locator("a[href^='https://www.youtube.com']")).to_have_count(1)
            expect(disclosure.locator("a[href^='javascript:']")).to_have_count(0)
            expect(disclosure).not_to_contain_text("javascript:alert")
            expect(page.locator("#sceneReviewPanel input[value='shopping']")).to_have_count(1)

            panel = page.locator("#sceneReviewPanel")
            try:
                with page.expect_request(
                    lambda request: request.method == "PATCH" and request.url.endswith("/api/assignment/current"),
                    timeout=3500,
                ):
                    panel.get_by_role("button", name="Confirm", exact=True).click()
                raise AssertionError("confirmed-without-labels must not autosave")
            except PlaywrightTimeout:
                pass
            expect(page.locator("#sceneReviewValidation")).to_contain_text("exactly one scene")
            panel.locator('input[value="airport"]').check()
            with page.expect_response(
                lambda response: response.url.endswith("/api/assignment/current") and response.request.method == "PATCH",
                timeout=8000,
            ) as saved:
                page.locator("#saveButton").click()
            assert saved.value.status == 200, saved.value.text()
            assert saved.value.request.post_data_json["scene_review"]["status"] == "confirmed"
            expect(page.locator("#saveState")).to_have_text("Saved", timeout=10000)
            page.reload()
            expect(page.locator('#sceneReviewPanel input[value="airport"]')).to_be_checked(timeout=10000)
            expect(page.locator("#metadataBanner")).to_contain_text("not submitted")
            expect(page.locator("#metadataBanner")).to_contain_text("Airport")

            _fill_transcripts(page, "workflow text")
            with page.expect_response(
                lambda response: response.url.endswith("/api/assignment/current") and response.request.method == "PATCH",
                timeout=8000,
            ) as text_saved:
                page.locator("#saveButton").click()
            assert text_saved.value.status == 200, text_saved.value.text()
            assert "scene_review" not in text_saved.value.request.post_data_json
            expect(page.locator("#saveState")).to_have_text("Saved", timeout=10000)

            with page.expect_response(
                lambda response: response.url.endswith("/api/assignment/current/complete") and response.request.method == "POST",
                timeout=8000,
            ) as completed:
                page.locator("#completeButton").click()
            assert completed.value.status == 200, completed.value.text()
            expect(page.locator("#claimButton")).to_be_visible(timeout=10000)
            page.evaluate(
                "([username, taskId]) => AnnotationOffline.deleteTaskData(username, taskId)",
                ["browser-workflow", tasks[0]],
            )
            _screenshot(page, artifacts, "desktop-idle-after-complete.png")

            page.goto(url + "/completed.html")
            expect(page.locator("[data-view]").first).to_be_visible(timeout=10000)
            page.locator("[data-view]").first.click()
            expect(page.locator("#completedMetadata")).to_contain_text("Airport", timeout=10000)
            expect(page.locator("#completedMetadata")).to_contain_text("Scene confirmed")
            expect(page.locator("#completedMetadata")).not_to_contain_text("not submitted")
            expect(page.locator("#completedMetadataDisclosure")).to_contain_text("Submitted")
            expect(page.locator("#completedMetadataDisclosure")).to_contain_text("Airport")
            page.locator("#correctButton").click()
            expect(page.locator("#sceneReviewPanel")).to_be_visible(timeout=15000)
            expect(page.locator(".review-reference")).to_contain_text("Last published review")
            expect(page.locator(".review-reference")).to_contain_text("Scene confirmed")
            expect(page.locator("#metadataBanner")).to_contain_text("Scene review pending")
            _screenshot(page, artifacts, "desktop-reopen-reference.png")
            page.set_viewport_size({"width": 390, "height": 844})
            _screenshot(page, artifacts, "mobile-reopen-reference.png")
            page.set_viewport_size({"width": 1366, "height": 1000})
            abandon = page.locator("#abandonButton")
            expect(abandon).to_be_enabled(timeout=10000)
            abandon.click()
            expect(page.locator("#claimButton")).to_be_visible(timeout=10000)
            expect(page.locator("#sceneReviewPanel")).to_be_hidden()

            page.goto(url + "/admin")
            expect(page.locator("#adminKey")).to_be_visible(timeout=10000)
            page.locator("#adminKey").fill(site["admin_key"])
            page.locator("#loginButton").click()
            expect(page.locator("#adminApp")).to_be_visible(timeout=15000)
            expect(page.locator("#sourceSceneGroups")).to_contain_text("Airport", timeout=10000)
            expect(page.locator("#sourceSceneGroups")).to_contain_text("Tasks")
            expect(page.locator("#confidenceGroups")).to_contain_text("Source confidence")
            expect(page.locator("#reviewStatusGroups")).to_contain_text("Confirmed")
            expect(page.locator("#metadataOverviewPanel")).to_contain_text("may overlap")

            page.locator("[data-view='corpus']").click()
            expect(page.locator("#corpusSourceScene")).to_be_visible(timeout=5000)
            page.locator("#corpusSourceScene").select_option("airport")
            page.locator("#corpusStatus").select_option("annotated")
            page.locator("#corpusFilterForm button[type='submit']").click()
            expect(page.locator("#corpusMatchedStats")).to_contain_text("tasks", timeout=10000)
            expect(page.locator("#corpusTaskBody")).to_contain_text("Airport", timeout=10000)
            expect(page.locator("#corpusTaskBody")).to_contain_text("Confirmed")

            page.locator("[data-task-action='view-corpus']").first.click()
            expect(page.locator("#adminTaskMetadataBanner")).to_contain_text("Airport", timeout=10000)
            expect(page.locator("#adminReviewCurrent")).to_contain_text("Airport")
            page.locator("#adminReviewStatus").select_option("confirmed")
            for box in page.locator("#adminReviewScenes input[type=checkbox]").all():
                box.set_checked(box.get_attribute("value") == "shopping")
            page.locator("#adminReviewReason").fill("Content is actually Shopping")
            page.locator("#adminReviewForm button[type='submit']").click()
            expect(page.locator("#adminReviewCurrent")).to_contain_text("Shopping", timeout=10000)
            expect(page.locator("#adminTaskMetadataBanner")).to_contain_text("Shopping")
            expect(page.locator("#adminTaskMetadataBanner")).not_to_contain_text("Scene confirmed (Airport)")
            page.get_by_role("dialog").get_by_label("Close").click()
            expect(page.locator("#taskDialog")).not_to_be_visible(timeout=5000)

            page.locator("[data-view='annotators']").click()
            expect(page.locator("[data-annotator-id]").first).to_be_visible(timeout=10000)
            page.locator("[data-annotator-id]").first.click()
            expect(page.locator("#sceneScopeForm")).to_be_visible(timeout=10000)
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-scope-ready", "1", timeout=10000)
            page.locator("#scopeMode").select_option("restricted")
            for box in page.locator("#scopeSceneGrid input[type=checkbox]").all():
                box.set_checked(box.get_attribute("value") == "airport")
            page.locator("#scopeReason").fill("Restrict claims to Airport")
            page.locator("#sceneScopeForm button[type='submit']").click()
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-scope-ready", "1", timeout=10000)
            expect(page.locator("#scopeMode")).to_have_value("restricted", timeout=10000)
            _screenshot(page, artifacts, "desktop-admin-scope.png")

            with db.db_conn() as conn:
                latest = conn.execute(
                    """SELECT r.status, array_agg(l.scene_code ORDER BY l.scene_code)
                       FROM scene_reviews r
                       JOIN annotation_versions v ON v.id = r.version_id
                       LEFT JOIN scene_review_labels l ON l.review_id = r.id
                       WHERE v.task_id = %s AND NOT r.superseded
                       GROUP BY r.id, r.status, r.review_no
                       ORDER BY r.review_no DESC LIMIT 1""",
                    (tasks[0],),
                ).fetchone()
                assert latest[0] == "confirmed"
                assert list(latest[1]) == ["shopping"]
            assert not errors, errors
            unexpected = _console_failures(console, allow_substrings=("Failed to load resource",))
            assert not unexpected, unexpected
        finally:
            browser.close()


def test_same_evidence_historical_and_layout(provenance_site, tmp_path):
    url = provenance_site["url"]
    task = provenance_site["tasks"][1]
    artifacts = tmp_path / "artifacts"
    errors = []
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET eligible=false WHERE id=%s",
            (provenance_site["tasks"][0],),
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            _login_annotator(page, url, "browser-same-evidence")
            _select_airport_and_claim(page)
            banner = page.locator("#metadataBanner")
            expect(banner).to_contain_text("Airport", timeout=10000)
            expect(banner).to_contain_text("Source confidence Medium")
            expect(banner).not_to_contain_text("Source confidence High")
            page.locator("#metadataDisclosure summary").click()
            expect(page.locator("#metadataDisclosure")).to_contain_text("Shopping")
            expect(page.locator("#metadataDisclosure")).to_contain_text("Source confidence High")
            _screenshot(page, artifacts, "desktop-same-evidence.png")

            with db.db_conn() as conn:
                claimed = conn.execute(
                    "SELECT claim_source_id FROM assignments WHERE task_id=%s",
                    (task,),
                ).fetchone()
            assert claimed and claimed[0]
            with db.db_conn() as conn:
                conn.execute("UPDATE task_sources SET is_current=false WHERE id=%s", (claimed[0],))
            insert_source(
                task, scene="airport", confidence="low",
                batch="acceptance-historical-current",
                basis="revised current airport source",
                url="https://example.com/current",
            )
            page.reload()
            expect(page.locator("#metadataBanner")).to_contain_text("Source confidence Medium", timeout=10000)
            page.locator("#metadataDisclosure summary").click()
            expect(page.locator("#metadataDisclosure")).to_contain_text("Source at claim time")
            expect(page.locator("#metadataDisclosure")).to_contain_text("Current sources")
            expect(page.locator("#metadataDisclosure")).to_contain_text("Source confidence Low")

            if page.locator("#metadataDisclosure").evaluate("el => el.open"):
                page.locator("#metadataDisclosure summary").click()
            page.set_viewport_size({"width": 390, "height": 844})
            transcript = page.locator("textarea[data-text='0']")
            expect(transcript).to_be_visible()
            box = transcript.bounding_box()
            assert box is not None
            assert box["y"] < 844, box
            _screenshot(page, artifacts, "mobile-same-evidence.png")
            assert not errors, errors
        finally:
            browser.close()


def test_metadata_ui_off_still_saves_transcript(provenance_site, monkeypatch):
    monkeypatch.setenv("ANNOTATION_METADATA_UI", "0")
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        try:
            page.goto(url + "/login.html")
            page.locator("#username").fill("browser-ui-off")
            page.locator("#joinBtn").click()
            expect(page.locator("#claimButton")).to_be_visible(timeout=15000)
            expect(page.locator("#idleScenePicker")).to_have_count(0)
            page.locator("#claimButton").click()
            expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=10000)
            expect(page.locator("#sceneReviewPanel")).to_be_hidden()
            expect(page.locator("#metadataBanner")).to_be_hidden()
            page.locator("textarea[data-text='0']").fill("text with metadata ui disabled")
            with page.expect_response(
                lambda response: response.url.endswith("/api/assignment/current") and response.request.method == "PATCH",
                timeout=8000,
            ) as saved:
                page.locator("#saveButton").click()
            assert saved.value.status == 200, saved.value.text()
            assert "scene_review" not in saved.value.request.post_data_json
        finally:
            browser.close()


def test_revision_conflict_blocks_further_edits(live_site):
    url, _task = live_site
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            _login_annotator(page, url, "browser-conflict")
            page.locator("#claimButton").click()
            expect(page.locator("textarea[data-text='0']")).to_be_visible(timeout=10000)
            bumped = page.evaluate(
                """async () => {
                  const asg = await (await fetch('/api/assignment')).json();
                  const response = await fetch('/api/assignment/current', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                      lease_token: asg.lease_token,
                      expected_revision: asg.revision,
                      operation_id: crypto.randomUUID(),
                      segments: []
                    })
                  });
                  return response.status;
                }"""
            )
            assert bumped == 200
            page.locator("textarea[data-text='0']").fill("stale client text")
            with page.expect_response(
                lambda response: response.url.endswith("/api/assignment/current") and response.request.method == "PATCH",
                timeout=8000,
            ) as conflicted:
                page.locator("#saveButton").click()
            assert conflicted.value.status == 409, conflicted.value.text()
            expect(page.locator("#saveState")).to_have_attribute("data-state", "error", timeout=8000)
            expect(page.locator("#saveState")).to_contain_text("Conflict")
            assert not errors, errors
        finally:
            browser.close()
