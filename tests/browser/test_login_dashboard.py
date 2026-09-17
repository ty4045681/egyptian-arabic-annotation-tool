"""Playwright checks for the login-page annotation speed chart."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect, sync_playwright

import annotation_repository as repo
import db
from tests.browser.test_scene_workflow import _login_annotator


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
        expect(page.locator("#username")).to_be_visible()
        expect(page.locator("#joinBtn")).to_be_enabled()

        page.route("**/api/leaderboard", lambda route: route.abort("failed"))
        page.reload()
        expect(page.locator("#speedStatus")).to_contain_text(
            "Could not load annotation statistics.", timeout=15000,
        )
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
        browser.close()


def test_login_from_dashboard_still_reaches_workspace(provenance_site):
    url = provenance_site["url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        _login_annotator(page, url, "dashboard-login")
        expect(page.locator("#claimButton")).to_be_visible()
        browser.close()
