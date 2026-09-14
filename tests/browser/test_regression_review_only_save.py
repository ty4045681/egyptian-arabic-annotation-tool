"""Real browser acceptance: scene-only changes must persist before task submission."""
from playwright.sync_api import sync_playwright, expect
import db

from tests.browser.conftest import live_site  # re-export for reviewer imports


def test_review_only_manual_save_survives_reload(live_site,tmp_path):
    url,task=live_site
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1366,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(url+'/login.html')
            page.locator('#username').fill('browser-review-only')
            page.locator('#joinBtn').click()
            expect(page.locator('#claimButton')).to_be_visible(timeout=15000)
            page.locator('#claimButton').click()
            expect(page.locator('#metadataBanner')).to_contain_text('机场',timeout=10000)
            expect(page.locator('#metadataBanner')).to_contain_text('来源置信度高')
            page.locator('#sceneReviewPanel').get_by_role('button',name='确认',exact=True).click()
            page.locator('#sceneReviewPanel input[value="airport"]').check()
            with page.expect_response(lambda response:response.url.endswith('/api/assignment/current') and response.request.method=='PATCH',timeout=6000) as saved:
                page.locator('#saveButton').click()
            assert saved.value.status==200,saved.value.text()
            page.reload()
            expect(page.locator('#sceneReviewPanel input[value="airport"]')).to_be_checked(timeout=10000)
            expect(page.locator('#metadataBanner')).to_contain_text('未提交')
            with db.db_conn() as conn:
                row=conn.execute('''SELECT sr.status FROM scene_reviews sr JOIN annotation_versions v ON v.id=sr.version_id
                    WHERE v.task_id=%s ORDER BY sr.review_no DESC LIMIT 1''',(task,)).fetchone()
                assert row==('confirmed',)
            assert not errors,errors
        finally:
            page.screenshot(path=str(tmp_path/'browser-review-only.png'),full_page=True)
            browser.close()
