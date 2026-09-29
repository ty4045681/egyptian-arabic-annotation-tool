"""Cross-check reference interactions against the real queue and settings APIs."""

from playwright.sync_api import expect, sync_playwright

from tests.browser.admin_helpers import choose
from tests.browser.cross_check_helpers import (
    login_admin,
    open_cross_checks,
    queue_awaiting,
    screenshot,
    seed_originals,
)
from tests.test_api import login
from tests.test_cross_check_submit import complete, text_segments, words


def test_reference_queue_live_filters_counts_and_mobile(cross_check_site):
    client = cross_check_site["client"]
    rounds = queue_awaiting(client, cross_check_site["seed_tasks"], 2)
    seed_originals(client, cross_check_site["seed_tasks"], 1, words(100))
    login(client, "bob")
    assignment = client.post(
        "/api/assignment/claim", json={"source_scene": "airport"}
    ).json
    response, _ = complete(
        client, assignment, segments=text_segments(assignment, words(100))
    )
    assert response.json["cross_check"]["state"] == "passed"
    client.post("/api/logout", json={})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(
            "window.violations=[];document.addEventListener('securitypolicyviolation',e=>window.violations.push(e.violatedDirective))"
        )
        login_admin(page, cross_check_site["url"])
        open_cross_checks(page)
        table = page.get_by_test_id("cross-check-table")
        rows = table.locator(".ant-table-row")
        expect(rows).to_have_count(2)
        expect(table).to_contain_text("audio-000.wav")
        expect(table).to_contain_text("Airport")
        expect(
            table.get_by_role("columnheader", name="Original → Cross-check")
        ).to_be_visible()
        expect(
            page.get_by_role("tab", name="Awaiting review", exact=True)
        ).to_contain_text("2")
        expect(page.get_by_role("tab", name="Passed", exact=True)).to_contain_text("1")
        expect(page.locator(".cc-metric").filter(has_text="Pass rate")).to_contain_text(
            "33%"
        )
        expect(
            page.locator(".cc-metric").filter(has_text="Training export on hold")
        ).to_contain_text("20s")
        expect(
            page.locator(".cc-metric").filter(has_text="Cross-check submissions")
        ).to_contain_text("30s labor")
        expect(rows.first).to_contain_text("11.0%")
        expect(rows.first).to_contain_text("Diff > 10%")
        page.get_by_role("tab", name="All", exact=True).click()
        expect(rows).to_have_count(3)
        choose(page, '[aria-label="Date range"]', "Last 7 days")
        expect(page.get_by_role("button", name="Remove Last 7 days")).to_be_visible()
        page.get_by_role("button", name="Reset filters", exact=True).click()
        expect(rows).to_have_count(2)
        screenshot(page, "reference-queue-desktop.png")
        page.locator("#ccSearch").fill(rounds[0]["round_id"])
        expect(rows).to_have_count(1)
        expect(
            page.locator(f'[data-cc-round="{rounds[0]["round_id"]}"]')
        ).to_be_visible()
        page.get_by_role("button", name="More filters", exact=True).click()
        choose(page, '[aria-label="Reason"]', "Submission status differs")
        expect(rows).to_have_count(0)
        expect(table).to_contain_text("No rounds match this state and filters.")
        page.get_by_role("button", name="Reset filters", exact=True).click()
        expect(rows).to_have_count(2)
        page.go_back()
        expect(page.locator("#ccSearch")).to_have_value(rounds[0]["round_id"])
        expect(rows).to_have_count(0)
        page.get_by_role("button", name="Reset filters", exact=True).click()
        page.get_by_role("group", name="Scene", exact=True).get_by_role("button", name="Airport", exact=True).click()
        page.get_by_role("group", name="Scene", exact=True).get_by_role("button", name="Clinic", exact=True).click()
        expect(rows).to_have_count(2)
        page.get_by_role("button", name="Remove Airport", exact=True).click()
        expect(rows).to_have_count(0)
        page.get_by_role("button", name="Remove Clinic", exact=True).click()
        expect(rows).to_have_count(2)
        page.get_by_role("group", name="Scene", exact=True).get_by_role("button", name="Airport", exact=True).click()
        page.get_by_role("button", name="More filters", exact=True).click()
        choose(page, '[aria-label="Original annotator"]', "alice")
        choose(page, '[aria-label="Cross-check annotator"]', "bob")
        expect(rows).to_have_count(2)
        choose(page, '[aria-label="Date range"]', "Custom range")
        page.locator("#ccFrom").fill("2030-01-01")
        page.locator("#ccTo").fill("2020-01-01")
        page.get_by_role("button", name="Apply dates", exact=True).click()
        expect(page.get_by_text("Choose a valid start and end date.")).to_be_visible()
        page.locator("#ccFrom").fill("2020-01-01")
        page.locator("#ccTo").fill("2030-01-01")
        with page.expect_request(
            lambda request: "/api/admin/cross-checks?" in request.url
            and "to=2030-01-02" in request.url
        ):
            page.get_by_role("button", name="Apply dates", exact=True).click()
        expect(rows).to_have_count(2)
        page.get_by_role("tab", name="Passed", exact=True).click()
        expect(rows).to_have_count(1)
        expect(
            rows.first.get_by_role("button", name="View", exact=True)
        ).to_be_visible()
        expect(page.locator("#ccFrom")).to_have_value("2020-01-01")
        page.get_by_role("tab", name="Awaiting review", exact=True).click()
        expect(rows).to_have_count(2)
        rows.first.get_by_role("button", name="Review", exact=True).click()
        expect(page.locator("#ccReviewTitle")).to_be_visible()
        page.locator("#ccBackToList").click()
        expect(rows).to_have_count(2)
        expect(page.locator("#ccFrom")).to_have_value("2020-01-01")
        page.locator("#ccSearch").fill("no-matching-file")
        expect(rows).to_have_count(0)
        page.reload()
        expect(page.locator("#ccSearch")).to_have_value("no-matching-file")
        expect(table).to_contain_text("No rounds match this state and filters.")
        page.get_by_role("button", name="Reset filters", exact=True).click()
        expect(rows).to_have_count(2)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot(page, "reference-queue-mobile.png")
        page.emulate_media(color_scheme="dark")
        expect(page.locator("html")).to_have_attribute("data-admin-mode", "dark")
        screenshot(page, "reference-queue-dark.png")
        page.locator("#ccSamplingButton").click()
        modal = page.get_by_role("dialog", name="Cross-check sampling")
        expect(modal).to_be_visible()
        expect(page.locator("#ccSamplingHint")).to_have_text("≈ 1 in 1 claims")
        screenshot(page, "reference-settings-mobile-dark.png")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        assert not page.evaluate("window.violations")
        browser.close()
