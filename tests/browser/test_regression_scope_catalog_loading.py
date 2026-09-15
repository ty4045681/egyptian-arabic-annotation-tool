"""The scene catalog is part of a fully loaded scope editor."""
from playwright.sync_api import expect, sync_playwright

import db
from tests.test_admin_repository import _make_user


def test_scope_editor_waits_for_catalog_and_preserves_existing_access(provenance_site):
    site = provenance_site
    person, _ = _make_user('scope-catalog-loading')
    uid = person['id']
    with db.db_conn() as conn:
        conn.execute("UPDATE annotator_scene_scopes SET mode='restricted', revision=3 WHERE user_id=%s", (uid,))
        conn.execute("INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'airport')", (uid,))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        held = []
        page.route('**/api/admin/metadata/facets*', lambda route: held.append(route))
        try:
            page.goto(site['url'] + '/admin')
            page.locator('#adminKey').fill(site['admin_key'])
            page.locator('#loginButton').click()
            expect(page.locator('#adminApp')).to_be_visible(timeout=15000)
            button = page.locator(f'#sidebarAnnotatorList [data-annotator-id="{uid}"]')
            expect(button).to_be_visible(timeout=10000)
            button.click()
            expect(page.locator('#annotatorName')).to_have_text('scope-catalog-loading', timeout=10000)
            assert held, 'The independent facet response should still be pending'
            mode = page.locator('#scopeMode')
            assert not (mode.is_visible() and mode.is_enabled()), 'An editor without its scene catalog must not allow accidental empty-scope saves'
            held.pop().continue_()
            expect(mode).to_be_visible(timeout=10000)
            expect(mode).to_be_enabled(timeout=10000)
            expect(mode).to_have_value('restricted')
            expect(page.locator('#scopeSceneGrid input[value="airport"]')).to_be_checked()
            page.locator('#scopeSceneGrid input[value="spoken_languages"]').check()
            page.locator('#scopeReason').fill('Add Spoken languages while preserving Airport')
            with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith('/scene-scope')) as saved:
                page.locator('#sceneScopeForm button[type="submit"]').click()
            assert saved.value.status == 200
            body = saved.value.request.post_data_json
            assert body['expected_revision'] == 3
            assert body['mode'] == 'restricted'
            assert set(body['scene_codes']) == {'airport', 'spoken_languages'}
            with db.db_conn() as conn:
                assert conn.execute('SELECT mode,revision FROM annotator_scene_scopes WHERE user_id=%s', (uid,)).fetchone() == ('restricted', 4)
                actual = {row[0] for row in conn.execute('SELECT scene_code FROM annotator_scene_access WHERE user_id=%s', (uid,))}
                assert actual == {'airport', 'spoken_languages'}
        finally:
            for route in held:
                route.abort()
            browser.close()
