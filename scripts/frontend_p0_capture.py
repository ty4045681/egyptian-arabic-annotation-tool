"""P0 evidence scenarios, invoked explicitly by scripts/frontend_p0.py.

These are capture scenarios for the existing UI, not acceptance tests for a new UI.
Layout defects are recorded rather than asserted away. All data is synthetic.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

import db
import server
from preprocess_store import store_preprocessed_task
from tests.browser.conftest import (
    configure_admin, insert_source, start_app_server, stop_app_server, write_silence_wav,
)
from tests.browser.cross_check_helpers import (
    login_admin, login_annotator, open_cross_checks, queue_awaiting, claim_next,
)
from tests.test_cross_check_claim import enable_cross_check

pytest_plugins = ["tests.conftest", "tests.browser.conftest"]
OUTPUT = Path(os.environ["FRONTEND_P0_OUTPUT"])
ARABIC = "أنا عايز أروح المطار، الرحلة رقم 123 الساعة 10:30. ممكن تساعدني؟ English / العربية — أهلاً 👋"
VIEWPORTS = [(1440, 900), (1366, 768), (1920, 1080), (1024, 768), (390, 844)]


def write_json(name, data):
    (OUTPUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def seed_audio(count=12, *, long_name=False, folder="p0-pending"):
    filename = ("airport_recording_long_filename_" * 5 + "العربية.wav") if long_name else f"sample-{count}.wav"
    duration = max(10.0, count * 0.2)
    step = duration / count
    with db.db_conn() as conn:
        task = store_preprocessed_task(
            conn, rel_path=f"{folder}/{filename}", filename=filename,
            folder=folder, duration=duration,
            segments=[{"id": i + 1, "start": round(i * step, 3),
                       "end": round((i + 1) * step, 3), "duration": round(step, 3),
                       "asr_text": ARABIC, "text": "", "exclude_from_training": False}
                      for i in range(count)],
            waveform_payload=b"\x10\x20\xf0\xdf" * 1000,
        )["task_id"]
    insert_source(task, scene="airport", confidence="high", batch="p0-synthetic",
                  basis="Synthetic source evidence for layout capture; no real transcript or user data.")
    write_silence_wav(Path(server.app.config["AUDIO_DIR"]) / folder / filename,
                      seconds=duration, rate=8000)
    return str(task)


class Recorder:
    def __init__(self, browser, scenario):
        self.scenario = scenario
        self.context = browser.new_context(viewport={"width": 1440, "height": 900},
            locale="en-US", timezone_id="Asia/Shanghai", device_scale_factor=1,
            reduced_motion="reduce")
        self.page = self.context.new_page()
        self.errors = []
        self.failed_requests = []
        self.console = []
        self.records = []
        self.headers = []
        self.page.on("pageerror", lambda e: self.errors.append({"message": str(e), "stack": e.stack}))
        self.page.on("console", lambda m: self.console.append({"type": m.type, "text": m.text})
                     if m.type in ("error", "warning") else None)
        self.page.on("requestfailed", lambda r: self.failed_requests.append({
            "method": r.method, "path": urlsplit(r.url).path, "failure": r.failure}))
        self.page.on("response", self.response)
        self.version = browser.version

    def response(self, response):
        if response.request.resource_type == "document":
            self.headers.append({"path": urlsplit(response.url).path,
                "status": response.status,
                "headers": {k: v for k, v in response.headers.items() if k in
                    ("content-security-policy", "cache-control", "content-type",
                     "x-frame-options", "x-content-type-options", "referrer-policy")}})

    def shot(self, name, *, size=None, note="", full=False):
        page = self.page
        if size:
            page.set_viewport_size({"width": size[0], "height": size[1]})
        page.evaluate("document.fonts.ready")
        page.evaluate("window.scrollTo(0, 0)")
        # Let responsive layout, ResizeObserver and chart animation settle.
        page.wait_for_timeout(200)
        viewport = page.viewport_size
        filename = f"{name}-{viewport['width']}x{viewport['height']}.png"
        target = OUTPUT / "screenshots" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target), full_page=full, animations="disabled")
        geometry = page.evaluate("""() => {
          const rect = el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
            return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,
              visible:!!(r.width&&r.height)&&s.visibility!=='hidden'&&s.display!=='none',
              fontSize:s.fontSize,lineHeight:s.lineHeight,direction:s.direction,
              color:s.color,background:s.backgroundColor}; };
          const selectors=['.app-header','.taskbar','#taskName','#saveState','#saveButton',
            '#completeButton','#sceneReviewPanel','#wavePanel','#tablePanel',
            'textarea[data-text="0"]','aside','.date-toolbar','.table-toolbar',
            '#ccQueuePanel','#ccListBody','#sceneScopeForm','#loginView','.layout'];
          return {viewport:{width:innerWidth,height:innerHeight},
            document:{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight},
            boxes:Object.fromEntries(selectors.map(s=>[s,document.querySelector(s)?rect(document.querySelector(s)):null])),
            controls:Array.from(document.querySelectorAll('button,input,select,textarea'))
              .map(e=>({id:e.id,label:(e.getAttribute('aria-label')||e.textContent||'').trim().slice(0,70),...rect(e)}))
              .filter(e=>e.visible),
            scrollContainers:Array.from(document.querySelectorAll('div,section,main,aside'))
              .filter(e=>e.clientWidth>0&&e.scrollWidth>e.clientWidth+1)
              .map(e=>({id:e.id,class:e.className,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,
                overflowX:getComputedStyle(e).overflowX})).slice(0,40),
            transcriptCount:document.querySelectorAll('textarea[data-text]').length,
            bodyFont:getComputedStyle(document.body).fontFamily};
        }""")
        self.records.append({"file": f"screenshots/{filename}", "url": urlsplit(page.url).path +
            ("?" + urlsplit(page.url).query if urlsplit(page.url).query else ""),
            "note": note, "full_page": full, "geometry": geometry})

    def finish(self):
        write_json(f"capture-{self.scenario}.json", {"browser": self.version,
            "locale": "en-US", "timezone": "Asia/Shanghai", "device_scale_factor": 1,
            "synthetic_data": True, "page_errors": self.errors,
            "console_messages": self.console, "failed_requests": self.failed_requests,
            "document_headers": self.headers, "screenshots": self.records})
        self.context.close()


@pytest.fixture
def evidence_site(client, monkeypatch):
    configure_admin(monkeypatch)
    httpd, thread, url = start_app_server()
    yield url
    stop_app_server(httpd, thread)


def test_capture_login_workspace_history(evidence_site, client):
    seed_audio(long_name=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        r = Recorder(browser, "annotator")
        p = r.page
        try:
            p.goto(evidence_site + "/login.html")
            expect(p.locator("#joinBtn")).to_be_visible()
            p.wait_for_timeout(600)
            for size in ((1440, 900), (390, 844)):
                r.shot("login-empty-statistics", size=size, note="Synthetic corpus with no submissions")
            p.set_viewport_size({"width": 1366, "height": 768})
            login_annotator(p, evidence_site, "p0-long-username-abcdefghijklmnop")
            expect(p.locator("#claimButton")).to_be_visible()
            r.shot("workspace-idle")
            claim_next(p)
            p.locator('textarea[data-text="0"]').fill(ARABIC * 3)
            p.locator("#saveButton").click()
            expect(p.locator("#saveState")).to_have_text("Saved", timeout=15000)
            for size in VIEWPORTS:
                r.shot("workspace-long-arabic", size=size,
                       note="12 segments; 165+ character filename; mixed RTL/LTR transcript")
            r.shot("workspace-long-arabic-full", size=(1366, 768), full=True)
            p.locator("#sceneReviewPanel").get_by_role("button", name="Confirm", exact=True).click()
            expect(p.locator("#sceneReviewValidation")).to_be_visible()
            r.shot("workspace-review-validation", note="Confirmation without a selected scene")
            p.locator('#sceneReviewPanel input[value="airport"]').check()
            for i in range(1, 12):
                p.locator(f'textarea[data-text="{i}"]').fill(ARABIC)
            p.locator("#saveButton").click()
            expect(p.locator("#saveState")).to_have_text("Saved", timeout=15000)
            p.locator("#completeButton").click()
            expect(p.locator(".state h1")).to_have_text("Task completed", timeout=15000)
            r.shot("workspace-pool-empty")
            p.goto(evidence_site + "/completed.html")
            expect(p.locator("[data-view]").first).to_be_visible(timeout=15000)
            r.shot("history-list", size=(1440, 900))
            p.locator("[data-view]").first.click()
            expect(p.locator("#correctButton")).to_be_visible()
            r.shot("history-detail")
            r.shot("history-detail", size=(390, 844))
            p.set_viewport_size({"width": 1366, "height": 768})
            p.once("dialog", lambda dialog: dialog.accept())
            p.locator("#correctButton").click()
            expect(p.locator('textarea[data-text="0"]')).to_be_visible(timeout=15000)
            r.shot("workspace-correction-draft")
        finally:
            r.finish()
            browser.close()


def test_capture_login_loading_and_error(evidence_site):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        r = Recorder(browser, "login-states")
        p = r.page
        held = []
        try:
            p.route("**/api/leaderboard", lambda route: held.append(route))
            p.goto(evidence_site + "/login.html")
            expect(p.locator("#speedStatus")).to_contain_text("Loading")
            r.shot("login-statistics-loading", note="Leaderboard response intentionally held")
            for route in held:
                route.abort("failed")
            expect(p.locator("#speedStatus")).to_contain_text("Could not load annotation statistics.")
            r.shot("login-statistics-error", note="Injected network failure; login remains enabled")
            p.unroute("**/api/leaderboard")
        finally:
            r.finish()
            browser.close()


def dump_storage(page):
    return page.evaluate("""async () => {
      const db=await new Promise((ok,no)=>{const r=indexedDB.open('annotation-offline-v1');
        r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)});
      const result={name:db.name,version:db.version,stores:{},
        localStorage:Object.fromEntries(Object.entries(localStorage)),
        sessionStorageKeys:Object.keys(sessionStorage)};
      for(const name of db.objectStoreNames){
        const tx=db.transaction(name,'readonly'),store=tx.objectStore(name);
        const [keys,records]=await Promise.all([store.getAllKeys(),store.getAll()].map(r=>
          new Promise((ok,no)=>{r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})));
        result.stores[name]={keyPath:store.keyPath,autoIncrement:store.autoIncrement,
          indexes:Array.from(store.indexNames),keys,records};
      }
      db.close();return result;
    }""")


def test_capture_offline_conflict_and_takeover(evidence_site):
    seed_audio(2)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        r = Recorder(browser, "persistence")
        p = r.page
        try:
            login_annotator(p, evidence_site, "p0-offline")
            claim_next(p)
            p.locator("#fontUp").click()
            r.context.set_offline(True)
            p.locator('textarea[data-text="0"]').fill(ARABIC)
            p.locator("#saveButton").click()
            expect(p.locator("#saveState")).to_have_attribute("data-state", "error", timeout=15000)
            r.shot("workspace-offline-save-error", size=(1366, 768),
                   note="Intentional browser offline state; request failures expected")
            write_json("storage-offline-sample.json", dump_storage(p))
            r.context.set_offline(False)
            expect(p.locator("#saveState")).to_have_text("Saved", timeout=20000)
            write_json("storage-recovered-sample.json", dump_storage(p))
            p.route("**/api/assignment/current", lambda route: route.fulfill(
                status=409, content_type="application/json",
                body=json.dumps({"error": "P0 synthetic version conflict", "current_revision": 99}))
                if route.request.method == "PATCH" else route.continue_())
            p.locator('textarea[data-text="0"]').fill(ARABIC + " local conflicting edit")
            p.locator("#saveButton").click()
            expect(p.locator("#localConflictOverlay")).to_be_visible(timeout=15000)
            r.shot("workspace-revision-conflict", note="Injected 409 response; not a spontaneous baseline failure")
            r.shot("workspace-revision-conflict", size=(390, 844))
            p.unroute("**/api/assignment/current")
            other = browser.new_context(viewport={"width": 1440, "height": 900})
            q = other.new_page()
            q.goto(evidence_site + "/login.html")
            q.locator("#username").fill("p0-offline")
            q.locator("#joinBtn").click()
            expect(q.locator("#takeoverBtn")).to_be_visible(timeout=15000)
            original_page = r.page
            r.page = q
            r.shot("login-session-takeover", note="Same synthetic username on a second browser context")
            r.page = original_page
            other.close()
        finally:
            r.finish()
            browser.close()


def test_capture_admin(evidence_site, client, seed_tasks):
    queue_awaiting(client, seed_tasks, 1, original=ARABIC,
                  secondary="النص المختلف تماماً في هذا التسجيل. " + ARABIC)
    enable_cross_check(enabled=False, sampling_rate_bps=0)
    seed_audio(long_name=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        r = Recorder(browser, "admin")
        p = r.page
        try:
            p.goto(evidence_site + "/login.html")
            expect(p.locator("#chartFrame")).to_be_visible(timeout=15000)
            r.shot("login-populated-statistics", note="Synthetic submitted audio, current 28-day chart")
            p.goto(evidence_site + "/admin")
            expect(p.locator("#adminKey")).to_be_visible()
            r.shot("admin-login")
            login_admin(p, evidence_site)
            expect(p.locator("#kpiTotalAudio")).not_to_have_text("—", timeout=15000)
            for size in VIEWPORTS:
                r.shot("admin-overview", size=size)
            p.set_viewport_size({"width": 1440, "height": 900})
            p.locator('[data-view="annotators"]').click()
            r.shot("admin-annotators-unselected")
            p.locator('#sidebarAnnotatorList [data-annotator-id]').filter(has_text="alice").click()
            expect(p.locator("#scopeMode")).to_be_enabled(timeout=15000)
            r.shot("admin-annotator-scope")
            p.locator("#deactivateAnnotatorButton").click()
            expect(p.locator("#deactivateDialog")).to_be_visible()
            p.wait_for_timeout(250)
            r.shot("admin-deactivate-preview", note="Preview only; no deactivation executed")
            p.keyboard.press("Escape")
            p.locator('#annotatorTaskBody [data-task-action="view"]').first.click()
            expect(p.locator("#taskDialog")).to_be_visible()
            expect(p.locator("#revokeFromDetailButton")).to_be_visible(timeout=15000)
            r.shot("admin-task-detail")
            p.locator("#revokeFromDetailButton").click()
            expect(p.locator("#revokeDialog")).to_be_visible()
            p.wait_for_timeout(300)
            r.shot("admin-revoke-preview", note="Preview only; no revocation executed")
            p.keyboard.press("Escape")
            if p.locator("#taskDialog").is_visible():
                p.keyboard.press("Escape")
            for view in ("corpus", "quality", "activity"):
                p.locator(f'[data-view="{view}"]').click()
                expect(p.locator(f'[data-page="{view}"]')).to_be_visible()
                p.wait_for_timeout(500)
                r.shot(f"admin-{view}")
            open_cross_checks(p)
            expect(p.locator("[data-cc-round]").first).to_be_visible(timeout=15000)
            for size in ((1440, 900), (1366, 768), (390, 844)):
                r.shot("admin-cross-check-queue", size=size)
            p.set_viewport_size({"width": 1440, "height": 900})
            p.locator("#ccSamplingButton").click()
            expect(p.locator("#ccSamplingPercent")).to_be_visible()
            r.shot("admin-sampling-settings")
            p.keyboard.press("Escape")
            p.locator("[data-cc-round]").first.click()
            expect(p.locator("#ccReviewTitle")).to_be_visible(timeout=15000)
            r.shot("admin-cross-check-review")
            r.shot("admin-cross-check-review", size=(390, 844), full=True)
            p.set_viewport_size({"width": 1440, "height": 900})
            p.goto(evidence_site + "/admin?view=corpus&range=custom&from=2000-01-01&to=2000-01-02")
            expect(p.locator("#corpusTaskState")).to_contain_text("No tasks", timeout=15000)
            r.shot("admin-corpus-empty-date-range")
        finally:
            r.finish()
            browser.close()


@pytest.mark.parametrize("count", [100, 500, 1000])
def test_capture_segment_scale(evidence_site, count):
    seed_audio(count)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        r = Recorder(browser, f"scale-{count}")
        p = r.page
        try:
            login_annotator(p, evidence_site, f"p0-scale-{count}")
            started = time.perf_counter()
            claim_next(p)
            expect(p.locator("textarea[data-text]")).to_have_count(count)
            claim_ms = (time.perf_counter() - started) * 1000
            p.locator('textarea[data-text="0"]').focus()
            p.evaluate("""() => {
              window.p0InputSamples=[];
              document.querySelector('textarea[data-text="0"]').addEventListener('input',()=>{
                const t=performance.now(); requestAnimationFrame(()=>requestAnimationFrame(()=>
                  window.p0InputSamples.push(performance.now()-t)));
              }, {capture:true});
            }""")
            for i in range(24):
                p.keyboard.insert_text("م")
                p.wait_for_function("n=>window.p0InputSamples.length>=n", arg=i + 1)
            samples = p.evaluate("window.p0InputSamples")
            p.locator("#saveButton").click()
            expect(p.locator("#saveState")).to_have_text("Saved", timeout=15000)
            r.shot(f"workspace-{count}-segments", size=(1366, 768))
            write_json(f"scale-{count}.json", {"synthetic": True, "segments": count,
                "claim_to_all_rows_ms": round(claim_ms, 2), "input_samples_ms": samples,
                "input_median_ms": round(statistics.median(samples), 2),
                "input_p95_ms": round(sorted(samples)[int(len(samples) * .95)], 2),
                "input_max_ms": round(max(samples), 2),
                "measurement": "input capture listener to second requestAnimationFrame; 24 real keyboard.insert_text inputs; unthrottled headless Chromium; not production INP",
                "dom_elements": p.locator("*").count(),
                "rendered_textareas": p.locator("textarea[data-text]").count(),
                "browser": browser.version})
        finally:
            r.finish()
            browser.close()
