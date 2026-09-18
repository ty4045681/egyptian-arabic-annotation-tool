"""Reopening known-source audio must keep its source in the rendered banner."""
import re
import uuid

from playwright.sync_api import expect, sync_playwright

import annotation_repository as repo
import db
from tests.test_repository import full_segments


def test_reopened_known_source_keeps_english_source_banner(live_site):
    url, task = live_site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url + '/login.html')
            page.locator('#username').fill('english-reopen-source')
            page.locator('#joinBtn').click()
            expect(page.locator('#claimButton')).to_be_visible(timeout=15000)
            page.locator('#claimButton').click()
            expect(page.locator('#metadataBanner')).to_contain_text('Airport', timeout=10000)
            with db.db_conn() as conn:
                uid = conn.execute('SELECT id FROM annotators WHERE username=%s', ('english-reopen-source',)).fetchone()[0]
            assignment = repo.get_assignment(uid)
            fence = repo.load_session_fence(uid)
            repo.complete(fence, assignment['lease_token'], assignment['revision'],
                          'annotated', [], full_segments(assignment), str(uuid.uuid4()),
                          'english-reopen-source',
                          scene_review={'status': 'confirmed', 'scene_codes': ['hotel']})
            repo.reopen_completed(fence, task, str(uuid.uuid4()))
            page.reload()
            banner = page.locator('#metadataBanner [data-metadata-headline]')
            expect(banner).to_contain_text(re.compile(r'^Airport'), timeout=10000)
            expect(banner).to_contain_text('Source confidence High')
            # A draft review edit must not replace known source evidence either.
            page.locator('#sceneReviewPanel').get_by_role('button', name='Confirm', exact=True).click()
            expect(banner).to_contain_text(re.compile(r'^Airport'))
            expect(banner).not_to_contain_text('Spoken languages')
        finally:
            browser.close()
