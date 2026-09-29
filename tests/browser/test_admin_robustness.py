"""Fault injection against the real admin UI and disposable database."""

import re

import pytest
from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.admin_helpers import navigate, open_annotator
from tests.browser.cross_check_helpers import login_admin
from tests.test_admin_repository import _complete_next, _make_user


@pytest.mark.parametrize("view,endpoint", [
    ("overview", "overview*"),
    ("annotators", "annotators?*"),
    ("corpus", "tasks?*"),
    ("quality", "quality?*"),
    ("cross-checks", "cross-checks?*"),
    ("activity", "audit?*"),
])
def test_list_failure_is_retryable_without_losing_navigation(provenance_site, view, endpoint):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        login_admin(page, provenance_site["url"])
        pattern = f"**/api/admin/{endpoint}"
        page.route(pattern, lambda route: route.fulfill(status=503, json={"error": "Temporary service outage"}))
        navigate(page, view)
        if view == "overview":
            page.locator("#refreshButton").click()
        expect(page.locator("#adminMain")).to_contain_text("Temporary service outage", timeout=15000)
        expect(page.locator("#adminApp")).to_be_visible()
        page.unroute(pattern)
        page.locator("#adminMain").get_by_role("button", name="Retry", exact=True).first.click()
        expect(page.locator("#adminMain")).not_to_contain_text("Temporary service outage", timeout=15000)
        if view == "corpus":
            expect(page.get_by_test_id("corpus-tbody").locator(".ant-table-row")).to_have_count(2)
        navigate(page, "overview")
        expect(page.locator("#kpiTotalAudio")).to_have_text("2")
        assert not errors
        browser.close()


@pytest.mark.parametrize("payload", ["empty-response", "invalid-row", "invalid-facet"])
def test_invalid_corpus_data_shows_recoverable_error(provenance_site, payload):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        login_admin(page, provenance_site["url"])
        pattern = "**/api/admin/metadata/facets" if payload == "invalid-facet" else "**/api/admin/tasks?*"

        def corrupt(route):
            response = route.fetch()
            data = response.json()
            if payload == "empty-response":
                data = {}
            elif payload == "invalid-row":
                data["items"][0] = None
            else:
                data["scenes"] = [None]
            route.fulfill(response=response, json=data)

        page.route(pattern, corrupt)
        navigate(page, "corpus")
        expect(page.locator("#adminMain")).to_contain_text("unexpected data", timeout=15000)
        expect(page.locator("#adminApp")).to_be_visible()
        page.unroute(pattern)
        if payload == "invalid-facet":
            page.get_by_role("button", name="Retry catalog", exact=True).click()
            expect(page.get_by_role("group", name="Scene", exact=True).get_by_role("button", name="Airport", exact=False)).to_be_enabled()
        else:
            page.get_by_test_id("corpus-retry").click()
        expect(page.get_by_test_id("corpus-tbody").locator(".ant-table-row")).to_have_count(2)
        expect(page.locator("#adminMain")).not_to_contain_text("unexpected data")
        assert not errors
        browser.close()


@pytest.mark.parametrize("endpoint", ["metadata/facets", "annotators?*"])
def test_corpus_auxiliary_session_expiry_clears_console(provenance_site, endpoint):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        login_admin(page, provenance_site["url"])
        pattern = f"**/api/admin/{endpoint}"
        page.route(pattern, lambda route: route.fulfill(status=401, json={"error": "Session expired"}))
        navigate(page, "corpus")
        expect(page.locator("#adminKey")).to_be_visible(timeout=10000)
        expect(page.locator("#adminApp")).to_have_count(0)
        page.unroute(pattern)
        page.locator("#adminKey").fill(provenance_site["admin_key"])
        page.locator("#loginButton").click()
        expect(page.locator("#adminApp")).to_be_visible()
        navigate(page, "corpus")
        expect(page.get_by_test_id("corpus-tbody").locator(".ant-table-row")).to_have_count(2)
        browser.close()


@pytest.mark.parametrize("kind", ["revoke", "deactivate"])
@pytest.mark.parametrize("delivery", ["request-lost", "response-lost", "unreadable-response"])
def test_uncertain_admin_action_replays_once(provenance_site, kind, delivery):
    person, _ = _make_user("robustness-actions")
    _complete_next(person)
    _complete_next(person)
    posted = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        login_admin(page, provenance_site["url"])
        open_annotator(page, person["id"])
        drawer = page.locator(".admin-annotator-drawer")
        expect(drawer.locator(".ant-table-row")).to_have_count(2)
        if kind == "revoke":
            drawer.get_by_role("checkbox", name="Select all", exact=True).check()
            drawer.get_by_role("button", name="Revoke selected (2)").click()
            modal = page.get_by_role("dialog", name="Revoke 2 annotations")
            modal.locator("#revokeReason").fill("Verify uncertain revocation")
            modal.locator("#revokeAdminKey").fill(provenance_site["admin_key"])
            endpoint = "annotations/revoke"
            submit, retry = "Revoke", "Retry same revocation"
        else:
            drawer.get_by_role("button", name="Deactivate annotator").click()
            modal = page.get_by_role("dialog", name="Deactivate robustness-actions")
            modal.locator("#deactivateReason").fill("Verify uncertain deactivation")
            modal.locator("#confirmUsername").fill(person["username"])
            modal.locator("#deactivateAdminKey").fill(provenance_site["admin_key"])
            endpoint = f"annotators/{person['id']}/deactivate"
            submit, retry = "Deactivate and return work", "Retry same deactivation"

        def interrupt(route):
            posted.append(route.request.post_data_json)
            if len(posted) > 1:
                route.continue_()
                return
            if delivery != "request-lost":
                assert route.fetch().status == 200
            if delivery == "unreadable-response":
                route.fulfill(status=200, json={})
            else:
                route.abort("connectionreset")

        page.route(f"**/api/admin/{endpoint}", interrupt)
        modal.get_by_role("button", name=submit, exact=True).click()
        expect(modal).to_contain_text("Retry will send the same request")
        expect(modal.locator("textarea")).to_be_disabled()
        expect(modal.get_by_role("button", name="Cancel", exact=True)).to_be_disabled()
        retry_button = modal.get_by_role("button", name=re.compile(re.escape(retry) + r"$"))
        expect(retry_button).to_be_enabled()
        page.keyboard.press("Escape")
        expect(modal).to_be_visible()
        retry_button.click()
        expect(modal).not_to_be_visible(timeout=15000)
        expect(page.locator("#annotatorCurrent")).to_have_text("0")
        browser.close()
    assert len(posted) == 2
    assert posted[0] == posted[1]
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM admin_actions WHERE operation_id=%s", (posted[0]["operation_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM annotation_versions WHERE submitted_by_user_id=%s AND lifecycle='revoked'", (person["id"],)).fetchone()[0] == 2
