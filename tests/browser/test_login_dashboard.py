"""Playwright checks for the login-page annotation speed chart."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect, sync_playwright

import annotation_repository as repo
import db
from annotation_metadata.taxonomy import SCENE_ORDER
from tests.browser.conftest import insert_source
from tests.browser.test_scene_workflow import _login_annotator

SCENE_LABELS = {
    "restaurant": "Restaurant",
    "hotel": "Hotel",
    "taxi": "Taxi",
    "airport": "Airport",
    "clinic": "Clinic",
    "tourism_information": "Tourism information",
    "emergencies": "Emergencies",
    "spoken_languages": "Spoken languages",
    "business_negotiation": "Business negotiation",
    "shopping": "Shopping",
}


def _segments(assignment, prefix="text"):
    return [
        {
            "id": segment["id"],
            "start": segment["start"],
            "end": segment["end"],
            "duration": segment["duration"],
            "text": f"{prefix} {segment['id']}",
            "exclude_from_training": False,
        }
        for segment in assignment["segments"]
    ]


def _complete_and_date(user, submitted_at):
    assignment = repo.claim(user["fence"])
    repo.complete(
        user["fence"],
        assignment["lease_token"],
        assignment["revision"],
        "annotated",
        [],
        _segments(assignment, "dash"),
        str(uuid.uuid4()),
        f"dash-{uuid.uuid4()}",
    )
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions AS version
                  SET submitted_at = %s
                 FROM annotation_tasks AS task
                WHERE task.id = %s
                  AND version.id = task.current_published_version_id""",
            (submitted_at, assignment["task_id"]),
        )
        conn.commit()
    return assignment["task_id"]


def _seed_chart_rows(*, today_duration=10.0):
    user = repo.login("dashboard-seed", str(uuid.uuid4()), 1800)
    now = datetime.now(timezone.utc)
    _complete_and_date(user, now - timedelta(days=20))
    today_id = _complete_and_date(user, now)
    if today_duration != 10.0:
        with db.db_conn() as conn:
            conn.execute(
                "UPDATE annotation_tasks SET duration = %s WHERE id = %s",
                (today_duration, today_id),
            )
            conn.commit()
    return today_id


def _read_today_tooltip():
    return """() => {
      const chart = window.LoginDashboard.getChart();
      const index = chart.data.labels.length - 1;
      const meta = chart.getDatasetMeta(0).data[index];
      chart.setActiveElements([]);
      chart.tooltip.setActiveElements([], {x: 0, y: 0});
      chart.update('none');
      chart.setActiveElements([{datasetIndex: 0, index: index}]);
      chart.tooltip.setActiveElements(
        [{datasetIndex: 0, index: index}],
        {x: meta.x, y: meta.y}
      );
      chart.update('none');
      const body = (chart.tooltip.body || []).map(item => (item.lines || []).join(' '));
      return {
        hours: chart.data.datasets[0].data[index],
        title: chart.tooltip.title || [],
        body: body,
        store: window.LoginDashboard.tooltipLines(
          window.LoginDashboard.currentSpeed(), index
        ),
      };
    }"""


def _collect_page_errors(page):
    failures = []
    page.on("pageerror", lambda error: failures.append(f"pageerror:{error}"))
    page.on("console", lambda message: (
        failures.append(f"console:{message.type}:{message.text}")
        if message.type == "error" else None
    ))
    return failures


def test_login_dashboard_desktop_and_mobile_chart(provenance_site):
    url = provenance_site["url"]
    _seed_chart_rows()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            reduced_motion="reduce",
        )
        page = context.new_page()
        failures = _collect_page_errors(page)
        page.goto(url + "/login.html")
        expect(page.locator("#username")).to_be_focused()
        page.wait_for_function(
            """() => window.LoginDashboard && window.LoginDashboard.getChart()
              && window.LoginDashboard.getChart().getDatasetMeta(0).data.length === 28""",
            timeout=15000,
        )
        expect(page.locator("#speedChart")).to_be_visible()
        layout = page.evaluate(
            """() => {
              const login = document.querySelector('.login-pane').getBoundingClientRect();
              const dash = document.querySelector('.dashboard-pane').getBoundingClientRect();
              return {
                sideBySide: dash.left >= login.right - 1,
                stacked: dash.top >= login.bottom - 1,
                overflow: document.documentElement.scrollWidth > innerWidth + 1,
                conflictInLogin: Boolean(document.querySelector('.login-pane #conflictBox')),
              };
            }"""
        )
        assert layout["sideBySide"] is True
        assert layout["stacked"] is False
        assert layout["overflow"] is False
        assert layout["conflictInLogin"] is True

        filter_layout = page.evaluate(
            """() => {
              const title = document.querySelector('.dash-title').getBoundingClientRect();
              const sub = document.querySelector('#dashSub').getBoundingClientRect();
              const filter = document.querySelector('.scene-filter').getBoundingClientRect();
              const lbTitle = document.querySelector('.lb-title').getBoundingClientRect();
              const total = document.querySelector('.lb-total').getBoundingClientRect();
              return {
                filterRightOfTitle: filter.left >= Math.max(title.right, sub.right) - 1,
                filterSameBand: filter.top < title.bottom + 40,
                totalRightOfTitle: total.left >= lbTitle.right - 1,
                totalSameRow: lbTitle.bottom > total.top && total.bottom > lbTitle.top,
                options: [...document.querySelectorAll('#sceneSelect option')].map(
                  item => ({value: item.value, text: item.textContent.trim()})
                ),
                subtitle: document.getElementById('dashSub').textContent,
                totalText: document.getElementById('lbTotalValue').textContent,
                totalTitle: document.getElementById('lbTotal').getAttribute('title'),
                totalAria: document.getElementById('lbTotal').getAttribute('aria-label'),
              };
            }"""
        )
        assert filter_layout["filterRightOfTitle"] is True
        assert filter_layout["filterSameBand"] is True
        assert filter_layout["totalRightOfTitle"] is True
        assert filter_layout["totalSameRow"] is True
        assert filter_layout["options"][0] == {"value": "", "text": "All scenes"}
        assert [item["value"] for item in filter_layout["options"][1:]] == list(SCENE_ORDER)
        assert [item["text"] for item in filter_layout["options"][1:]] == [
            SCENE_LABELS[code] for code in SCENE_ORDER
        ]
        assert filter_layout["subtitle"] == "All scenes · Last 28 days"
        assert "h" in filter_layout["totalText"] and "min" in filter_layout["totalText"]
        assert filter_layout["totalTitle"] == "Excludes audio marked abnormal"
        assert "excluding audio marked abnormal" in filter_layout["totalAria"].lower()
        assert "hours" in filter_layout["totalAria"].lower()

        chart = page.evaluate(
            """() => {
              const chart = window.LoginDashboard.getChart();
              const meta = chart.getDatasetMeta(0);
              const annotations = chart.options.plugins.annotation.annotations;
              const colors = chart.data.datasets[0].backgroundColor;
              const today = colors.length - 1;
              return {
                bars: meta.data.length,
                annotations: Object.keys(annotations).length,
                dashes: Object.values(annotations).map(item => item.borderDash),
                yZero: chart.options.scales.y.beginAtZero,
                yTitle: chart.options.scales.y.title.text,
                xTitle: chart.options.scales.x.title.text,
                todayAlpha: colors[today],
                earlierAlpha: colors[0],
                yMin: chart.scales.y.min,
              };
            }"""
        )
        assert chart["bars"] == 28
        assert chart["annotations"] == 4
        assert chart["dashes"] == [[6, 4], [6, 4], [6, 4], [6, 4]]
        assert chart["yZero"] is True
        assert chart["yMin"] == 0
        assert chart["yTitle"] == "New annotated audio (hours)"
        assert chart["xTitle"] == "Date"
        assert "0.58" in str(chart["todayAlpha"])
        assert "0.58" not in str(chart["earlierAlpha"])

        tooltip = page.evaluate(
            """() => {
              const chart = window.LoginDashboard.getChart();
              const index = chart.data.labels.length - 1;
              const speed = {
                days: chart.data.labels.map((date, i) => ({
                  date,
                  duration_seconds: chart.data.datasets[0].data[i] * 3600,
                  is_partial: i === index,
                })),
                weeks: [0, 1, 2, 3].map(function (weekIndex) {
                  const ann = chart.options.plugins.annotation.annotations['weekAvg' + weekIndex];
                  return {average_daily_duration_seconds: ann.yMin * 3600};
                }),
              };
              const lines = window.LoginDashboard.tooltipLines(speed, index);
              const meta = chart.getDatasetMeta(0).data[index];
              chart.setActiveElements([{datasetIndex: 0, index: index}]);
              chart.tooltip.setActiveElements(
                [{datasetIndex: 0, index: index}],
                {x: meta.x, y: meta.y}
              );
              chart.update('none');
              const body = (chart.tooltip.body || []).map(item => (item.lines || []).join(' '));
              return {
                lines: lines,
                title: chart.tooltip.title || [],
                body: body,
              };
            }"""
        )
        joined = "\n".join(tooltip["lines"] + tooltip["title"] + tooltip["body"])
        assert "Daily added:" in joined
        assert "7-day average:" in joined
        assert "h/day" in joined
        assert "Today so far" in joined
        assert tooltip["lines"][0].endswith(", 2026") or "," in tooltip["lines"][0]

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        mobile = page.evaluate(
            """() => {
              const login = document.querySelector('.login-pane').getBoundingClientRect();
              const dash = document.querySelector('.dashboard-pane').getBoundingClientRect();
              const chart = window.LoginDashboard.getChart();
              return {
                stacked: dash.top >= login.bottom - 1,
                sideBySide: dash.left >= login.right - 1,
                overflow: document.documentElement.scrollWidth > innerWidth + 1,
                maxTicks: chart.options.scales.x.ticks.maxTicksLimit,
                height: document.querySelector('.chart-frame').getBoundingClientRect().height,
              };
            }"""
        )
        assert mobile["stacked"] is True
        assert mobile["sideBySide"] is False
        assert mobile["overflow"] is False
        assert mobile["maxTicks"] == 4
        assert 270 <= mobile["height"] <= 290
        labels = page.evaluate(
            """() => {
              const chart = window.LoginDashboard.getChart();
              const anns = Object.values(chart.options.plugins.annotation.annotations);
              return anns.map(item => ({
                font: item.label.font.size,
                bg: item.label.backgroundColor,
                position: item.label.position,
                content: item.label.content,
              }));
            }"""
        )
        assert labels
        assert all(item["font"] >= 11 for item in labels)
        assert all(item["position"] == "center" for item in labels)
        assert all(
            item["bg"] not in ("transparent", "rgba(0, 0, 0, 0)", "")
            for item in labels
        )
        assert all(str(item["content"]).endswith("h") for item in labels)
        assert all("h/day" not in str(item["content"]) for item in labels)
        mobile_filter = page.evaluate(
            """() => {
              const titleBlock = document.querySelector('.dash-head > div').getBoundingClientRect();
              const head = document.querySelector('.dash-head').getBoundingClientRect();
              const filter = document.querySelector('.scene-filter').getBoundingClientRect();
              const lbTitle = document.querySelector('.lb-title').getBoundingClientRect();
              const total = document.querySelector('.lb-total').getBoundingClientRect();
              const long = document.querySelector('.lb-total-long');
              return {
                filterBelow: filter.top >= titleBlock.bottom - 1,
                overflow: document.documentElement.scrollWidth > innerWidth + 1,
                filterWidth: filter.width,
                headWidth: head.width,
                totalRight: total.left >= lbTitle.right - 1,
                overlap: lbTitle.right > total.left + 1 && lbTitle.bottom > total.top + 1
                  && total.bottom > lbTitle.top + 1,
                longDisplay: getComputedStyle(long).display,
                totalWraps: document.getElementById('lbTotalValue').getClientRects().length > 1,
              };
            }"""
        )
        assert mobile_filter["filterBelow"] is True
        assert mobile_filter["overflow"] is False
        assert mobile_filter["filterWidth"] >= mobile_filter["headWidth"] - 2
        assert mobile_filter["totalRight"] is True
        assert mobile_filter["overlap"] is False
        assert mobile_filter["longDisplay"] == "none"
        assert mobile_filter["totalWraps"] is False
        assert not failures, failures
        browser.close()


def test_empty_and_failure_states_do_not_block_login(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1366, "height": 768},
            reduced_motion="reduce",
        )
        page.goto(url + "/login.html")
        expect(page.locator("#speedStatus")).to_contain_text(
            "No annotations in the last 28 days.", timeout=15000,
        )
        expect(page.locator("#lbTotalValue")).to_have_text("0 h 00 min", timeout=15000)
        expect(page.locator("#username")).to_be_visible()
        expect(page.locator("#joinBtn")).to_be_enabled()

        page.route("**/api/leaderboard", lambda route: route.abort("failed"))
        page.reload()
        expect(page.locator("#speedStatus")).to_contain_text(
            "Could not load annotation statistics.", timeout=15000,
        )
        expect(page.locator("#lbTotalValue")).to_have_text("Unavailable", timeout=15000)
        page.unroute("**/api/leaderboard")

        _seed_chart_rows()
        failing = browser.new_page(
            viewport={"width": 390, "height": 844},
            reduced_motion="reduce",
        )
        failing.route(
            "**/static/vendor/**",
            lambda route: route.abort("failed"),
        )
        failing.goto(url + "/login.html")
        expect(failing.locator("#speedStatus")).to_contain_text(
            "Could not load annotation statistics.", timeout=15000,
        )
        expect(failing.locator("#username")).to_be_editable()
        failing.locator("#username").fill("chart-js-missing")
        failing.locator("#joinBtn").click()
        expect(failing).not_to_have_url("**/login.html", timeout=15000)
        expect(failing.get_by_role("button", name="Log out")).to_be_visible()
        browser.close()


def test_tooltip_follows_successful_refresh(provenance_site):
    url = provenance_site["url"]
    today_id = _seed_chart_rows(today_duration=3600)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1366, "height": 768},
            reduced_motion="reduce",
        )
        page.goto(url + "/login.html")
        page.wait_for_function(
            """() => window.LoginDashboard && window.LoginDashboard.getChart()
              && window.LoginDashboard.getChart().getDatasetMeta(0).data.length === 28""",
            timeout=15000,
        )
        first = page.evaluate(_read_today_tooltip())
        first_text = "\n".join(first["store"] + first["title"] + first["body"])
        assert first["hours"] == 1
        assert "1 h 00 min" in first_text
        with db.db_conn() as conn:
            conn.execute(
                "UPDATE annotation_tasks SET duration = %s WHERE id = %s",
                (7200, today_id),
            )
            conn.commit()
        page.evaluate("() => window.LoginDashboard.load()")
        page.wait_for_function(
            """() => window.LoginDashboard.getChart()
              && window.LoginDashboard.getChart().data.datasets[0].data.at(-1) === 2""",
            timeout=15000,
        )
        second = page.evaluate(_read_today_tooltip())
        second_text = "\n".join(second["store"] + second["title"] + second["body"])
        assert second["hours"] == 2
        assert "2 h 00 min" in second_text
        assert "2 h 00 min" in "\n".join(second["body"] + second["store"])
        assert "1 h 00 min" not in "\n".join(second["body"])
        browser.close()


def test_stale_chart_survives_refresh_failure(provenance_site):
    url = provenance_site["url"]
    _seed_chart_rows()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1366, "height": 768},
            reduced_motion="reduce",
        )
        page.goto(url + "/login.html")
        expect(page.locator("#speedChart")).to_be_visible(timeout=15000)
        page.evaluate(
            """() => {
              window.__barCount = window.LoginDashboard.getChart()
                .getDatasetMeta(0).data.length;
            }"""
        )
        page.route("**/api/leaderboard", lambda route: route.abort("failed"))
        page.evaluate("() => window.LoginDashboard.load()")
        page.wait_for_timeout(300)
        still = page.evaluate(
            """() => {
              const chart = window.LoginDashboard.getChart();
              return {
                visible: Boolean(chart),
                bars: chart ? chart.getDatasetMeta(0).data.length : 0,
                hidden: document.getElementById('chartFrame').hidden,
              };
            }"""
        )
        assert still["visible"] is True
        assert still["bars"] == 28
        assert still["hidden"] is False
        expect(page.locator("#lbTotalValue")).not_to_have_text("Unavailable")
        expect(page.locator("#lbTotalValue")).not_to_have_text("—")
        browser.close()


def test_login_from_dashboard_still_reaches_workspace(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        _login_annotator(page, url, "dashboard-login")
        expect(page.locator("#claimButton")).to_be_visible()
        browser.close()


def _current_scene(task_id):
    with db.db_conn() as conn:
        row = conn.execute(
            """SELECT scene_code FROM task_sources
               WHERE task_id = %s AND is_current
               ORDER BY id LIMIT 1""",
            (task_id,),
        ).fetchone()
    return row[0] if row else None


def _seed_distinct_scene_rows(seed_tasks, provenance_tasks):
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET eligible = false WHERE id = ANY(%s)",
            (list(provenance_tasks),),
        )
        conn.commit()
    airport_id = seed_tasks(1, folder="dash-airport", duration=3600)[0]
    shopping_id = seed_tasks(1, folder="dash-shopping", duration=1800)[0]
    insert_source(
        airport_id, scene="airport", confidence="high", batch="dash-air-only",
    )
    insert_source(
        shopping_id, scene="shopping", confidence="high", batch="dash-shop-only",
    )
    user = repo.login("scene-dash", str(uuid.uuid4()), 1800)
    now = datetime.now(timezone.utc)
    today_id = _complete_and_date(user, now)
    older_id = _complete_and_date(user, now - timedelta(days=3))
    today_scene = _current_scene(today_id)
    older_scene = _current_scene(older_id)
    return {
        "today_scene": today_scene,
        "older_scene": older_scene,
        "today_hours": 1.0 if today_scene == "airport" else 0.5,
        "older_hours": 1.0 if older_scene == "airport" else 0.5,
    }


def test_scene_filter_updates_chart_without_refetch(provenance_site, seed_tasks):
    url = provenance_site["url"]
    seeded = _seed_distinct_scene_rows(seed_tasks, provenance_site["tasks"])
    assert seeded["today_scene"] in {"airport", "shopping"}
    assert seeded["older_scene"] in {"airport", "shopping"}
    assert seeded["today_scene"] != seeded["older_scene"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1366, "height": 768},
            reduced_motion="reduce",
        )
        failures = _collect_page_errors(page)
        page.goto(url + "/login.html")
        page.wait_for_function(
            """() => window.LoginDashboard && window.LoginDashboard.getChart()
              && window.LoginDashboard.getChart().getDatasetMeta(0).data.length === 28""",
            timeout=15000,
        )
        all_scenes = page.evaluate(
            """() => {
              const chart = window.LoginDashboard.getChart();
              const anns = chart.options.plugins.annotation.annotations;
              return {
                data: chart.data.datasets[0].data.slice(),
                week0: anns.weekAvg0.yMin,
                week3: anns.weekAvg3.yMin,
                subtitle: document.getElementById('dashSub').textContent,
              };
            }"""
        )
        assert all_scenes["data"][-1] == seeded["today_hours"]
        assert all_scenes["data"][-4] == seeded["older_hours"]
        assert all_scenes["subtitle"] == "All scenes · Last 28 days"

        page.route("**/api/leaderboard", lambda route: route.abort("failed"))
        page.locator("#sceneSelect").select_option("airport")
        airport = page.evaluate(
            """() => {
              const chart = window.LoginDashboard.getChart();
              const anns = chart.options.plugins.annotation.annotations;
              const index = chart.data.labels.length - 1;
              const meta = chart.getDatasetMeta(0).data[index];
              chart.setActiveElements([]);
              chart.tooltip.setActiveElements([], {x: 0, y: 0});
              chart.update('none');
              return {
                data: chart.data.datasets[0].data.slice(),
                week0: anns.weekAvg0.yMin,
                week3: anns.weekAvg3.yMin,
                subtitle: document.getElementById('dashSub').textContent,
                selected: window.LoginDashboard.selectedScene(),
                active: (chart.tooltip.getActiveElements
                  ? chart.tooltip.getActiveElements()
                  : []).length,
              };
            }"""
        )
        assert airport["selected"] == "airport"
        assert airport["subtitle"] == "Airport · Last 28 days"
        if seeded["today_scene"] == "airport":
            assert airport["data"][-1] == 1
            assert airport["data"][-4] == 0
        else:
            assert airport["data"][-1] == 0
            assert airport["data"][-4] == 1
        assert airport["data"] != all_scenes["data"]
        assert airport["week3"] != all_scenes["week3"]
        assert airport["active"] == 0

        page.locator("#sceneSelect").select_option("clinic")
        expect(page.locator("#speedStatus")).to_contain_text(
            "No annotations for Clinic in the last 28 days.",
        )
        expect(page.locator("#chartFrame")).to_be_hidden()
        expect(page.locator("#sceneSelect")).to_be_enabled()
        assert page.evaluate("() => window.LoginDashboard.getChart()") is None
        expect(page.locator("#dashSub")).to_have_text("Clinic · Last 28 days")

        page.locator("#sceneSelect").select_option(label="All scenes")
        page.wait_for_function(
            """() => window.LoginDashboard.getChart()
              && window.LoginDashboard.selectedScene() === ''""",
            timeout=15000,
        )
        restored = page.evaluate(
            """() => ({
              data: window.LoginDashboard.getChart().data.datasets[0].data.slice(),
              subtitle: document.getElementById('dashSub').textContent,
              selected: window.LoginDashboard.selectedScene(),
            })"""
        )
        assert restored["selected"] == ""
        assert restored["subtitle"] == "All scenes · Last 28 days"
        assert restored["data"][-1] == seeded["today_hours"]
        assert restored["data"][-4] == seeded["older_hours"]
        assert not failures, failures
        browser.close()


def test_scene_selection_survives_refresh(provenance_site, seed_tasks):
    url = provenance_site["url"]
    seeded = _seed_distinct_scene_rows(seed_tasks, provenance_site["tasks"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1366, "height": 768},
            reduced_motion="reduce",
        )
        page.goto(url + "/login.html")
        page.wait_for_function(
            """() => window.LoginDashboard && window.LoginDashboard.getChart()
              && window.LoginDashboard.getChart().getDatasetMeta(0).data.length === 28""",
            timeout=15000,
        )
        page.locator("#sceneSelect").select_option("airport")
        expected_today = 1 if seeded["today_scene"] == "airport" else 0
        page.wait_for_function(
            """(expected) => window.LoginDashboard.selectedScene() === 'airport'
              && window.LoginDashboard.getChart()
              && window.LoginDashboard.getChart().data.datasets[0].data.at(-1) === expected""",
            arg=expected_today,
            timeout=15000,
        )
        page.evaluate("() => window.LoginDashboard.load()")
        page.wait_for_function(
            """() => window.LoginDashboard.selectedScene() === 'airport'
              && document.getElementById('sceneSelect').value === 'airport'
              && document.getElementById('dashSub').textContent === 'Airport · Last 28 days'""",
            timeout=15000,
        )
        still = page.evaluate(
            """() => ({
              selected: window.LoginDashboard.selectedScene(),
              value: document.getElementById('sceneSelect').value,
              today: window.LoginDashboard.getChart().data.datasets[0].data.at(-1),
            })"""
        )
        assert still["selected"] == "airport"
        assert still["value"] == "airport"
        assert still["today"] == expected_today
        browser.close()
