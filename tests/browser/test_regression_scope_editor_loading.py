"""Claim-scope editor must not be usable until its matching annotator detail is loaded."""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect, sync_playwright

import db
from tests.test_admin_repository import _make_user

_DETAIL_PATH = re.compile(r"^/api/admin/annotators/[0-9a-fA-F-]{36}$")


def _is_annotator_detail_get(request) -> bool:
    if request.method != "GET":
        return False
    return bool(_DETAIL_PATH.match(urlparse(request.url).path))


def _detail_annotator_id(request) -> str:
    return urlparse(request.url).path.rsplit("/", 1)[-1]


def _login_admin(page, site):
    page.goto(site["url"] + "/admin")
    page.locator("#adminKey").fill(site["admin_key"])
    page.locator("#loginButton").click()
    expect(page.locator("#adminApp")).to_be_visible(timeout=15000)


def _open_annotator(page, user_id):
    button = page.locator(f'#sidebarAnnotatorList [data-annotator-id="{user_id}"]')
    expect(button).to_be_visible(timeout=10000)
    button.click()


def _wait_until(page, predicate, *, timeout=10000, message="condition was not met"):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(50)
    raise AssertionError(message)


def _assert_editor_unusable(page):
    form = page.locator("#sceneScopeForm")
    expect(form).to_be_visible()
    expect(form).to_have_attribute("data-scope-ready", "0")
    assert page.locator("#scopeEditorFields").evaluate("el => el.disabled && el.hidden") is True
    mode = page.locator("#scopeMode")
    assert not (mode.is_visible() and mode.is_enabled()), (
        "Claim scope must not be editable before the matching annotator detail is applied"
    )
    try:
        mode.select_option("restricted", timeout=1000)
        raise AssertionError("scope mode must not be selectable before the matching annotator detail is applied")
    except PlaywrightTimeout:
        pass
    try:
        with page.expect_request(
            lambda request: request.method == "PUT" and request.url.rstrip("/").endswith("/scene-scope"),
            timeout=1500,
        ):
            page.evaluate("document.getElementById('sceneScopeForm').requestSubmit()")
        raise AssertionError("claim-scope save must not run before the matching annotator detail is ready")
    except PlaywrightTimeout:
        pass


def _persisted_scope(user_id):
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT mode, allow_unknown, revision FROM annotator_scene_scopes WHERE user_id=%s",
            (user_id,),
        ).fetchone()
        codes = {
            item[0]
            for item in conn.execute(
                "SELECT scene_code FROM annotator_scene_access WHERE user_id=%s",
                (user_id,),
            )
        }
    return row, codes


def test_delayed_annotator_detail_blocks_scope_edit_then_saves_restricted_payload(provenance_site):
    site = provenance_site
    person, _ = _make_user("scope-detail-loading")
    uid = person["id"]
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotator_scene_scopes SET mode='all', allow_unknown=false, revision=2 WHERE user_id=%s",
            (uid,),
        )
        conn.execute("DELETE FROM annotator_scene_access WHERE user_id=%s", (uid,))
    held = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def intercept(route):
            if _is_annotator_detail_get(route.request):
                held.append(route)
                return
            route.continue_()

        page.route("**/api/admin/annotators/*", intercept)
        try:
            _login_admin(page, site)
            _open_annotator(page, uid)
            expect(page.locator("#annotatorDetail")).to_be_visible(timeout=10000)
            _wait_until(page, lambda: bool(held), message="The annotator detail GET should still be pending")
            _assert_editor_unusable(page)

            held.pop(0).continue_()
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-scope-ready", "1", timeout=10000)
            expect(page.locator("#scopeMode")).to_be_enabled()
            expect(page.locator("#scopeMode")).to_have_value("all")
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-annotator-id", str(uid))
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-revision", "2")
            page.unroute("**/api/admin/annotators/*")

            page.locator("#scopeMode").select_option("restricted")
            for box in page.locator("#scopeSceneGrid input[type=checkbox]").all():
                box.set_checked(box.get_attribute("value") in {"airport", "spoken_languages"})
            page.locator("#scopeReason").fill("Restrict claims to Spoken languages and Airport")
            with page.expect_response(
                lambda response: response.request.method == "PUT" and response.url.rstrip("/").endswith("/scene-scope"),
                timeout=8000,
            ) as saved:
                page.locator("#sceneScopeForm button[type='submit']").click()
            assert saved.value.status == 200, saved.value.text()
            body = saved.value.request.post_data_json
            assert body["expected_revision"] == 2
            assert body["mode"] == "restricted"
            assert set(body["scene_codes"]) == {"airport", "spoken_languages"}
            assert body["allow_unknown"] is True
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-scope-ready", "1", timeout=10000)
            expect(page.locator("#scopeMode")).to_have_value("restricted")
            expect(page.locator('#scopeSceneGrid input[value="airport"]')).to_be_checked()
            expect(page.locator('#scopeSceneGrid input[value="spoken_languages"]')).to_be_checked()
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-revision", "3")

            row, codes = _persisted_scope(uid)
            assert row == ("restricted", True, 3)
            assert codes == {"airport", "spoken_languages"}
            assert not errors, errors
        finally:
            for route in held:
                route.abort()
            browser.close()


def test_stale_annotator_detail_does_not_enable_or_overwrite_the_current_editor(provenance_site):
    site = provenance_site
    alice, _ = _make_user("scope-stale-alice")
    bob, _ = _make_user("scope-stale-bob")
    alice_id, bob_id = str(alice["id"]), str(bob["id"])
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotator_scene_scopes SET mode='restricted', allow_unknown=false, revision=5 WHERE user_id=%s",
            (alice["id"],),
        )
        conn.execute("DELETE FROM annotator_scene_access WHERE user_id=%s", (alice["id"],))
        conn.execute(
            "INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'airport')",
            (alice["id"],),
        )
        conn.execute(
            "UPDATE annotator_scene_scopes SET mode='all', allow_unknown=false, revision=1 WHERE user_id=%s",
            (bob["id"],),
        )
    held = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})

        def intercept(route):
            if _is_annotator_detail_get(route.request):
                held.setdefault(_detail_annotator_id(route.request), []).append(route)
                return
            route.continue_()

        page.route("**/api/admin/annotators/*", intercept)
        pending = []
        try:
            _login_admin(page, site)
            _open_annotator(page, alice_id)
            expect(page.locator("#annotatorDetail")).to_be_visible(timeout=10000)
            _wait_until(page, lambda: alice_id in held, message="Alice detail GET should still be pending")
            _open_annotator(page, bob_id)
            _wait_until(page, lambda: bob_id in held, message="Bob detail GET should still be pending")
            _assert_editor_unusable(page)

            held[alice_id].pop(0).continue_()
            _assert_editor_unusable(page)
            expect(page.locator("#annotatorName")).not_to_have_text("scope-stale-alice")

            held[bob_id].pop(0).continue_()
            expect(page.locator("#annotatorName")).to_have_text("scope-stale-bob", timeout=10000)
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-scope-ready", "1", timeout=10000)
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-annotator-id", bob_id)
            expect(page.locator("#scopeMode")).to_have_value("all")
            expect(page.locator("#sceneScopeForm")).to_have_attribute("data-revision", "1")
            row, codes = _persisted_scope(bob["id"])
            assert row[0] == "all"
            assert codes == set()
        finally:
            for routes in held.values():
                pending.extend(routes)
            for route in pending:
                route.abort()
            browser.close()


def test_failed_annotator_detail_keeps_scope_editor_disabled(provenance_site):
    site = provenance_site
    person, _ = _make_user("scope-detail-failed")
    uid = person["id"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})

        def intercept(route):
            if _is_annotator_detail_get(route.request):
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error":"annotator detail failed"}',
                )
                return
            route.continue_()

        page.route("**/api/admin/annotators/*", intercept)
        try:
            _login_admin(page, site)
            _open_annotator(page, uid)
            expect(page.locator("#scopeEditorStatus")).to_contain_text("Could not load claim scope.", timeout=10000)
            _assert_editor_unusable(page)
            row, codes = _persisted_scope(uid)
            assert row[0] == "all"
            assert codes == set()
        finally:
            browser.close()
