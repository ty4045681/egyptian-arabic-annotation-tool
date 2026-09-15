"""Independent rendered checks: English UI and one ten-scene catalog."""
import re

from playwright.sync_api import expect, sync_playwright

import db
from tests.browser.conftest import ADMIN_KEY
from tests.test_regression_english_acceptance import CATALOG


def assert_english_chrome(page):
    # Fixtures contain English data. The Arabic font-control caption is UI copy too.
    body = page.locator('body').inner_text()
    attrs = page.locator('[placeholder],[title],[aria-label]').evaluate_all(
        "nodes => nodes.map(n => ['placeholder','title','aria-label'].map(a => n.getAttribute(a)||'').join(' ')).join('\\n')"
    )
    assert not re.search(r'[\u3400-\u9fff\u0600-\u06ff]', body + attrs), body
    assert page.locator('html').get_attribute('lang') == 'en'


def test_english_ui_and_spoken_fallback_across_annotation_admin(live_site, tmp_path):
    url, task = live_site
    with db.db_conn() as conn:
        conn.execute('UPDATE task_sources SET scene_code=NULL WHERE task_id=%s', (task,))
    labels = [label for _, label in CATALOG]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        failures = []
        page.on('pageerror', lambda error: failures.append(str(error)))
        page.goto(url + '/login.html')
        assert_english_chrome(page)
        page.locator('#username').fill('english-ui-acceptance')
        page.locator('#joinBtn').click()
        expect(page.locator('#idleScenePicker')).to_be_visible(timeout=15000)
        picker_labels = page.locator('#idleScenePicker .scene-picker button > span:first-child').all_text_contents()
        assert picker_labels[1:] == labels
        assert_english_chrome(page)
        page.locator('#idleScenePicker').get_by_role('button', name='Spoken languages').click()
        expect(page.locator('#idleScenePicker button[aria-pressed=true]')).to_contain_text('Spoken languages')
        page.locator('#claimButton').click()
        expect(page.locator('#metadataBanner')).to_contain_text('Spoken languages', timeout=10000)
        expect(page.locator('textarea[data-text="0"]')).to_be_visible()
        page.locator('#sceneReviewPanel').get_by_role('button', name='Confirm', exact=True).click()
        assert page.locator('#sceneReviewPanel .scene-review-scenes label').all_text_contents() == labels
        page.locator('#metadataDisclosure > summary').click()
        assert_english_chrome(page)
        page.screenshot(path=str(tmp_path / 'english-annotation-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / 'english-annotation-mobile.png'), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.goto(url + '/completed.html')
        expect(page.locator('body')).to_contain_text('Completed')
        assert_english_chrome(page)
        page.goto(url + '/admin')
        assert_english_chrome(page)
        page.locator('#adminKey').fill(ADMIN_KEY)
        page.locator('#loginButton').click()
        expect(page.locator('#adminApp')).to_be_visible(timeout=15000)
        expect(page.locator('#sourceSceneGroups')).to_contain_text('Spoken languages', timeout=10000)
        for field in ('overviewSourceScene', 'corpusSourceScene'):
            assert page.locator('#' + field + ' option').all_text_contents()[1:] == labels
            assert page.locator('#' + field + ' option[value=unknown]').count() == 0
        assert page.locator('#scopeSceneGrid label').all_text_contents() == labels
        assert page.locator('#scopeAllowUnknown').count() == 0
        assert_english_chrome(page)
        page.screenshot(path=str(tmp_path / 'english-admin-overview.png'), full_page=True)
        for view in ('annotators', 'corpus', 'quality', 'activity'):
            page.locator('[data-view="' + view + '"]').click()
            assert_english_chrome(page)
        assert not failures, failures
        browser.close()
