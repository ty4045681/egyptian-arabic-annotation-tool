"""A completed write for the previous person must not erase the current draft."""
from playwright.sync_api import expect, sync_playwright

from tests.test_admin_repository import _make_user
from tests.browser.admin_helpers import choose, expect_choice, open_annotator, close_annotator


def test_late_scope_save_preserves_new_person_draft(provenance_site):
    site = provenance_site
    first, _ = _make_user('scope-save-first')
    second, _ = _make_user('scope-save-second')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        held = []
        try:
            page.goto(site['url'] + '/admin')
            page.locator('#adminKey').fill(site['admin_key'])
            page.locator('#loginButton').click()
            expect(page.locator('#adminApp')).to_be_visible(timeout=15000)
            page.locator('[data-view="annotators"]').click()
            open_annotator(page, first["id"])
            expect(page.locator('#scopeMode')).to_be_enabled(timeout=10000)
            choose(page, "#scopeMode", "Cannot claim")
            page.locator('#scopeReason').fill('Pause first person')
            page.route(f'**/api/admin/annotators/{first["id"]}/scene-scope', lambda route: held.append(route))
            page.locator('#sceneScopeForm button[type="submit"]').click()
            expect(page.locator('#scopeMode')).to_be_disabled()
            assert held
            close_annotator(page)
            open_annotator(page, second["id"])
            expect(page.locator('#annotatorName')).to_have_text('scope-save-second')
            expect(page.locator('#scopeMode')).to_be_enabled(timeout=10000)
            choose(page, "#scopeMode", "Restricted scenes")
            page.locator('#scopeSceneGrid input[value="hotel"]').check()
            page.locator('#scopeReason').fill('Unsaved Hotel scope for second person')
            with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith('/scene-scope')) as saved:
                held.pop().continue_()
            assert saved.value.status == 200
            expect(page.locator('.ant-message')).to_contain_text('Claim scope saved. Existing assignments are not released.')
            expect(page.locator('#annotatorName')).to_have_text('scope-save-second')
            expect(page.locator('#scopeReason')).to_have_value('Unsaved Hotel scope for second person')
            expect_choice(page, "#scopeMode", "Restricted scenes")
            expect(page.locator('#scopeSceneGrid input[value="hotel"]')).to_be_checked()
        finally:
            for route in held:
                route.abort()
            browser.close()
