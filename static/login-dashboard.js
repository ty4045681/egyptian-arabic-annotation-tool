/* Login-page leaderboard and 28-day annotation-speed chart.
 * Login, takeover, and Enter-to-submit live in login.html and must not
 * depend on Chart.js loading.
 */
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var BAR_FILL = "rgb(129, 140, 248)";
  var BAR_FILL_TODAY = "rgba(129, 140, 248, 0.58)";
  var WEEK_LINE = "#fbbf24";
  var ALL_SCENES_LABEL = "All scenes";
  var MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  var speedChart = null;
  var lastSpeed = null;
  var lastPayload = null;
  var selectedScene = "";
  var totalLoaded = false;
  var inflight = null;
  var sceneFilterBound = false;

  function $(id) {
    return document.getElementById(id);
  }

  function cssVar(name, fallback) {
    try {
      var value = getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
      return value || fallback;
    } catch (err) {
      return fallback;
    }
  }

  function reduceMotion() {
    return Boolean(
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function isNarrow() {
    return window.innerWidth <= 760;
  }

  function esc(value) {
    var node = document.createElement("div");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatHoursMinutes(seconds) {
    var total = Math.max(0, Math.round(Number(seconds || 0) / 60));
    return Math.floor(total / 60) + " h " + String(total % 60).padStart(2, "0") + " min";
  }

  function formatHoursMinutesSpoken(seconds) {
    var total = Math.max(0, Math.round(Number(seconds || 0) / 60));
    return Math.floor(total / 60) + " hours " + (total % 60) + " minutes";
  }

  function parseISODate(iso) {
    var parts = String(iso || "").split("-");
    return {
      y: Number(parts[0]) || 0,
      m: Number(parts[1]) || 0,
      d: Number(parts[2]) || 0,
    };
  }

  function formatLongDate(iso) {
    var parsed = parseISODate(iso);
    if (!parsed.y || !parsed.m || !parsed.d) return String(iso || "");
    return MONTHS[parsed.m - 1] + " " + parsed.d + ", " + parsed.y;
  }

  function formatTickDate(iso) {
    var parsed = parseISODate(iso);
    if (!parsed.y || !parsed.m || !parsed.d) return String(iso || "");
    return MONTHS[parsed.m - 1] + " " + parsed.d;
  }

  function hoursFromSeconds(seconds) {
    return Number(seconds || 0) / 3600;
  }

  function formatHoursPerDay(seconds) {
    return hoursFromSeconds(seconds).toFixed(1) + " h/day";
  }

  function formatWeekLabel(seconds) {
    var hours = hoursFromSeconds(seconds).toFixed(1);
    return isNarrow() ? hours + "h" : hours + " h/day";
  }

  function currentSpeed() {
    return lastSpeed;
  }

  function weekForIndex(weeks, index) {
    if (!weeks || !weeks.length) return null;
    var slot = Math.floor(Number(index) / 7);
    if (slot < 0) slot = 0;
    if (slot >= weeks.length) slot = weeks.length - 1;
    return weeks[slot];
  }

  function tooltipLines(speed, index) {
    var days = speed && speed.days ? speed.days : [];
    var day = days[index];
    if (!day) return [];
    var week = weekForIndex(speed.weeks, index);
    var lines = [
      formatLongDate(day.date),
      "Daily added: " + formatHoursMinutes(day.duration_seconds),
    ];
    if (week) {
      lines.push("7-day average: " + formatHoursPerDay(week.average_daily_duration_seconds));
    }
    if (day.is_partial) lines.push("Today so far");
    return lines;
  }

  function setSpeedStatus(text) {
    var status = $("speedStatus");
    if (!status) return;
    status.textContent = text || "";
  }

  function showLegend(show) {
    var legend = $("chartLegend");
    if (!legend) return;
    if (show) legend.classList.add("show");
    else legend.classList.remove("show");
  }

  function destroyChart() {
    if (speedChart && typeof speedChart.destroy === "function") {
      speedChart.destroy();
    }
    speedChart = null;
    var canvas = $("speedChart");
    if (canvas && window.Chart && typeof Chart.getChart === "function") {
      var leftover = Chart.getChart(canvas);
      if (leftover) leftover.destroy();
    }
  }

  function chartAvailable() {
    return typeof window.Chart === "function";
  }

  function sceneLabel(code, options) {
    if (!code) return ALL_SCENES_LABEL;
    var i;
    for (i = 0; i < (options || []).length; i += 1) {
      if (options[i] && options[i].code === code) {
        return options[i].label || code;
      }
    }
    return code;
  }

  function knownScene(code, options) {
    if (!code) return true;
    var i;
    for (i = 0; i < (options || []).length; i += 1) {
      if (options[i] && options[i].code === code) return true;
    }
    return false;
  }

  function updateSubtitle(label) {
    var sub = $("dashSub");
    if (sub) sub.textContent = (label || ALL_SCENES_LABEL) + " · Last 28 days";
  }

  function emptyMessage(label) {
    if (!label || label === ALL_SCENES_LABEL) {
      return "No annotations in the last 28 days.";
    }
    return "No annotations for " + label + " in the last 28 days.";
  }

  function weekAnnotations(speed) {
    var annotations = {};
    var weeks = speed.weeks || [];
    var i;
    for (i = 0; i < weeks.length; i += 1) {
      var week = weeks[i];
      var startIndex = i * 7;
      var endIndex = startIndex + 6;
      var averageHours = hoursFromSeconds(week.average_daily_duration_seconds);
      annotations["weekAvg" + i] = {
        type: "line",
        xMin: startIndex - 0.45,
        xMax: endIndex + 0.45,
        yMin: averageHours,
        yMax: averageHours,
        borderColor: cssVar("--warn", WEEK_LINE),
        borderWidth: 2,
        borderDash: [6, 4],
        clip: false,
        label: {
          display: true,
          content: formatWeekLabel(week.average_daily_duration_seconds),
          position: "center",
          xAdjust: 0,
          yAdjust: isNarrow() ? -12 : -10,
          backgroundColor: cssVar("--card", "#1e1e2e"),
          color: cssVar("--warn", WEEK_LINE),
          borderWidth: 0,
          padding: 4,
          font: { size: 11, weight: "600" },
        },
      };
    }
    return annotations;
  }

  function buildChartConfig(speed) {
    var days = speed.days;
    var pri = cssVar("--pri", BAR_FILL);
    var tick = cssVar("--t2", "#989bb0");
    var grid = "rgba(255,255,255,0.06)";
    var colors = days.map(function (day) {
      return day.is_partial ? BAR_FILL_TODAY : pri;
    });
    var preferReduced = reduceMotion();
    return {
      type: "bar",
      data: {
        labels: days.map(function (day) { return day.date; }),
        datasets: [{
          label: "Daily added hours",
          data: days.map(function (day) { return hoursFromSeconds(day.duration_seconds); }),
          backgroundColor: colors,
          hoverBackgroundColor: colors,
          borderWidth: 0,
          maxBarThickness: 18,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: preferReduced ? false : { duration: 400 },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: cssVar("--card2", "#252536"),
            titleColor: cssVar("--text", "#e2e4ec"),
            bodyColor: cssVar("--text", "#e2e4ec"),
            borderColor: cssVar("--bdr", "#2e2e40"),
            borderWidth: 1,
            displayColors: false,
            callbacks: {
              title: function (items) {
                if (!items.length) return "";
                return tooltipLines(currentSpeed(), items[0].dataIndex)[0] || "";
              },
              label: function (item) {
                var lines = tooltipLines(currentSpeed(), item.dataIndex);
                return lines[1] || "";
              },
              afterBody: function (items) {
                if (!items.length) return [];
                return tooltipLines(currentSpeed(), items[0].dataIndex).slice(2);
              },
            },
          },
          annotation: {
            annotations: weekAnnotations(speed),
          },
        },
        scales: {
          x: {
            title: {
              display: true,
              text: "Date",
              color: tick,
            },
            ticks: {
              color: tick,
              maxRotation: 45,
              minRotation: 0,
              autoSkip: true,
              maxTicksLimit: isNarrow() ? 4 : 7,
              callback: function (value) {
                var label = this.getLabelForValue(value);
                return formatTickDate(label);
              },
            },
            grid: { color: grid },
            border: { color: cssVar("--bdr", "#2e2e40") },
          },
          y: {
            beginAtZero: true,
            title: {
              display: true,
              text: "New annotated audio (hours)",
              color: tick,
            },
            ticks: { color: tick },
            grid: { color: grid },
            border: { color: cssVar("--bdr", "#2e2e40") },
          },
        },
        onResize: function (chart) {
          if (chart && chart.options && chart.options.scales && chart.options.scales.x) {
            chart.options.scales.x.ticks.maxTicksLimit = isNarrow() ? 4 : 7;
          }
          if (lastSpeed && chart.options.plugins && chart.options.plugins.annotation) {
            chart.options.plugins.annotation.annotations = weekAnnotations(lastSpeed);
          }
        },
      },
    };
  }

  function updateAria(speed, label) {
    var canvas = $("speedChart");
    var table = $("speedTable");
    if (!speed || !speed.days) return;
    var peak = speed.days.reduce(function (best, day) {
      return day.duration_seconds > best.duration_seconds ? day : best;
    }, speed.days[0]);
    var sceneText = label || ALL_SCENES_LABEL;
    if (canvas) {
      canvas.setAttribute(
        "aria-label",
        "Bar chart of newly annotated audio hours for " + sceneText +
        " from " +
        speed.from + " through " + speed.through +
        " in " + speed.timezone +
        ". Peak " + hoursFromSeconds(peak.duration_seconds).toFixed(1) +
        " hours on " + formatLongDate(peak.date) +
        ". Yellow dashed lines show each 7-day average."
      );
    }
    if (!table) return;
    var rows = speed.days.map(function (day, index) {
      var week = weekForIndex(speed.weeks, index);
      var avg = week
        ? hoursFromSeconds(week.average_daily_duration_seconds).toFixed(2)
        : "0.00";
      return "<tr><td>" + esc(day.date) + "</td><td>" +
        hoursFromSeconds(day.duration_seconds).toFixed(2) +
        "</td><td>" + avg + "</td></tr>";
    }).join("");
    table.innerHTML =
      "<table><caption>Daily added annotated audio hours and 7-day averages for " +
      esc(sceneText) + "</caption>" +
      "<thead><tr><th>Date</th><th>Hours</th><th>7-day average hours per day</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table>";
  }

  function renderSpeedChart(speed, label) {
    var frame = $("chartFrame");
    var canvas = $("speedChart");
    if (!frame || !canvas) return;
    if (!chartAvailable()) {
      destroyChart();
      frame.hidden = true;
      showLegend(false);
      setSpeedStatus("Could not load annotation statistics.");
      return;
    }
    var config = buildChartConfig(speed);
    frame.hidden = false;
    showLegend(true);
    setSpeedStatus("");
    if (speedChart) {
      speedChart.data = config.data;
      speedChart.options.plugins.annotation.annotations =
        config.options.plugins.annotation.annotations;
      speedChart.options.plugins.tooltip.callbacks =
        config.options.plugins.tooltip.callbacks;
      speedChart.options.scales.x.ticks.maxTicksLimit =
        config.options.scales.x.ticks.maxTicksLimit;
      speedChart.options.animation = config.options.animation;
      speedChart.update(reduceMotion() ? "none" : undefined);
      if (speedChart.tooltip && typeof speedChart.tooltip.setActiveElements === "function") {
        speedChart.tooltip.setActiveElements([], {x: 0, y: 0});
      }
    } else {
      speedChart = new Chart(canvas.getContext("2d"), config);
    }
    updateAria(speed, label);
  }

  function allDaysZero(speed) {
    if (!speed || !Array.isArray(speed.days) || speed.days.length !== 28) return false;
    return speed.days.every(function (day) {
      return Number(day.duration_seconds || 0) === 0;
    });
  }

  function validSpeed(speed) {
    return Boolean(
      speed &&
      Array.isArray(speed.days) &&
      speed.days.length === 28 &&
      Array.isArray(speed.weeks) &&
      speed.weeks.length === 4
    );
  }

  function renderSpeed(speed, label) {
    if (!validSpeed(speed)) {
      if (speedChart || lastSpeed) return;
      destroyChart();
      var frame = $("chartFrame");
      if (frame) frame.hidden = true;
      showLegend(false);
      setSpeedStatus("Could not load annotation statistics.");
      return;
    }
    updateSubtitle(label);
    lastSpeed = speed;
    if (allDaysZero(speed)) {
      destroyChart();
      var emptyFrame = $("chartFrame");
      if (emptyFrame) emptyFrame.hidden = true;
      showLegend(false);
      $("speedTable") && ($("speedTable").innerHTML = "");
      setSpeedStatus(emptyMessage(label));
      return;
    }
    renderSpeedChart(speed, label);
  }

  function renderSpeedFailure() {
    if (speedChart || lastSpeed) return;
    destroyChart();
    var frame = $("chartFrame");
    if (frame) frame.hidden = true;
    showLegend(false);
    setSpeedStatus("Could not load annotation statistics.");
  }

  function renderLeaderboard(rows) {
    var tbody = $("lbBody");
    var empty = $("lbEmpty");
    var table = $("lbTable");
    if (!tbody || !empty || !table) return;
    if (!rows || !rows.length) {
      empty.style.display = "";
      empty.textContent = "No data yet";
      table.style.display = "none";
      tbody.innerHTML = "";
      return;
    }
    empty.style.display = "none";
    table.style.display = "";
    var me = ($("username") && $("username").value.trim()) || "";
    tbody.innerHTML = rows.map(function (row, index) {
      var cls = row.user === me ? " lb-me" : "";
      return '<tr class="' + cls + '"><td class="lb-rank">' + (index + 1) +
        '</td><td class="lb-user" title="' + esc(row.user) + '">' + esc(row.user) +
        '</td><td class="lb-num">' + esc(row.annotated) +
        '</td><td class="lb-hr">' + formatHoursMinutes(row.duration_seconds) +
        "</td></tr>";
    }).join("");
  }

  function renderLeaderboardFailure() {
    var empty = $("lbEmpty");
    if (empty && empty.style.display !== "none") {
      empty.textContent = "Failed to load";
    }
  }

  function renderTotal(seconds) {
    var valueEl = $("lbTotalValue");
    var wrap = $("lbTotal");
    if (!valueEl) return;
    var n = Number(seconds);
    if (!Number.isFinite(n)) n = 0;
    valueEl.textContent = formatHoursMinutes(n);
    totalLoaded = true;
    if (wrap) {
      wrap.setAttribute(
        "aria-label",
        "Total annotated time, excluding audio marked abnormal: " +
        formatHoursMinutesSpoken(n)
      );
    }
  }

  function renderTotalFailure() {
    if (totalLoaded) return;
    var valueEl = $("lbTotalValue");
    var wrap = $("lbTotal");
    if (valueEl) valueEl.textContent = "Unavailable";
    if (wrap) {
      wrap.setAttribute(
        "aria-label",
        "Total annotated time, excluding audio marked abnormal: Unavailable"
      );
    }
  }

  function fillSceneOptions(options) {
    var select = $("sceneSelect");
    if (!select) return;
    var html = '<option value="">' + ALL_SCENES_LABEL + "</option>";
    (options || []).forEach(function (item) {
      if (!item || !item.code) return;
      html += '<option value="' + esc(item.code) + '">' +
        esc(item.label || item.code) + "</option>";
    });
    select.innerHTML = html;
    if (!knownScene(selectedScene, options)) selectedScene = "";
    select.value = selectedScene;
  }

  function bindSceneFilter() {
    var select = $("sceneSelect");
    if (!select || sceneFilterBound) return;
    sceneFilterBound = true;
    select.addEventListener("change", function () {
      selectedScene = select.value || "";
      if (lastPayload) applyScene();
    });
  }

  function seriesForScene(root, code) {
    if (!root) return null;
    var part = code
      ? (root.by_scene && root.by_scene[code])
      : { days: root.days, weeks: root.weeks };
    if (!part) return null;
    return {
      timezone: root.timezone,
      window_days: root.window_days,
      from: root.from,
      through: root.through,
      generated_at: root.generated_at,
      days: part.days,
      weeks: part.weeks,
    };
  }

  function applyScene() {
    var payload = lastPayload;
    if (!payload) return;
    var options = payload.scene_options || [];
    var select = $("sceneSelect");
    if (!knownScene(selectedScene, options)) {
      selectedScene = "";
      if (select) select.value = "";
    }
    var label = sceneLabel(selectedScene, options);
    var speed = seriesForScene(payload.annotation_speed, selectedScene);
    if (selectedScene && !validSpeed(speed)) {
      selectedScene = "";
      if (select) select.value = "";
      label = ALL_SCENES_LABEL;
      speed = seriesForScene(payload.annotation_speed, "");
    }
    renderSpeed(speed, label);
  }

  function applyPayload(payload) {
    renderLeaderboard(payload.leaderboard || []);
    if (payload.total_annotated_duration_seconds != null) {
      renderTotal(payload.total_annotated_duration_seconds);
    }
    var speedOk = validSpeed(payload.annotation_speed);
    if (!speedOk && lastPayload) return;
    lastPayload = payload;
    fillSceneOptions(payload.scene_options || []);
    applyScene();
  }

  function loadDashboard() {
    if (inflight) return inflight;
    inflight = fetch("/api/leaderboard", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("leaderboard failed");
        return response.json();
      })
      .then(function (payload) {
        applyPayload(payload || {});
      })
      .catch(function () {
        renderLeaderboardFailure();
        renderTotalFailure();
        renderSpeedFailure();
      })
      .then(function () {
        inflight = null;
      });
    return inflight;
  }

  function startRefresh() {
    window.setInterval(function () {
      if (document.visibilityState === "visible") loadDashboard();
    }, REFRESH_MS);
  }

  window.LoginDashboard = {
    load: loadDashboard,
    tooltipLines: tooltipLines,
    currentSpeed: currentSpeed,
    formatHoursMinutes: formatHoursMinutes,
    selectedScene: function () {
      return selectedScene;
    },
    getChart: function () {
      if (speedChart) return speedChart;
      var canvas = $("speedChart");
      if (canvas && window.Chart && typeof Chart.getChart === "function") {
        return Chart.getChart(canvas);
      }
      return null;
    },
  };

  bindSceneFilter();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bindSceneFilter();
      loadDashboard();
      startRefresh();
    });
  } else {
    loadDashboard();
    startRefresh();
  }
})();
