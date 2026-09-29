"""Submission comparison, scene overrides, and whole-audio skip decisions."""

from playwright.sync_api import expect, sync_playwright

import db
from annotation_metadata.repository import append_review, latest_review
from tests.browser.admin_helpers import choose, expect_choice
from tests.browser.cross_check_helpers import login_admin, open_cross_checks, queue_awaiting


def open_panel(page, site):
    login_admin(page, site["url"])
    open_cross_checks(page)
    page.locator("[data-cc-round]").first.click()
    panel = page.locator("#ccSubmissionReview")
    expect(panel).to_contain_text("Source scene: Airport")
    expect(page.locator("#ccSceneOverride")).to_be_enabled()
    return panel


def test_comparison_override_reset_and_published_scene(cross_check_site):
    queued = queue_awaiting(cross_check_site["client"], cross_check_site["seed_tasks"], 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        panel = open_panel(page, cross_check_site)
        expect(panel).to_contain_text("A and B agree")
        expect(panel.locator('[data-submission-side="original"]')).to_contain_text("Annotated")
        expect(panel.locator('[data-scene-side="original"]')).to_contain_text("Not reviewed")
        expect(panel.locator('[data-scene-side="secondary"]')).to_contain_text("Using source scene")
        expect(panel.locator("[data-final-status]")).to_contain_text("Kept from A & B")
        expect(panel.locator("[data-final-scene]")).to_contain_text("Airport")
        first = page.locator('[data-review-segment="1"]')
        first.get_by_role("button", name="Use B", exact=False).click()
        page.locator("#ccDecisionReason").fill("Listened to the audio and checked the scene")
        panel.locator(".cc-final-status").get_by_text("Skipped", exact=True).click()
        expect(panel.locator("[data-final-status]")).to_contain_text("Changed by admin")
        expect(panel.locator("#ccSkipWarning")).to_contain_text("whole audio")
        panel.get_by_role("checkbox", name="Noisy", exact=True).check()
        panel.get_by_role("switch", name="Override scene review").click()
        expect_choice(page, "#ccSceneVerdict", "Matches the source scene")
        expect(page.locator("#ccFinalScene")).to_be_disabled()
        expect(panel.locator("[data-final-scene]")).to_contain_text("Confirmed by admin")
        choose(page, "#ccSceneVerdict", "Doesn't match — reassign")
        choose(page, "#ccFinalScene", "Hotel")
        expect(panel.locator("[data-final-scene]")).to_contain_text("Reassigned by admin")
        expect(panel.locator('[data-submission-side="original"]')).to_contain_text("Annotated")
        expect(panel.locator('[data-scene-side="secondary"]')).to_contain_text("Not reviewed")
        expect(panel.locator(".cc-submission-changes")).to_contain_text("Annotated → Skipped")
        panel.get_by_role("button", name="Reset to defaults").click()
        expect(panel.get_by_role("radio", name="Annotated", exact=True)).to_be_checked()
        expect(panel.get_by_role("switch", name="Override scene review")).not_to_be_checked()
        expect(panel.locator(".cc-submission-changes")).to_have_count(0)
        expect(panel.locator("[data-final-scene]")).to_contain_text("Airport")
        expect(first.locator(".cc-segment-final")).to_contain_text("x0 x1")
        expect(page.locator("#ccDecisionReason")).to_have_value("Listened to the audio and checked the scene")
        panel.get_by_role("switch", name="Override scene review").click()
        choose(page, "#ccSceneVerdict", "Doesn't match — reassign")
        choose(page, "#ccFinalScene", "Hotel")
        page.locator("#ccSceneReviewNote").fill("Hotel lobby sounds in the background")
        page.locator("#ccSaveDraft").click()
        expect(page.locator(".ant-message")).to_contain_text("saved in this browser tab")
        page.reload()
        expect(panel.locator("[data-final-scene]")).to_contain_text("Hotel")
        expect(page.locator("#ccSceneReviewNote")).to_have_value("Hotel lobby sounds in the background")
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionSummary")).to_be_visible()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccReviewBody")).to_contain_text("Adjudication is complete", timeout=15000)
        expect(panel.locator("[data-final-scene]")).to_contain_text("Hotel")
        expect(panel.locator("[data-final-scene]")).to_contain_text("Published review")
        expect(panel.get_by_role("switch")).to_have_count(0)
        browser.close()
    with db.db_conn() as conn, conn.cursor() as cur:
        versions = cur.execute("SELECT original_version_id, secondary_version_id, final_version_id FROM cross_check_rounds WHERE id=%s", (queued["round_id"],)).fetchone()
        assert latest_review(cur, versions[0]) is None
        assert latest_review(cur, versions[1]) is None
        final = latest_review(cur, versions[2])
        assert final["status"] == "confirmed"
        assert final["scene_codes"] == ["hotel"]
        assert final["note"] == "Hotel lobby sounds in the background"


def test_skip_requires_reason_and_excludes_audio_without_resolving_segments(cross_check_site):
    queued = queue_awaiting(cross_check_site["client"], cross_check_site["seed_tasks"], 1)[0]
    sent = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.route("**/api/admin/cross-checks/*/decision", lambda route: (sent.append(route.request.post_data_json), route.continue_()))
        panel = open_panel(page, cross_check_site)
        panel.locator(".cc-final-status").get_by_text("Skipped", exact=True).click()
        expect(page.locator(".cc-review-progress")).to_contain_text("1 unresolved")
        page.locator("#ccConfirmDecision").click()
        expect(panel.get_by_text("A reason is required.", exact=True)).to_be_visible()
        page.locator("#ccDecisionReason").fill("Whole audio is too noisy to transcribe")
        page.locator("#ccConfirmDecision").click()
        expect(panel.get_by_text("Select at least one skip reason.", exact=True)).to_be_visible()
        assert not sent
        panel.get_by_role("checkbox", name="Noisy", exact=True).check()
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccDecisionSummary")).to_contain_text("Publish 2 segments as skipped")
        page.locator("#ccConfirmDecision").click()
        expect(page.locator("#ccReviewBody")).to_contain_text("Adjudication is complete", timeout=15000)
        expect(panel.locator("[data-final-status]")).to_contain_text("Skipped")
        assert len(sent) == 1
        assert sent[0]["skip_reasons"] == ["noisy"]
        browser.close()
    with db.db_conn() as conn:
        status = conn.execute("SELECT status FROM annotation_tasks WHERE id=%s", (queued["task_id"],)).fetchone()[0]
    assert status == "skipped"


def test_review_disagreement_and_non_scene_verdicts_are_preserved(cross_check_site):
    queued = queue_awaiting(cross_check_site["client"], cross_check_site["seed_tasks"], 1)[0]
    with db.db_conn() as conn, conn.cursor() as cur:
        versions = cur.execute("SELECT original_version_id, secondary_version_id FROM cross_check_rounds WHERE id=%s", (queued["round_id"],)).fetchone()
        for version, status, codes, note in [(versions[0], "confirmed", ["airport"], "Airport announcement"), (versions[1], "uncertain", [], "Cannot identify the background")]:
            actor = cur.execute("SELECT submitted_by_user_id FROM annotation_versions WHERE id=%s", (version,)).fetchone()[0]
            append_review(cur, version_id=version, status=status, scene_codes=codes, note=note, actor_kind="annotator", actor_user_id=actor)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        panel = open_panel(page, cross_check_site)
        expect(panel).to_contain_text("A and B differ")
        expect(panel.locator('[data-scene-side="original"]')).to_contain_text("Airport announcement")
        expect(panel.locator('[data-scene-side="secondary"]')).to_contain_text("Cannot determine")
        expect(panel.locator('[data-scene-side="secondary"]')).not_to_contain_text("Using source scene")
        expect(panel.locator("[data-final-scene]")).to_contain_text("Kept from A")
        panel.get_by_role("switch", name="Override scene review").click()
        choose(page, "#ccSceneVerdict", "Multiple scenes")
        choose(page, "#ccFinalScene", "Hotel")
        expect(panel.locator("[data-final-scene]")).to_contain_text("Multiple scenes: Airport, Hotel")
        choose(page, "#ccSceneVerdict", "Cannot determine")
        expect(panel.locator("[data-final-scene]")).to_contain_text("Cannot determine")
        expect(page.locator("#ccFinalScene")).to_be_disabled()
        expect(panel.locator("[data-final-scene]")).not_to_contain_text("Source scene")
        panel.get_by_role("button", name="Reset to defaults").click()
        expect(panel.locator("[data-final-scene]")).to_contain_text("Airport")
        browser.close()
