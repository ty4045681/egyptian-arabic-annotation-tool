/* Admin cross-check queue, sampling settings, comparison, and adjudication. */
(function (root) {
  "use strict";

  const STATES = [
    "awaiting_review", "in_progress", "passed", "adjudicated", "cancelled", "invalidated",
  ];
  const REASON_CODES = [
    "word_difference_exceeded",
    "submission_status_conflict",
    "empty_original_text",
    "empty_secondary_text",
    "bad_quality_conflict",
    "comparison_unavailable",
  ];
  const POLL_MS = 30000;
  const SEARCH_MS = 300;

  const CC = () => root.CrossCheck;
  let host = null;
  const ui = {
    filters: emptyFilters(),
    items: [],
    cursor: null,
    listEpoch: 0,
    listAbort: null,
    summary: null,
    summaryError: "",
    settings: null,
    settingsError: "",
    settingsLoaded: false,
    selectedRoundId: "",
    detail: null,
    detailEpoch: 0,
    detailAbort: null,
    listScroll: 0,
    showingDetail: false,
    frozenDecision: null,
    frozenCancel: null,
    frozenSettings: null,
    editor: null,
    editorBase: "",
    retainedDraft: null,
    sceneOverride: false,
    sceneReview: null,
    sceneCatalogOk: true,
    audioTimer: null,
    diffIndex: -1,
    pollTimer: 0,
    searchTimer: 0,
    pendingWrite: false,
    staleSettings: null,
    lastTrigger: null,
  };

  function emptyFilters() {
    return {
      state: "awaiting_review",
      source_scene: "",
      batch_code: "",
      original_annotator_id: "",
      secondary_annotator_id: "",
      reason_code: "",
      q: "",
      from: "",
      to: "",
    };
  }

  function $(id) { return host.$(id); }
  function element(tag, options, children) { return host.element(tag, options || {}, children || []); }
  function text(node, value, fallback) { return host.text(node, value, fallback); }
  function clear(node) { return host.clear(node); }

  function annotatorName(id) {
    if (!id) return "—";
    const people = host.getAnnotators() || [];
    const found = people.find((item) => String(item.id) === String(id));
    if (found && found.username) return found.username;
    return CC().shortId(id);
  }

  function hasPendingWrite() {
    return Boolean(hasLocalDraft() || ui.pendingWrite || ui.frozenDecision || ui.frozenCancel || ui.frozenSettings);
  }

  function hasLocalDraft() {
    return Boolean((ui.editor && ui.editor.dirty) || ui.retainedDraft ||
      (ui.showingDetail && $("ccDecisionReason")?.value.trim()));
  }

  function writeUnresolved() {
    if (!(ui.pendingWrite || ui.frozenDecision || ui.frozenCancel || ui.frozenSettings)) return false;
    host.toast("A request is still pending. Retry the same request to confirm its result before leaving or refreshing.", "error");
    return true;
  }

  function canLeaveView() {
    return !writeUnresolved() && (!hasLocalDraft() || confirmDiscard());
  }

  function contributeUrl(url) {
    if (!host || host.currentView() !== "cross-checks") return;
    url.searchParams.set("state", ui.filters.state || "awaiting_review");
    const keys = ["source_scene", "batch_code", "original_annotator_id", "secondary_annotator_id", "reason_code", "q", "from", "to"];
    keys.forEach((key) => {
      if (ui.filters[key]) url.searchParams.set(key, ui.filters[key]);
    });
    if (ui.selectedRoundId) url.searchParams.set("round", ui.selectedRoundId);
  }

  function restoreFromUrl(params) {
    const filters = emptyFilters();
    const state = params.get("state");
    if (STATES.includes(state)) filters.state = state;
    filters.source_scene = params.get("source_scene") || "";
    filters.batch_code = params.get("batch_code") || "";
    filters.original_annotator_id = params.get("original_annotator_id") || "";
    filters.secondary_annotator_id = params.get("secondary_annotator_id") || "";
    filters.reason_code = params.get("reason_code") || "";
    filters.q = params.get("q") || "";
    filters.from = params.get("from") || "";
    filters.to = params.get("to") || "";
    ui.filters = filters;
    ui.selectedRoundId = params.get("round") || "";
    writeFilterFields();
  }

  function writeFilterFields() {
    const map = {
      ccState: "state",
      ccSourceScene: "source_scene",
      ccBatch: "batch_code",
      ccOriginalAnnotator: "original_annotator_id",
      ccSecondaryAnnotator: "secondary_annotator_id",
      ccReason: "reason_code",
      ccSearch: "q",
      ccFrom: "from",
      ccTo: "to",
    };
    Object.entries(map).forEach(([id, key]) => {
      const node = $(id);
      if (node) node.value = ui.filters[key] || "";
    });
  }

  function readFilterFields() {
    ui.filters.state = $("ccState")?.value || "awaiting_review";
    ui.filters.source_scene = $("ccSourceScene")?.value || "";
    ui.filters.batch_code = $("ccBatch")?.value || "";
    ui.filters.original_annotator_id = $("ccOriginalAnnotator")?.value || "";
    ui.filters.secondary_annotator_id = $("ccSecondaryAnnotator")?.value || "";
    ui.filters.reason_code = $("ccReason")?.value || "";
    ui.filters.q = $("ccSearch")?.value.trim() || "";
    ui.filters.from = $("ccFrom")?.value || "";
    ui.filters.to = $("ccTo")?.value || "";
  }

  function fillFilterOptions() {
    const scenes = Object.entries(host.getSceneLabels() || {}).map(([value, text]) => ({ value, text }));
    fillSelect($("ccSourceScene"), scenes, "All source scenes");
    const batches = (host.getBatchOptions && host.getBatchOptions()) || [];
    fillSelect($("ccBatch"), batches, "All batches");
    fillAnnotatorSelect($("ccOriginalAnnotator"), "All original annotators");
    fillAnnotatorSelect($("ccSecondaryAnnotator"), "All cross-check annotators");
    fillSelect($("ccReason"), REASON_CODES.map((code) => ({ value: code, text: CC().reasonLabel(code) })), "All reasons");
    fillSelect($("ccState"), STATES.map((state) => ({ value: state, text: CC().stateLabel(state) })), null, ui.filters.state);
    writeFilterFields();
  }

  function fillSelect(select, items, allText, selected) {
    if (!select) return;
    const current = selected !== undefined ? selected : select.value;
    select.replaceChildren();
    if (allText) select.appendChild(element("option", { value: "", text: allText }));
    items.forEach((item) => select.appendChild(element("option", { value: item.value, text: item.text })));
    if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
  }

  function fillAnnotatorSelect(select, allText) {
    if (!select) return;
    const people = host.getAnnotators() || [];
    fillSelect(select, people.map((item) => ({
      value: item.id,
      text: item.status === "active" ? item.username : `${item.username} (deactivated)`,
    })), allText);
  }

  function listQuery() {
    const params = new URLSearchParams();
    params.set("state", ui.filters.state || "awaiting_review");
    params.set("limit", "50");
    params.set("timezone", host.getTimezone() || "UTC");
    ["source_scene", "batch_code", "original_annotator_id", "secondary_annotator_id", "reason_code", "q"].forEach((key) => {
      if (ui.filters[key]) params.set(key, ui.filters[key]);
    });
    if (ui.filters.from) params.set("from", ui.filters.from);
    if (ui.filters.to) params.set("to", host.shiftDate(ui.filters.to, 1));
    return params;
  }

  function setListState(message, error) {
    const node = $("ccListState");
    if (!node) return;
    node.hidden = !message;
    node.classList.toggle("is-error", Boolean(error));
    text(node, message || "");
  }

  async function loadView() {
    if (writeUnresolved()) return;
    fillFilterOptions();
    startPolling();
    const jobs = [loadSummary(), loadSettings(), loadList(true)];
    await Promise.allSettled(jobs);
    if (ui.selectedRoundId) await openRound(ui.selectedRoundId, { fromUrl: true });
    else showList();
  }

  async function loadSummary() {
    try {
      const data = await host.api.get("/api/admin/overview");
      ui.summary = data;
      ui.summaryError = "";
      renderSummary(data);
    } catch (error) {
      if (error.name === "AbortError") return;
      ui.summaryError = error.message || "Could not load cross-check summary.";
      renderSummary(ui.summary);
      const retry = $("ccSummaryRetry");
      if (retry) retry.hidden = false;
    }
  }

  function renderSummary(data) {
    const cc = host.pick(data || {}, ["cross_check"], {}) || {};
    const totals = host.pick(data || {}, ["totals"], {}) || {};
    text($("ccSummaryScope"), "All-time, all scenes");
    text($("ccKpiInProgress"), ui.summaryError && !data ? "—" : host.formatInteger(cc.in_progress_count || 0));
    text($("ccKpiPending"), ui.summaryError && !data ? "—" : host.formatInteger(cc.pending_review_count || 0));
    text($("ccKpiPassed"), ui.summaryError && !data ? "—" : host.formatInteger(cc.passed_count || 0));
    text($("ccKpiAdjudicated"), ui.summaryError && !data ? "—" : host.formatInteger(cc.adjudicated_count || 0));
    text($("ccKpiBlocked"), ui.summaryError && !data ? "—" : host.formatDuration(cc.blocked_audio_seconds || 0, false));
    text($("ccKpiOldest"), ui.summaryError && !data ? "—" : host.formatDateTime(cc.oldest_pending_created_at || null));
    text($("ccKpiSubmitted"), ui.summaryError && !data ? "—" : host.formatInteger(totals.cross_check_submitted_count || 0));
    text($("ccKpiSubmittedAudio"), ui.summaryError && !data ? "—" : host.formatDuration(totals.cross_check_submitted_audio_seconds || 0, false));
    const note = $("ccSummaryError");
    if (note) {
      note.hidden = !ui.summaryError;
      text(note, ui.summaryError);
    }
    renderOverviewCards(data);
    renderQualityCard(data);
  }

  function renderOverviewCards(data) {
    const cc = host.pick(data || {}, ["cross_check"], {}) || {};
    const totals = host.pick(data || {}, ["totals"], {}) || {};
    text($("overviewCcPending"), host.formatInteger(cc.pending_review_count || 0));
    text($("overviewCcInProgress"), host.formatInteger(cc.in_progress_count || 0));
    text($("overviewCcBlocked"), host.formatDuration(cc.blocked_audio_seconds || 0, false));
    text($("overviewCcSubmitted"), host.formatInteger(totals.cross_check_submitted_count || 0));
    text($("overviewCcSubmittedAudio"), host.formatDuration(totals.cross_check_submitted_audio_seconds || 0, false));
    text($("overviewCcOldest"), host.formatDateTime(cc.oldest_pending_created_at || null));
  }

  function renderQualityCard(data) {
    const cc = host.pick(data || {}, ["cross_check"], data && data.cross_check ? data.cross_check : {});
    text($("qualityCrossCheckPending"), host.formatInteger((cc && cc.pending_review_count) || 0));
  }

  async function loadSettings() {
    const draft = ui.staleSettings ? {
      enabled: $("ccSamplingEnabled").checked,
      percent: $("ccSamplingPercent").value,
      reason: $("ccSamplingReason").value,
    } : null;
    const save = $("ccSettingsSave");
    if (save) save.disabled = true;
    try {
      const data = await host.api.get("/api/admin/cross-check-settings");
      ui.settings = data;
      ui.settingsLoaded = true;
      ui.settingsError = "";
      paintSettingsForm(data);
      if (draft) {
        $("ccSamplingEnabled").checked = draft.enabled;
        $("ccSamplingPercent").value = draft.percent;
        $("ccSamplingReason").value = draft.reason;
        const stale = $("ccSettingsStale");
        stale.hidden = false;
        text(stale, `Server: ${data.enabled ? "enabled" : "disabled"}, ${CC().percentFromBps(data.sampling_rate_bps)}% (revision ${data.revision}). Your draft: ${draft.enabled ? "enabled" : "disabled"}, ${draft.percent}%. Your inputs are preserved. Review both values and save again to confirm.`);
      }
      freezeSettingsControls();
    } catch (error) {
      ui.settingsLoaded = false;
      ui.settingsError = error.message || "Could not load sampling settings.";
      const status = $("ccSettingsStatus");
      if (status) {
        status.hidden = false;
        text(status, ui.settingsError);
      }
      if (save) save.disabled = true;
    }
  }

  function paintSettingsForm(data) {
    if (!data) return;
    $("ccSamplingEnabled").checked = Boolean(data.enabled);
    $("ccSamplingPercent").value = CC().percentFromBps(data.sampling_rate_bps);
    $("ccSamplingReason").value = "";
    text($("ccSamplingThreshold"), "Review threshold: word difference > 10%");
    text($("ccSamplingMeta"), `Comparison ${data.comparison_version || "worddiff_v1"} · revision ${data.revision}`);
    const status = $("ccSettingsStatus");
    if (status) {
      status.hidden = true;
      text(status, "");
    }
    const stale = $("ccSettingsStale");
    if (stale) stale.hidden = true;
  }

  function settingsUnchanged() {
    if (!ui.settings) return true;
    try {
      const enabled = $("ccSamplingEnabled").checked;
      const bps = CC().samplingBpsFromPercent($("ccSamplingPercent").value);
      return enabled === Boolean(ui.settings.enabled) && bps === Number(ui.settings.sampling_rate_bps);
    } catch (_) {
      return false;
    }
  }

  async function saveSettings() {
    if (ui.pendingWrite || !ui.settingsLoaded || !ui.settings) return;
    formError($("ccSettingsError"), "");
    const reasonErr = CC().reasonError($("ccSamplingReason").value);
    if (reasonErr) {
      formError($("ccSettingsError"), reasonErr);
      $("ccSamplingReason").focus();
      return;
    }
    let bps;
    try {
      bps = CC().samplingBpsFromPercent($("ccSamplingPercent").value);
    } catch (error) {
      formError($("ccSettingsError"), error.message);
      $("ccSamplingPercent").focus();
      return;
    }
    if (settingsUnchanged() && !ui.staleSettings && !ui.frozenSettings) {
      host.toast("Sampling settings are unchanged.");
      return;
    }
    if (!ui.frozenSettings) {
      ui.frozenSettings = {
        operation_id: host.uuid(),
        expected_revision: Number(ui.settings.revision),
        enabled: $("ccSamplingEnabled").checked,
        sampling_rate_bps: bps,
        reason: CC().trimmedReason($("ccSamplingReason").value),
      };
    }
    ui.pendingWrite = true;
    freezeSettingsControls();
    try {
      const result = await host.api.put("/api/admin/cross-check-settings", ui.frozenSettings);
      ui.settings = result;
      ui.frozenSettings = null;
      ui.staleSettings = null;
      paintSettingsForm(result);
      host.toast("Sampling settings saved. In-progress and awaiting-review rounds are unchanged.");
      closeDialog($("ccSettingsDialog"));
    } catch (error) {
      if (error.status === 409 && error.body && error.body.code === "stale_revision") {
        ui.staleSettings = true;
        ui.frozenSettings = null;
        await loadSettings();
        formError($("ccSettingsError"), "Sampling settings changed elsewhere. Confirm and save again.");
      } else if (error.status === 400) {
        ui.frozenSettings = null;
        formError($("ccSettingsError"), error.message);
      } else {
        formError($("ccSettingsError"), error.message + " You can retry with the same request.");
      }
    } finally {
      ui.pendingWrite = false;
      freezeSettingsControls();
    }
  }

  function freezeSettingsControls() {
    for (const id of ["ccSamplingEnabled", "ccSamplingPercent", "ccSamplingReason"]) {
      $(id).disabled = Boolean(ui.pendingWrite || ui.frozenSettings);
    }
    host.setButtonBusy($("ccSettingsSave"), ui.pendingWrite, "Saving…");
    $("ccSettingsSave").disabled = ui.pendingWrite || !ui.settingsLoaded;
  }

  async function loadList(reset) {
    if (reset) {
      ui.items = [];
      ui.cursor = null;
      ui.listEpoch += 1;
      if (ui.listAbort) ui.listAbort.abort();
      ui.listAbort = new AbortController();
      clear($("ccListBody"));
      setListState("Loading cross-check rounds…");
      $("ccLoadMore").hidden = true;
    }
    const epoch = ui.listEpoch;
    const params = listQuery();
    if (!reset && ui.cursor) params.set("cursor", ui.cursor);
    try {
      const data = await host.api.get(`/api/admin/cross-checks?${params.toString()}`, { signal: ui.listAbort && ui.listAbort.signal });
      if (epoch !== ui.listEpoch) return;
      const incoming = host.listValue(data, ["items"]);
      const seen = new Set(ui.items.map((item) => item.round_id));
      const unique = incoming.filter((item) => {
        if (seen.has(item.round_id)) return false;
        seen.add(item.round_id);
        return true;
      });
      ui.items = reset ? unique : ui.items.concat(unique);
      ui.cursor = data.next_cursor || null;
      renderList();
      $("ccLoadMore").hidden = !ui.cursor;
      text($("ccLoadedCount"), `${host.formatInteger(ui.items.length)} loaded`);
      if (!ui.items.length) setListState("No cross-check rounds match these filters.");
      else setListState("");
    } catch (error) {
      if (error.name === "AbortError" || epoch !== ui.listEpoch) return;
      if (error.status === 400 && ui.cursor && !reset) {
        host.toast("That page is no longer valid. Reloading the first page.", "error");
        return loadList(true);
      }
      if (reset) setListState(error.message || "Could not load the queue.", true);
      else {
        host.toast(error.message || "Could not load more rounds.", "error");
        setListState("");
      }
    }
  }

  function renderList() {
    const body = $("ccListBody");
    clear(body);
    for (const item of ui.items) {
      const row = element("tr", { className: item.round_id === ui.selectedRoundId ? "is-selected" : "" });
      row.appendChild(element("td", { className: "mono-cell", text: CC().shortId(item.task_id), title: item.task_id }));
      row.appendChild(element("td", { text: annotatorName(item.original_annotator_id) }));
      row.appendChild(element("td", { text: annotatorName(item.secondary_annotator_id) }));
      row.appendChild(element("td", { className: "mono-cell", text: item.duration_seconds == null ? "—" : host.formatDuration(item.duration_seconds) }));
      row.appendChild(element("td", {}, stateBadge(item.state)));
      row.appendChild(element("td", { className: "mono-cell", text: CC().formatWordDifferenceRate(item.word_difference_rate) }));
      row.appendChild(element("td", { text: CC().reasonLabels(item.reason_codes).join("; ") || "—" }));
      row.appendChild(element("td", { text: host.formatDateTime(item.created_at) }));
      row.appendChild(element("td", { text: host.formatDateTime(item.submitted_at) }));
      const actions = element("td", { className: "actions-cell" });
      const button = element("button", {
        className: "table-action",
        text: "View",
        type: "button",
        dataset: { ccRound: item.round_id },
      });
      actions.appendChild(button);
      if (item.training_export_blocked) {
        actions.appendChild(element("span", { className: "badge badge-warning", text: "Training export on hold" }));
      }
      row.appendChild(actions);
      body.appendChild(row);
    }
  }

  function stateBadge(state) {
    const tone = state === "awaiting_review" || state === "in_progress" ? "badge-warning"
      : state === "cancelled" || state === "invalidated" ? "badge-neutral"
        : state === "passed" || state === "adjudicated" ? "" : "badge-neutral";
    return element("span", { className: `badge ${tone}`.trim(), text: CC().stateLabel(state) });
  }

  function showList() {
    ui.showingDetail = false;
    $("ccListPanel").hidden = false;
    $("ccReviewPanel").hidden = true;
    stopAudio();
    $("ccListWrap").scrollTop = ui.listScroll || 0;
  }

  async function openRound(roundId, options) {
    options = options || {};
    if (!roundId) return;
    if (!options.afterWrite && writeUnresolved()) return;
    if (!options.preserveLocal && hasLocalDraft() && !confirmDiscard()) return;
    if (!options.preserveLocal) ui.retainedDraft = null;
    resetEditorState();
    if (ui.showingDetail) ui.listScroll = $("ccListWrap").scrollTop;
    ui.selectedRoundId = roundId;
    ui.showingDetail = true;
    stopAudio();
    ui.detailEpoch += 1;
    if (ui.detailAbort) ui.detailAbort.abort();
    ui.detailAbort = new AbortController();
    const epoch = ui.detailEpoch;
    $("ccListPanel").hidden = true;
    $("ccReviewPanel").hidden = false;
    clear($("ccReviewBody"));
    $("ccReviewBody").appendChild(element("div", { className: "table-state" }, [
      element("span", { className: "spinner spinner-small", attrs: { "aria-hidden": "true" } }),
      " Loading round…",
    ]));
    host.syncUrl();
    try {
      const detail = await host.api.get(`/api/admin/cross-checks/${encodeURIComponent(roundId)}`, {
        signal: ui.detailAbort.signal,
      });
      if (epoch !== ui.detailEpoch) return;
      ui.detail = detail;
      renderDetail(detail);
      const title = $("ccReviewTitle");
      if (title && title.focus) title.focus();
    } catch (error) {
      if (error.name === "AbortError" || epoch !== ui.detailEpoch) return;
      clear($("ccReviewBody"));
      $("ccReviewBody").appendChild(element("div", { className: "table-state is-error" }, [
        element("p", { text: error.message || "Could not load this round." }),
        element("button", { className: "button button-secondary", type: "button", text: "Try again", dataset: { ccRetryRound: roundId } }),
      ]));
      renderRetainedDraft($("ccReviewBody"));
      if (!options.fromUrl) host.toast(error.message, "error");
    }
  }

  function resetEditorState() {
    ui.editor = null;
    ui.editorBase = "";
    ui.sceneOverride = false;
    ui.sceneReview = null;
    ui.frozenDecision = null;
    ui.diffIndex = -1;
  }

  function confirmDiscard() {
    return window.confirm("Discard unsaved adjudication edits?");
  }

  function renderDetail(detail) {
    clear($("ccReviewBody"));
    const body = $("ccReviewBody");
    try {
      body.appendChild(renderDetailHeader(detail));
      body.appendChild(renderComparisonStats(detail));
      body.appendChild(renderPlayer(detail));
      body.appendChild(renderManuscripts(detail));
      body.appendChild(renderBadQuality(detail));
      body.appendChild(renderReviews(detail));
      body.appendChild(renderDecision(detail));
      renderRetainedDraft(body);
      body.appendChild(renderAudit(detail));
      bindDetailEvents(detail);
      highlightAll(detail);
    } catch (error) {
      body.appendChild(element("p", {
        className: "cc-warning",
        attrs: { id: "ccRenderError", role: "alert" },
        text: "Could not render this round: " + (error && error.message ? error.message : String(error)),
      }));
    }
  }

  function renderDetailHeader(detail) {
    const wrap = element("div", { className: "cc-detail-head" });
    wrap.appendChild(element("h3", { attrs: { id: "ccReviewTitle", tabindex: "-1" }, text: detail.filename || CC().shortId(detail.task_id) }));
    const meta = element("p", { className: "cc-detail-meta" });
    meta.appendChild(element("span", { text: host.formatDuration(detail.duration_seconds, false) }));
    meta.appendChild(stateBadge(detail.state));
    if (detail.training_export_blocked) meta.appendChild(element("span", { className: "badge badge-warning", text: "Training export on hold" }));
    wrap.appendChild(meta);
    const reasons = CC().reasonLabels(detail.reason_codes);
    if (reasons.length) wrap.appendChild(element("p", { text: "Triggered because: " + reasons.join("; ") }));
    const covered = detail.final_version_id || detail.original_version_id;
    if (detail.current_published_version_id && covered && String(detail.current_published_version_id) !== String(covered)) {
      wrap.appendChild(element("p", { className: "cc-warning", attrs: { role: "status" }, text: "The published version has changed since this review." }));
    }
    if (detail.state === "in_progress") {
      wrap.appendChild(element("p", { className: "cc-warning", text: "This cross-check is still in progress. The secondary transcript is not a final result and cannot be adjudicated yet." }));
    }
    if (detail.state === "passed") {
      wrap.appendChild(element("p", { text: "This comparison passed. The original published transcript remains current." }));
    }
    return wrap;
  }

  function renderComparisonStats(detail) {
    const panel = element("section", { className: "cc-stats", attrs: { "aria-label": "Comparison summary" } });
    if (detail.comparison_unavailable) {
      panel.appendChild(element("p", { className: "cc-warning", text: "Comparison unavailable" + (detail.comparison_unavailable_reason ? `: ${detail.comparison_unavailable_reason}` : "") + ". You can still listen and adjudicate." }));
      return panel;
    }
    const list = element("dl", { className: "cc-stat-grid" });
    [
      ["Original words", detail.original_word_count == null ? "—" : host.formatInteger(detail.original_word_count)],
      ["Cross-check words", detail.secondary_word_count == null ? "—" : host.formatInteger(detail.secondary_word_count)],
      ["Edit distance", detail.edit_distance == null ? "—" : host.formatInteger(detail.edit_distance)],
      ["Word difference", CC().formatWordDifferenceRate(detail.word_difference_rate)],
    ].forEach(([label, value]) => {
      list.appendChild(element("div", {}, [element("dt", { text: label }), element("dd", { className: "mono-cell", text: value })]));
    });
    panel.appendChild(list);
    return panel;
  }

  function renderPlayer(detail) {
    const wrap = element("section", { className: "cc-player", attrs: { "aria-label": "Audio and difference navigation" } });
    const audio = element("audio", {
      className: "detail-audio",
      attrs: { id: "ccAudio", controls: "", preload: "metadata", src: detail.audio_url || "" },
    });
    wrap.appendChild(audio);
    wrap.appendChild(element("p", { className: "cc-audio-status", attrs: { id: "ccAudioStatus", role: "status" }, text: "Audio ready." }));
    const nav = element("div", { className: "cc-diff-nav" });
    nav.appendChild(element("button", { className: "button button-secondary button-small", type: "button", text: "Previous difference", attrs: { id: "ccPrevDiff" } }));
    nav.appendChild(element("button", { className: "button button-secondary button-small", type: "button", text: "Next difference", attrs: { id: "ccNextDiff" } }));
    nav.appendChild(element("span", { className: "cc-diff-pos", attrs: { id: "ccDiffPos" }, text: "" }));
    wrap.appendChild(nav);
    return wrap;
  }

  function renderManuscripts(detail) {
    const grid = element("div", { className: "cc-manuscripts" });
    grid.appendChild(renderSide(detail, "original", "Original", detail.original_segments, detail.original_target_status, detail.original_skip_reasons, detail.original_annotator_id));
    grid.appendChild(renderSide(detail, "secondary", "Cross-check", detail.secondary_segments, detail.secondary_target_status, detail.secondary_skip_reasons, detail.secondary_annotator_id));
    return grid;
  }

  function renderSide(detail, side, title, segments, status, skipReasons, annotatorId) {
    const column = element("section", { className: "cc-side", attrs: { "data-cc-side": side } });
    column.appendChild(element("h4", { text: title }));
    column.appendChild(element("p", { className: "cc-side-meta", text: `${annotatorName(annotatorId)} · ${status || "—"}` }));
    if (status === "skipped" && skipReasons && skipReasons.length) {
      column.appendChild(element("p", { text: "Skip reasons: " + skipReasons.join(", ") }));
    }
    const list = element("div", { className: "cc-segments" });
    const helper = window.CrossCheck;
    const ranges = helper
      ? helper.rangesBySegment(helper.asOps ? helper.asOps(detail.diff_ops) : (Array.isArray(detail.diff_ops) ? detail.diff_ops : []), side)
      : new Map();
    const listSegments = Array.isArray(segments) ? segments : [];
    listSegments.forEach((segment) => {
      const block = element("article", {
        className: "cc-segment",
        attrs: { "data-segment-id": String(segment.id), "data-cc-side": side, tabindex: "-1" },
      });
      const meta = element("div", { className: "cc-segment-meta", attrs: { dir: "ltr" } });
      meta.appendChild(element("span", { text: "#" + segment.id }));
      meta.appendChild(element("span", { text: formatSpan(segment.start, segment.end) }));
      if (segment.exclude_from_training) meta.appendChild(element("span", { className: "badge badge-danger", text: "Bad quality" }));
      block.appendChild(meta);
      const transcript = element("p", { className: "cc-transcript", attrs: { dir: "auto" } });
      const result = helper
        ? helper.highlightTranscript(transcript, segment.text || "", ranges.get(String(segment.id)) || [])
        : { fallback: false };
      block.appendChild(transcript);
      if (result.fallback) {
        block.appendChild(element("p", { className: "cc-highlight-note", text: "Precise word highlight unavailable; this segment is marked as a whole." }));
      }
      if (detail.state === "in_progress" && side === "secondary") {
        block.appendChild(element("p", { className: "cc-highlight-note", text: "Not a final submitted transcript." }));
      }
      list.appendChild(block);
    });
    if (!listSegments.length) list.appendChild(element("p", { className: "cc-empty", text: "No segments." }));
    column.appendChild(list);
    return column;
  }

  function formatSpan(start, end) {
    const fmt = (value) => {
      const n = Number(value) || 0;
      const m = Math.floor(n / 60);
      const s = (n % 60).toFixed(2);
      return `${m}:${String(s).padStart(5, "0")}`;
    };
    return `${fmt(start)}–${fmt(end)}`;
  }

  function renderBadQuality(detail) {
    const map = detail.segment_map || {};
    const original = map.original_bad_quality || [];
    const secondary = map.secondary_bad_quality || [];
    const panel = element("section", { className: "cc-bq", attrs: { "aria-label": "Bad quality coverage" } });
    panel.appendChild(element("h4", { text: "Bad quality coverage" }));
    if (!original.length && !secondary.length) {
      panel.appendChild(element("p", { text: "No bad-quality intervals on either side." }));
      return panel;
    }
    panel.appendChild(element("p", { text: `Original: ${formatBq(original)}` }));
    panel.appendChild(element("p", { text: `Cross-check: ${formatBq(secondary)}` }));
    return panel;
  }

  function formatBq(items) {
    if (!items.length) return "none";
    return items.map((item) => `${item.start_ms}–${item.end_ms} ms`).join(", ");
  }

  function renderReviews(detail) {
    const wrap = element("section", { className: "cc-reviews" });
    wrap.appendChild(renderReviewCard("Original scene review", detail.original_review));
    wrap.appendChild(renderReviewCard("Cross-check scene review", detail.secondary_review));
    return wrap;
  }

  function renderReviewCard(title, review) {
    const card = element("article", { className: "cc-review-card" });
    card.appendChild(element("h4", { text: title }));
    if (root.AnnotationMetadata && review) {
      const hostNode = element("div");
      card.appendChild(hostNode);
      AnnotationMetadata.renderReviewControls(hostNode, { value: review, disabled: true, scenes: [] });
    } else {
      card.appendChild(element("p", { text: review ? JSON.stringify(review.status || review) : "No scene review." }));
    }
    return card;
  }

  function renderDecision(detail) {
    const wrap = element("section", { className: "cc-decision", attrs: { "aria-label": "Adjudication" } });
    if (detail.state === "awaiting_review") wrap.appendChild(renderDecisionForm(detail));
    else if (detail.state === "in_progress") wrap.appendChild(renderCancelForm(detail));
    else wrap.appendChild(renderDecisionRecord(detail));
    return wrap;
  }

  function renderDecisionForm(detail) {
    const form = element("form", { className: "cc-decision-form", attrs: { id: "ccDecisionForm" } });
    form.appendChild(element("h4", { text: "Decision" }));
    form.appendChild(element("p", { text: "Choose a result after listening. Nothing is selected by default." }));
    const choices = element("div", { className: "cc-choice-row", attrs: { role: "radiogroup", "aria-label": "Decision" } });
    [
      ["original", "Use original"],
      ["secondary", "Use cross-check"],
      ["edited", "Edit and publish"],
    ].forEach(([value, label]) => {
      const id = "ccDecision-" + value;
      const wrap = element("label", { className: "cc-choice" });
      wrap.appendChild(element("input", { attrs: { type: "radio", name: "ccDecision", id, value } }));
      wrap.appendChild(element("span", { text: label }));
      choices.appendChild(wrap);
    });
    form.appendChild(choices);
    const editor = element("div", { className: "cc-editor", attrs: { id: "ccEditor", hidden: "" } });
    editor.appendChild(element("p", { text: "Start from a copy of one manuscript. The original evidence stays read-only." }));
    const bases = element("div", { className: "cc-choice-row" });
    [
      ["original", "Start from original"],
      ["secondary", "Start from cross-check"],
    ].forEach(([value, label]) => {
      const wrap = element("label", { className: "cc-choice" });
      wrap.appendChild(element("input", { attrs: { type: "radio", name: "ccEditBase", value } }));
      wrap.appendChild(element("span", { text: label }));
      bases.appendChild(wrap);
    });
    editor.appendChild(bases);
    editor.appendChild(element("div", { attrs: { id: "ccEditorFields" } }));
    form.appendChild(editor);
    form.appendChild(element("label", { className: "field-label", attrs: { for: "ccDecisionReason" }, text: "Reason" }));
    form.appendChild(element("textarea", { attrs: { id: "ccDecisionReason", rows: "3", maxlength: "4000", required: "" } }));
    form.appendChild(element("p", { className: "form-error", attrs: { id: "ccDecisionError", role: "alert", hidden: "" } }));
    form.appendChild(element("div", { className: "cc-confirm-box", attrs: { id: "ccDecisionSummary", hidden: "" } }));
    const actions = element("div", { className: "cc-decision-actions" });
    actions.appendChild(element("button", { className: "button button-primary", type: "submit", text: "Confirm decision", attrs: { id: "ccConfirmDecision" } }));
    actions.appendChild(element("button", { className: "button button-secondary", type: "button", text: "Copy local draft", attrs: { id: "ccCopyDraft", hidden: "" } }));
    form.appendChild(actions);
    return form;
  }

  function renderCancelForm(detail) {
    const form = element("form", { attrs: { id: "ccCancelForm" } });
    form.appendChild(element("h4", { text: "Cancel cross-check" }));
    form.appendChild(element("p", { text: "This ends the round and releases the secondary assignment. The cross-check transcript is not published." }));
    form.appendChild(element("label", { className: "field-label", attrs: { for: "ccCancelReason" }, text: "Reason" }));
    form.appendChild(element("textarea", { attrs: { id: "ccCancelReason", rows: "3", maxlength: "4000", required: "" } }));
    form.appendChild(element("p", { className: "form-error", attrs: { id: "ccCancelError", role: "alert", hidden: "" } }));
    form.appendChild(element("button", { className: "button button-danger", type: "submit", text: "Cancel cross-check", attrs: { id: "ccCancelButton" } }));
    return form;
  }

  function renderDecisionRecord(detail) {
    const box = element("div", { className: "cc-record" });
    box.appendChild(element("h4", { text: "Resolution" }));
    if (detail.decision) box.appendChild(element("p", { text: "Decision: " + decisionLabel(detail.decision) }));
    if (detail.decision_reason) box.appendChild(element("p", { text: "Reason: " + detail.decision_reason }));
    if (detail.final_version_id) box.appendChild(element("p", { className: "mono-cell", text: "Final version " + detail.final_version_id }));
    if (detail.termination_reason) box.appendChild(element("p", { text: "Termination reason: " + detail.termination_reason }));
    if (detail.state === "adjudicated") {
      box.appendChild(element("p", { text: "Adjudication is complete. Training eligibility still depends on the published status (including skipped results)." }));
    }
    return box;
  }

  function decisionLabel(decision) {
    if (decision === "original") return "Use original";
    if (decision === "secondary") return "Use cross-check";
    if (decision === "edited") return "Edit and publish";
    return String(decision || "");
  }

  function renderAudit(detail) {
    const node = element("details", { className: "cc-audit" });
    node.appendChild(element("summary", { text: "Audit details" }));
    const list = element("dl", { className: "cc-stat-grid" });
    [
      ["Round", detail.round_id],
      ["Revision", detail.revision],
      ["Original version", detail.original_version_id],
      ["Cross-check version", detail.secondary_version_id],
      ["Current published version", detail.current_published_version_id || "—"],
      ["Comparison", detail.comparison_version],
      ["Sampling snapshot", `${detail.sampling_rate_bps} bps · settings revision ${detail.settings_revision}`],
      ["Created", host.formatDateTime(detail.created_at)],
      ["Submitted", host.formatDateTime(detail.submitted_at)],
      ["Resolved", host.formatDateTime(detail.resolved_at)],
    ].forEach(([label, value]) => {
      list.appendChild(element("div", {}, [element("dt", { text: label }), element("dd", { className: "mono-cell", text: value == null ? "—" : String(value) })]));
    });
    node.appendChild(list);
    return node;
  }

  function highlightAll(detail) {
    /* highlighting already applied during renderSide */
    updateDiffPos(detail);
  }

  function differenceOps(detail) {
    return (detail.diff_ops || []).map((op, index) => ({ op, index })).filter((item) => item.op && item.op.op !== "match");
  }

  function updateDiffPos(detail) {
    const diffs = differenceOps(detail);
    const node = $("ccDiffPos");
    if (!node) return;
    if (!diffs.length) {
      text(node, "No word differences");
      return;
    }
    const current = ui.diffIndex >= 0 ? ui.diffIndex + 1 : 0;
    text(node, `${current} / ${diffs.length}`);
  }

  function bindDetailEvents(detail) {
    const audio = $("ccAudio");
    if (audio) {
      audio.addEventListener("error", () => {
        if (audio.readyState === 0) {
          text($("ccAudioStatus"), "Audio could not be loaded. Transcripts are still available.");
        }
      });
      audio.addEventListener("loadedmetadata", () => text($("ccAudioStatus"), "Audio ready."));
    }
    $("ccPrevDiff")?.addEventListener("click", () => stepDiff(detail, -1));
    $("ccNextDiff")?.addEventListener("click", () => stepDiff(detail, 1));
    $("ccReviewBody").addEventListener("click", (event) => {
      const mark = event.target.closest("mark[data-diff-index]");
      if (mark) {
        const index = Number(mark.dataset.diffIndex);
        const diffs = differenceOps(detail);
        ui.diffIndex = diffs.findIndex((item) => item.index === index);
        focusDiff(detail, index);
      }
    });
    $("ccDecisionForm")?.addEventListener("change", (event) => {
      if (event.target.name === "ccDecision") onDecisionChoice(detail);
      if (event.target.name === "ccEditBase") onEditorBase(detail, event.target.value);
    });
    $("ccDecisionForm")?.addEventListener("submit", (event) => {
      event.preventDefault();
      submitDecision(detail);
    });
    $("ccCancelForm")?.addEventListener("submit", (event) => {
      event.preventDefault();
      submitCancel(detail);
    });
    $("ccCopyDraft")?.addEventListener("click", copyEditorDraft);
  }

  function onDecisionChoice(detail) {
    const choice = selectedDecision();
    if (choice !== "edited" && ui.editor && ui.editor.dirty && !confirmDiscard()) {
      $("ccDecision-edited").checked = true;
      return;
    }
    $("ccEditor").hidden = choice !== "edited";
    if (choice !== "edited") {
      ui.editor = null;
      ui.editorBase = "";
    }
    $("ccDecisionSummary").hidden = true;
    delete $("ccDecisionSummary").dataset.ready;
    ui.frozenDecision = null;
  }

  function selectedDecision() {
    const node = document.querySelector('input[name="ccDecision"]:checked');
    return node ? node.value : "";
  }

  function onEditorBase(detail, base) {
    if (ui.editor && ui.editor.dirty && ui.editorBase && ui.editorBase !== base) {
      if (!confirmDiscard()) {
        document.querySelector(`input[name="ccEditBase"][value="${ui.editorBase}"]`).checked = true;
        return;
      }
    }
    const source = base === "secondary" ? detail.secondary_segments : detail.original_segments;
    ui.editorBase = base;
    ui.editor = {
      base: base,
      dirty: false,
      target_status: base === "secondary" ? (detail.secondary_target_status || "annotated") : (detail.original_target_status || "annotated"),
      skip_reasons: CC().cloneJson(base === "secondary" ? (detail.secondary_skip_reasons || []) : (detail.original_skip_reasons || [])),
      segments: (source || []).map(CC().editorSegment),
    };
    ui.sceneOverride = false;
    ui.sceneReview = null;
    renderEditorFields(detail);
  }

  function renderEditorFields(detail) {
    const hostNode = $("ccEditorFields");
    if (!hostNode || !ui.editor) return;
    clear(hostNode);
    const statusRow = element("div", { className: "cc-choice-row", attrs: { role: "radiogroup", "aria-label": "Result status" } });
    [["annotated", "Annotated"], ["skipped", "Skipped"]].forEach(([value, label]) => {
      const wrap = element("label", { className: "cc-choice" });
      const input = element("input", { attrs: { type: "radio", name: "ccTargetStatus", value } });
      if (ui.editor.target_status === value) input.checked = true;
      wrap.appendChild(input);
      wrap.appendChild(element("span", { text: label }));
      statusRow.appendChild(wrap);
    });
    hostNode.appendChild(statusRow);
    const skipBox = element("div", { className: "cc-skip-reasons", attrs: { id: "ccSkipReasons" } });
    CC().SKIP_REASONS.forEach(([value, label]) => {
      const wrap = element("label", { className: "cc-choice" });
      const box = element("input", { attrs: { type: "checkbox", value } });
      box.checked = (ui.editor.skip_reasons || []).includes(value);
      wrap.appendChild(box);
      wrap.appendChild(element("span", { text: label }));
      skipBox.appendChild(wrap);
    });
    skipBox.hidden = ui.editor.target_status !== "skipped";
    hostNode.appendChild(skipBox);
    const table = element("div", { className: "cc-editor-table" });
    ui.editor.segments.forEach((segment, index) => {
      const row = element("div", { className: "cc-editor-row" });
      row.appendChild(element("span", { className: "mono-cell", attrs: { dir: "ltr" }, text: "#" + segment.id + " " + formatSpan(segment.start, segment.end) }));
      const area = element("textarea", {
        className: "cc-editor-text",
        attrs: { dir: "auto", "data-editor-index": String(index), rows: "2" },
      });
      area.value = segment.text || "";
      row.appendChild(area);
      const bq = element("label", { className: "cc-choice" });
      const box = element("input", { attrs: { type: "checkbox", "data-editor-bq": String(index) } });
      box.checked = Boolean(segment.exclude_from_training);
      bq.appendChild(box);
      bq.appendChild(element("span", { text: "Bad quality" }));
      row.appendChild(bq);
      table.appendChild(row);
    });
    hostNode.appendChild(table);
    const override = element("label", { className: "cc-choice" });
    const overrideBox = element("input", { attrs: { type: "checkbox", id: "ccSceneOverride" } });
    overrideBox.checked = ui.sceneOverride;
    override.appendChild(overrideBox);
    override.appendChild(element("span", { text: "Override scene review" }));
    hostNode.appendChild(override);
    const sceneHost = element("div", { attrs: { id: "ccSceneReviewHost" } });
    sceneHost.hidden = !ui.sceneOverride;
    hostNode.appendChild(sceneHost);
    bindEditorEvents(detail, hostNode, sceneHost);
    if (ui.sceneOverride) renderSceneOverride(sceneHost);
    $("ccCopyDraft").hidden = false;
  }

  function bindEditorEvents(detail, hostNode, sceneHost) {
    hostNode.addEventListener("input", (event) => {
      const area = event.target.closest("[data-editor-index]");
      if (!area || !ui.editor) return;
      ui.editor.segments[Number(area.dataset.editorIndex)].text = area.value;
      ui.editor.dirty = true;
    });
    hostNode.addEventListener("change", (event) => {
      const target = event.target;
      if (target.name === "ccTargetStatus" && ui.editor) {
        ui.editor.target_status = target.value;
        ui.editor.dirty = true;
        $("ccSkipReasons").hidden = target.value !== "skipped";
      }
      if (target.dataset.editorBq && ui.editor) {
        ui.editor.segments[Number(target.dataset.editorBq)].exclude_from_training = target.checked;
        ui.editor.dirty = true;
      }
      if (target.closest("#ccSkipReasons") && ui.editor) {
        ui.editor.skip_reasons = Array.from($("ccSkipReasons").querySelectorAll("input:checked")).map((node) => node.value);
        ui.editor.dirty = true;
      }
      if (target.id === "ccSceneOverride") {
        ui.sceneOverride = target.checked;
        sceneHost.hidden = !ui.sceneOverride;
        if (ui.sceneOverride) renderSceneOverride(sceneHost);
        else ui.sceneReview = null;
        ui.editor.dirty = true;
      }
    });
  }

  function renderSceneOverride(container) {
    const catalogFailed = host.catalogFailed();
    ui.sceneCatalogOk = !catalogFailed;
    if (catalogFailed) {
      clear(container);
      container.appendChild(element("p", { className: "form-error", text: "Scene catalog failed to load. Scene override will not be sent." }));
      return;
    }
    const scenes = Object.entries(host.getSceneLabels() || {}).map(([code, label]) => ({ code, label_en: label }));
    const value = ui.sceneReview || { status: "pending", scene_codes: [], note: "" };
    if (root.AnnotationMetadata) {
      ui.sceneReview = AnnotationMetadata.renderReviewControls(container, {
        value: value,
        scenes: scenes,
        onChange: (next) => { ui.sceneReview = next; if (ui.editor) ui.editor.dirty = true; },
      });
    }
  }

  function stepDiff(detail, delta) {
    const diffs = differenceOps(detail);
    if (!diffs.length) return;
    if (ui.diffIndex < 0) ui.diffIndex = delta > 0 ? 0 : diffs.length - 1;
    else ui.diffIndex = (ui.diffIndex + delta + diffs.length) % diffs.length;
    focusDiff(detail, diffs[ui.diffIndex].index);
  }

  function focusDiff(detail, opIndex) {
    const op = (detail.diff_ops || [])[opIndex];
    if (!op) return;
    ["original", "secondary"].forEach((side) => {
      const mapping = op[side];
      if (!mapping) return;
      const node = document.querySelector(`[data-cc-side="${side}"][data-segment-id="${mapping.segment_id}"]`);
      if (node) {
        node.hidden = false;
        node.scrollIntoView({ block: "nearest" });
      }
    });
    const mark = document.querySelector(`mark[data-diff-index="${opIndex}"]`);
    if (mark) {
      mark.focus({ preventScroll: true });
      mark.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    playOp(op);
    updateDiffPos(detail);
  }

  function playOp(op) {
    const mapping = CC().playableMapping(op);
    const audio = $("ccAudio");
    if (!mapping || !audio) return;
    const start = Number(mapping.start_s);
    const end = Number(mapping.end_s);
    const begin = () => {
      try {
        audio.currentTime = start;
        audio.dataset.stop = String(end);
        audio.play().catch(() => text($("ccAudioStatus"), "Audio could not play."));
      } catch (_) {
        text($("ccAudioStatus"), "Audio is not ready yet.");
      }
    };
    if (audio.readyState >= 1) begin();
    else audio.addEventListener("loadedmetadata", begin, { once: true });
    audio.ontimeupdate = () => {
      const stop = Number(audio.dataset.stop || 0);
      if (stop && audio.currentTime >= stop - 0.02) {
        audio.pause();
        audio.dataset.stop = "";
      }
    };
  }

  function stopAudio() {
    const audio = $("ccAudio");
    if (!audio) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }

  function validateEdited() {
    if (!ui.editor) return "Choose a starting manuscript before editing.";
    if (ui.editor.target_status === "skipped" && !(ui.editor.skip_reasons || []).length) {
      return "Select at least one skip reason.";
    }
    if (ui.editor.target_status === "annotated") {
      const empty = ui.editor.segments.filter((segment) => !(segment.text || "").trim() && !segment.exclude_from_training);
      if (empty.length) return "Add text or mark Bad quality for every segment.";
    }
    if (ui.sceneOverride) {
      if (!ui.sceneCatalogOk) return "Scene catalog is unavailable, so a scene override cannot be sent.";
      const check = root.AnnotationMetadata ? AnnotationMetadata.validateReview(ui.sceneReview) : { ok: true };
      if (!check.ok) return check.error;
    }
    return "";
  }

  function buildDecisionBody(detail) {
    const decision = selectedDecision();
    const body = {
      operation_id: host.uuid(),
      expected_revision: Number(detail.revision),
      expected_original_version_id: detail.original_version_id,
      expected_secondary_version_id: detail.secondary_version_id,
      decision: decision,
      reason: CC().trimmedReason($("ccDecisionReason").value),
    };
    if (decision === "edited") {
      body.base = ui.editor.base;
      body.target_status = ui.editor.target_status;
      body.skip_reasons = ui.editor.target_status === "skipped" ? ui.editor.skip_reasons : [];
      body.segments = ui.editor.segments.map(CC().decisionSegment);
      if (ui.sceneOverride && ui.sceneReview && ui.sceneCatalogOk) {
        body.scene_review = {
          status: ui.sceneReview.status,
          scene_codes: ui.sceneReview.scene_codes || [],
          note: ui.sceneReview.note || "",
        };
      }
    }
    return body;
  }

  function impactText(decision, detail) {
    if (decision === "original") return "Keeps the original published version. This round will no longer block training export.";
    if (decision === "secondary") return "Publishes the cross-check transcript and its annotated or skipped result. This round will no longer block training export.";
    return "Publishes a new administrator version from the edited copy. This round will no longer block training export.";
  }

  function showDecisionSummary(detail, body) {
    const box = $("ccDecisionSummary");
    clear(box);
    box.hidden = false;
    box.appendChild(element("h5", { text: "Confirm this decision" }));
    box.appendChild(element("p", { text: "Result: " + decisionLabel(body.decision) }));
    if (body.decision === "edited") {
      box.appendChild(element("p", { text: `Starting manuscript: ${body.base === "secondary" ? "cross-check" : "original"}. Status: ${body.target_status}.` }));
    }
    box.appendChild(element("p", { text: "Reason: " + body.reason }));
    box.appendChild(element("p", { text: impactText(body.decision, detail) }));
    box.dataset.ready = "1";
  }

  async function submitDecision(detail) {
    if (ui.pendingWrite) return;
    formError($("ccDecisionError"), "");
    const decision = selectedDecision();
    if (!decision) {
      formError($("ccDecisionError"), "Choose a decision.");
      return;
    }
    const reasonErr = CC().reasonError($("ccDecisionReason").value);
    if (reasonErr) {
      formError($("ccDecisionError"), reasonErr);
      $("ccDecisionReason").focus();
      return;
    }
    if (decision === "edited") {
      const editErr = validateEdited();
      if (editErr) {
        formError($("ccDecisionError"), editErr);
        return;
      }
    }
    if (!ui.frozenDecision) {
      const summary = $("ccDecisionSummary");
      if (!summary.dataset.ready) {
        showDecisionSummary(detail, buildDecisionBody(detail));
        host.announce("Review the decision summary, then confirm again to submit.");
        return;
      }
      ui.frozenDecision = buildDecisionBody(detail);
    }
    ui.pendingWrite = true;
    freezeDecisionControls(true);
    try {
      const result = await host.api.post(
        `/api/admin/cross-checks/${encodeURIComponent(detail.round_id)}/decision`,
        ui.frozenDecision,
      );
      ui.frozenDecision = null;
      ui.editor = null;
      ui.retainedDraft = null;
      $("ccDecisionReason").value = "";
      host.toast(`Decision saved (${CC().stateLabel(result.state)}).`);
      await Promise.allSettled([loadSummary(), loadList(true), openRound(detail.round_id, { afterWrite: true })]);
    } catch (error) {
      if (error.status === 400) {
        ui.frozenDecision = null;
        $("ccDecisionSummary").dataset.ready = "";
        formError($("ccDecisionError"), error.message);
      } else if (error.status === 409) {
        ui.retainedDraft = { round_id: detail.round_id, ...CC().cloneJson(ui.frozenDecision) };
        delete ui.retainedDraft.operation_id;
        ui.frozenDecision = null;
        host.toast("This round changed. Your local draft is kept. Showing the latest server result.", "error");
        formError($("ccDecisionError"), "This round changed. Your local draft is kept. Reload the round before deciding again.");
        await openRound(detail.round_id, { preserveLocal: true, afterWrite: true });
      } else {
        formError($("ccDecisionError"), (error.message || "Request failed") + " Retry will resend the same decision.");
      }
    } finally {
      ui.pendingWrite = false;
      freezeDecisionControls(false);
    }
  }

  function freezeDecisionControls(frozen) {
    const form = $("ccDecisionForm");
    if (!form) return;
    for (const node of form.querySelectorAll("input, textarea, button, select")) {
      if (node.id === "ccCopyDraft" || node.id === "ccConfirmDecision") continue;
      node.disabled = Boolean(frozen || ui.frozenDecision);
    }
    host.setButtonBusy($("ccConfirmDecision"), frozen, "Submitting…");
    if (!frozen) text($("ccConfirmDecision"), ui.frozenDecision ? "Retry same decision" : "Confirm decision");
  }

  async function submitCancel(detail) {
    if (ui.pendingWrite) return;
    formError($("ccCancelError"), "");
    const reasonErr = CC().reasonError($("ccCancelReason").value);
    if (reasonErr) {
      formError($("ccCancelError"), reasonErr);
      return;
    }
    if (!window.confirm("Cancel this cross-check? The secondary assignment will be released and the transcript will not be published.")) return;
    if (!ui.frozenCancel) {
      ui.frozenCancel = {
        operation_id: host.uuid(),
        expected_revision: Number(detail.revision),
        reason: CC().trimmedReason($("ccCancelReason").value),
        confirm: true,
      };
    }
    ui.pendingWrite = true;
    host.setButtonBusy($("ccCancelButton"), true, "Cancelling…");
    try {
      await host.api.post(`/api/admin/cross-checks/${encodeURIComponent(detail.round_id)}/cancel`, ui.frozenCancel);
      ui.frozenCancel = null;
      host.toast("Cross-check cancelled.");
      await Promise.allSettled([loadSummary(), loadList(true), openRound(detail.round_id, { afterWrite: true })]);
    } catch (error) {
      if (error.status === 400) {
        ui.frozenCancel = null;
        formError($("ccCancelError"), error.message);
      } else if (error.status === 409) {
        ui.frozenCancel = null;
        formError($("ccCancelError"), "This round is no longer in progress. Reloading.");
        await openRound(detail.round_id, { afterWrite: true });
      } else {
        formError($("ccCancelError"), error.message + " Retry will resend the same cancel request.");
      }
    } finally {
      ui.pendingWrite = false;
      host.setButtonBusy($("ccCancelButton"), false);
    }
  }

  function renderRetainedDraft(container) {
    const draft = ui.retainedDraft;
    if (!draft) return;
    const section = element("section", { className: "cc-record", attrs: { id: "ccRetainedDraft" } }, [
      element("h4", { text: "Your unsaved decision" }),
      element("p", { text: "The round changed before your decision was saved. This local draft is kept for copying; it has not been published." }),
      element("p", { text: "Decision: " + decisionLabel(draft.decision) }),
      element("p", { text: "Reason: " + draft.reason }),
    ]);
    for (const segment of draft.segments || []) {
      section.appendChild(element("p", { className: "cc-transcript", attrs: { dir: "auto" }, text: segment.text }));
    }
    const copy = element("button", { className: "button button-secondary", type: "button", text: "Copy local draft", attrs: { id: "ccCopyRetainedDraft" } });
    copy.addEventListener("click", () => copyDraft(draft));
    section.appendChild(copy);
    container.appendChild(section);
  }

  function copyEditorDraft() {
    if (!ui.editor) return;
    return copyDraft({
      round_id: ui.detail && ui.detail.round_id,
      decision: "edited",
      reason: $("ccDecisionReason")?.value || "",
      base: ui.editor.base,
      target_status: ui.editor.target_status,
      skip_reasons: ui.editor.skip_reasons,
      segments: ui.editor.segments.map(CC().decisionSegment),
      scene_review: ui.sceneOverride ? ui.sceneReview : null,
    });
  }

  async function copyDraft(draft) {
    const payload = JSON.stringify(draft, null, 2);
    try {
      await navigator.clipboard.writeText(payload);
      host.toast("Local adjudication draft copied. It is not saved on the server.");
    } catch (_) {
      const blob = new Blob([payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = element("a", { attrs: { href: url, download: "cross-check-draft.json" } });
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      host.toast("Download started. This draft is not saved on the server.");
    }
  }

  function formError(node, message) {
    if (!node) return;
    node.hidden = !message;
    node.textContent = message || "";
  }

  function closeDialog(dialog) {
    host.closeDialog(dialog);
  }

  function onFiltersChanged() {
    readFilterFields();
    ui.selectedRoundId = "";
    host.syncUrl();
    loadList(true);
  }

  function startPolling() {
    stopPolling();
    ui.pollTimer = window.setInterval(() => {
      if (document.hidden) return;
      if (host.currentView() !== "cross-checks") return;
      if (hasPendingWrite()) return;
      loadSummary();
    }, POLL_MS);
  }

  function stopPolling() {
    if (ui.pollTimer) window.clearInterval(ui.pollTimer);
    ui.pollTimer = 0;
  }

  function clearSensitive() {
    stopPolling();
    stopAudio();
    if (ui.listAbort) ui.listAbort.abort();
    if (ui.detailAbort) ui.detailAbort.abort();
    ui.items = [];
    ui.detail = null;
    ui.editor = null;
    ui.retainedDraft = null;
    ui.frozenDecision = null;
    ui.frozenCancel = null;
    ui.frozenSettings = null;
    ui.summary = null;
    ui.settings = null;
    ui.settingsLoaded = false;
    ui.staleSettings = null;
    ui.selectedRoundId = "";
    ui.showingDetail = false;
    clear($("ccReviewBody"));
  }

  function leaveView() {
    stopPolling();
    stopAudio();
    if (ui.listAbort) ui.listAbort.abort();
    if (ui.detailAbort) ui.detailAbort.abort();
    resetEditorState();
    ui.retainedDraft = null;
    ui.showingDetail = false;
    clear($("ccReviewBody"));
  }

  function bindChrome() {
    $("ccFilterForm")?.addEventListener("submit", (event) => {
      event.preventDefault();
      onFiltersChanged();
    });
    $("ccSearch")?.addEventListener("input", () => {
      window.clearTimeout(ui.searchTimer);
      ui.searchTimer = window.setTimeout(onFiltersChanged, SEARCH_MS);
    });
    ["ccState", "ccSourceScene", "ccBatch", "ccOriginalAnnotator", "ccSecondaryAnnotator", "ccReason", "ccFrom", "ccTo"].forEach((id) => {
      $(id)?.addEventListener("change", onFiltersChanged);
    });
    $("ccLoadMore")?.addEventListener("click", async () => {
      host.setButtonBusy($("ccLoadMore"), true, "Loading…");
      await loadList(false);
      host.setButtonBusy($("ccLoadMore"), false);
    });
    $("ccListBody")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-cc-round]");
      if (button) openRound(button.dataset.ccRound);
    });
    $("ccBackToList")?.addEventListener("click", () => {
      if (!canLeaveView()) return;
      ui.selectedRoundId = "";
      resetEditorState();
      ui.retainedDraft = null;
      host.syncUrl();
      showList();
      renderList();
    });
    $("ccSamplingButton")?.addEventListener("click", async () => {
      host.openDialog($("ccSettingsDialog"));
      $("ccSamplingEnabled").focus();
      if (!ui.settingsLoaded) await loadSettings();
    });
    $("ccSettingsForm")?.addEventListener("submit", (event) => {
      event.preventDefault();
      saveSettings();
    });
    $("ccSummaryRetry")?.addEventListener("click", loadSummary);
    $("ccReviewBody")?.addEventListener("click", (event) => {
      const retry = event.target.closest("[data-cc-retry-round]");
      if (retry) openRound(retry.dataset.ccRetryRound, { preserveLocal: Boolean(ui.retainedDraft) });
    });
    document.addEventListener("click", (event) => {
      const open = event.target.closest("[data-open-cross-checks]");
      if (!open) return;
      ui.filters = emptyFilters();
      ui.filters.state = open.dataset.openCrossChecks || "awaiting_review";
      ui.selectedRoundId = "";
      writeFilterFields();
      host.navigate("cross-checks");
    });
    window.addEventListener("beforeunload", (event) => {
      if (!hasPendingWrite()) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  root.AdminCrossCheck = {
    init: function (injected) {
      host = injected;
      bindChrome();
    },
    loadView: loadView,
    leaveView: leaveView,
    contributeUrl: contributeUrl,
    restoreFromUrl: restoreFromUrl,
    fillFilterOptions: fillFilterOptions,
    renderOverviewCards: renderOverviewCards,
    renderQualityCard: function (data) {
      const cc = data && (data.cross_check || (data.stats && data.cross_check) || data);
      if (cc && typeof cc.pending_review_count !== "undefined") {
        text($("qualityCrossCheckPending"), host.formatInteger(cc.pending_review_count || 0));
      }
    },
    hasPendingWrite: hasPendingWrite,
    canLeaveView: canLeaveView,
    clearSensitive: clearSensitive,
    openAwaitingReview: function () {
      ui.filters = emptyFilters();
      writeFilterFields();
    },
  };
})(window);
