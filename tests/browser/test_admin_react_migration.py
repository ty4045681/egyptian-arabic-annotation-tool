"""Real API writes and security boundaries of the migrated primary admin."""

import re

from playwright.sync_api import expect, sync_playwright
import annotation_repository as repo
import db
from tests.browser.cross_check_helpers import login_admin
from tests.browser.admin_helpers import choose, navigate, open_annotator
from tests.test_admin_repository import _complete_next, _make_user


def test_directory_search_and_filters_include_later_pages(provenance_site):
    for index in range(50):
        _make_user(f"directory-{index:02}")
    person, _ = _make_user("zz-last-page")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        login_admin(page, provenance_site["url"])
        navigate(page, "quality")
        with page.expect_request(
            lambda r: "/api/admin/quality?" in r.url and str(person["id"]) in r.url
        ):
            choose(page, '[aria-label="Annotator"]', person["username"])
        navigate(page, "cross-checks")
        page.get_by_role("button", name="More filters", exact=True).click()
        with page.expect_request(
            lambda r: "/api/admin/cross-checks?" in r.url and str(person["id"]) in r.url
        ):
            choose(page, '[aria-label="Original annotator"]', person["username"])
        navigate(page, "annotators")
        expect(page.get_by_role("group", name="Activity", exact=True).get_by_role("button", name=re.compile(r"^All\s*51$"))).to_be_visible()
        page.get_by_role("textbox", name="Search annotators").fill(person["username"])
        rows = page.get_by_test_id("annotator-table").locator("tbody .ant-table-row")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text(person["username"])
        browser.close()


def test_primary_admin_uses_vite_assets_and_clears_expired_session(provenance_site):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        scripts = []
        errors = []
        page.on(
            "request",
            lambda request: scripts.append(request.url)
            if request.resource_type == "script"
            else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(
            "window.violations=[];document.addEventListener('securitypolicyviolation',e=>window.violations.push(e.violatedDirective))"
        )
        login_admin(page, provenance_site["url"])
        expect(page.locator("#adminApp")).to_have_attribute(
            "data-admin-framework", "react"
        )
        expect(page.locator("#kpiTotalAudio")).to_have_text("2")
        for view in ["annotators", "corpus", "quality", "cross-checks", "activity"]:
            navigate(page, view)
            expect(page.locator(".admin-topbar")).to_contain_text(
                {
                    "annotators": "Annotators",
                    "corpus": "Tasks & corpus",
                    "quality": "Quality",
                    "cross-checks": "Cross-checks",
                    "activity": "Activity log",
                }[view]
            )
        assert any("/frontend/assets/AdminConsole-" in url for url in scripts)
        assert not any(
            "/admin.js" in url or "/admin-cross-check.js" in url for url in scripts
        )
        page.context.clear_cookies()
        page.locator("#refreshButton").click()
        expect(page.locator("#adminKey")).to_be_visible(timeout=15000)
        expect(page.locator("#adminApp")).to_have_count(0)
        assert not page.evaluate("window.violations")
        assert not errors
        browser.close()


def test_batch_revoke_requires_key_and_changes_actual_versions(provenance_site):
    person, _ = _make_user("migration-batch")
    _complete_next(person)
    _complete_next(person)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        login_admin(page, provenance_site["url"])
        open_annotator(page, person["id"])
        drawer = page.locator(".admin-annotator-drawer")
        expect(drawer.locator(".ant-table-row")).to_have_count(2)
        drawer.get_by_role("checkbox", name="Select all", exact=True).check()
        drawer.get_by_role("button", name="Revoke selected (2)").click()
        modal = page.get_by_role("dialog", name="Revoke 2 annotations")
        expect(modal).to_contain_text("2 annotations · 20s affected")
        modal.locator("#revokeReason").fill("Return the batch for independent review")
        expect(modal.get_by_role("button", name="Revoke", exact=True)).to_be_disabled()
        modal.locator("#revokeAdminKey").fill(provenance_site["admin_key"])
        with page.expect_response(
            lambda r: r.request.method == "POST"
            and r.url.endswith("/annotations/revoke")
        ) as response:
            modal.get_by_role("button", name="Revoke", exact=True).click()
        assert response.value.status == 200, response.value.text()
        assert response.value.request.headers.get("x-csrf-token")
        expect(modal).not_to_be_visible()
        expect(page.locator("#annotatorCurrent")).to_have_text("0")
        browser.close()
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT lifecycle FROM annotation_versions WHERE submitted_by_user_id=%s",
            (person["id"],),
        ).fetchall()
        assert rows == [("revoked",), ("revoked",)]
        assert (
            conn.execute(
                "SELECT count(*) FROM task_annotator_blocks WHERE user_id=%s",
                (person["id"],),
            ).fetchone()[0]
            == 2
        )


def test_deactivate_uses_impact_preview_username_and_key(provenance_site):
    person, _ = _make_user("migration-deactivate")
    _complete_next(person)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        login_admin(page, provenance_site["url"])
        open_annotator(page, person["id"])
        page.get_by_role("button", name="Deactivate annotator", exact=True).click()
        modal = page.get_by_role("dialog", name="Deactivate migration-deactivate")
        expect(modal).to_contain_text("1 contributions returned")
        modal.locator("#deactivateReason").fill("Remove test access")
        modal.locator("#confirmUsername").fill(person["username"])
        expect(
            modal.get_by_role("button", name="Deactivate and return work")
        ).to_be_disabled()
        modal.locator("#deactivateAdminKey").fill(provenance_site["admin_key"])
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/deactivate")
        ) as response:
            modal.get_by_role("button", name="Deactivate and return work").click()
        assert response.value.status == 200, response.value.text()
        expect(modal).not_to_be_visible()
        expect(page.locator(".admin-annotator-drawer")).to_contain_text("Deactivated")
        browser.close()
    with db.db_conn() as conn:
        assert (
            conn.execute(
                "SELECT status FROM annotators WHERE id=%s", (person["id"],)
            ).fetchone()[0]
            == "deactivated"
        )


def test_task_drawer_releases_assignment_and_restores_revoked_version(provenance_site):
    person, _ = _make_user("migration-task-actions")
    completed = _complete_next(person)
    assigned = repo.claim(person["fence"])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        login_admin(page, provenance_site["url"])
        page.goto(
            f"{provenance_site['url']}/admin?view=corpus&task={assigned['task_id']}"
        )
        page.get_by_role("button", name="Release assignment", exact=True).click()
        modal = page.get_by_role("dialog").filter(
            has_text="The current assignment will be released"
        )
        modal.get_by_role("textbox").fill("Return the interrupted assignment")
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/release")
        ) as response:
            modal.get_by_role("button", name="Confirm release", exact=True).click()
        assert response.value.status == 200, response.value.text()
        expect(modal).not_to_be_visible()
        expect(
            page.get_by_role("button", name="Release assignment", exact=True)
        ).to_have_count(0)

        page.goto(
            f"{provenance_site['url']}/admin?view=corpus&task={completed['task_id']}"
        )
        page.get_by_role("button", name="Revoke annotation", exact=True).click()
        revoke = page.get_by_role("dialog", name="Revoke annotation", exact=True)
        revoke.locator("#revokeReason").fill("Verify the restore workflow")
        revoke.get_by_role("button", name="Revoke", exact=True).click()
        expect(revoke).not_to_be_visible()
        page.get_by_role("button", name="Restore", exact=True).click()
        restore = page.get_by_role("dialog").filter(
            has_text="The revoked contribution will be restored"
        )
        restore.get_by_role("textbox").fill("Keep the original approved contribution")
        with page.expect_response(
            lambda r: r.request.method == "POST"
            and r.url.endswith("/annotations/restore")
        ) as response:
            restore.get_by_role("button", name="Confirm restore", exact=True).click()
        assert response.value.status == 200, response.value.text()
        expect(restore).not_to_be_visible()
        expect(page.locator("#taskDetail")).to_contain_text("Annotated")
        browser.close()
    with db.db_conn() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM assignments WHERE task_id=%s",
                (assigned["task_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            str(
                conn.execute(
                    "SELECT current_published_version_id FROM annotation_tasks WHERE id=%s",
                    (completed["task_id"],),
                ).fetchone()[0]
            )
            == completed["version_id"]
        )
