"use strict";

const $ = (id) => document.getElementById(id);
const CSRF_STORAGE_KEY = "annotation.admin.csrf";
const VIEWS = new Set(["overview", "annotators", "corpus", "quality", "activity"]);
const VIEW_LABELS = {
  overview: "Overview",
  annotators: "Annotators",
  corpus: "Tasks & corpus",
  quality: "Review & quality",
  activity: "Activity log",
};

class ApiError extends Error {
  constructor(message, status = 0, body = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body || {};
  }
}

const state = {
  authenticated: false,
  csrfToken: "",
  keyId: "",
  view: "overview",
  range: "30d",
  dateFrom: "",
  dateTo: "",
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  overview: null,
  timeseries: [],
  annotators: [],
  selectedAnnotatorId: "",
  annotatorDetail: null,
  annotatorTasks: [],
  annotatorTaskCursor: null,
  selectedTaskIds: new Set(),
  corpusTasks: [],
  corpusTaskCursor: null,
  auditItems: [],
  auditCursor: null,
  qualityItems: [],
  qualityQuery: "",
  qualityAnnotatorId: "",
  qualitySignal: "",
  currentTaskDetail: null,
  pendingRevoke: null,
  pendingDeactivate: null,
  requestEpoch: 0,
  scopeLoadToken: 0,
  scopeSaving: false,
  scopeSavingId: "",
  scopeEditorBound: null,
  scopeCatalogFailed: false,
  sceneLabels: {},
  metadataFilters: {},
};

const CONF_LABEL = {
  high: "Source confidence High",
  medium: "Source confidence Medium",
  low: "Source confidence Low",
  unknown: "Source confidence Unknown",
};
const REVIEW_STATUS_LABEL = {
  pending: "Pending (published)",
  confirmed: "Confirmed",
  mixed: "Multiple scenes",
  out_of_scope: "Outside the ten scenes",
  uncertain: "Cannot determine",
  unreviewed_unpublished: "Unpublished, not reviewed",
  unreviewed_published: "Published, not reviewed",
};

const charts = {
  activity: { points: [], xPositions: [], padding: null },
  annotator: { points: [] },
};

function clear(node) {
  if (node) node.replaceChildren();
}

function text(node, value, fallback = "—") {
  if (!node) return;
  const shown = value === null || value === undefined || value === "" ? fallback : value;
  node.textContent = String(shown);
}

function element(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.title) node.title = String(options.title);
  if (options.type) node.type = options.type;
  if (options.value !== undefined) node.value = String(options.value);
  if (options.dataset) {
    for (const [key, value] of Object.entries(options.dataset)) node.dataset[key] = String(value);
  }
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child instanceof Node) node.appendChild(child);
    else if (child !== null && child !== undefined) node.appendChild(document.createTextNode(String(child)));
  }
  return node;
}

function pathValue(source, path) {
  if (!source || typeof source !== "object") return undefined;
  let value = source;
  for (const part of String(path).split(".")) {
    if (!value || typeof value !== "object" || !(part in value)) return undefined;
    value = value[part];
  }
  return value;
}

function pick(source, names, fallback = undefined) {
  for (const name of names) {
    const value = pathValue(source, name);
    if (value !== undefined && value !== null) return value;
  }
  return fallback;
}

function numberValue(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function boolValue(value, fallback = false) {
  if (value === undefined || value === null) return fallback;
  if (typeof value === "string") return value.toLowerCase() === "true" || value === "1";
  return Boolean(value);
}

function listValue(source, names) {
  if (Array.isArray(source)) return source;
  const result = pick(source, names, []);
  return Array.isArray(result) ? result : [];
}

function setCsrfToken(token) {
  state.csrfToken = typeof token === "string" ? token : "";
  try {
    if (state.csrfToken) sessionStorage.setItem(CSRF_STORAGE_KEY, state.csrfToken);
    else sessionStorage.removeItem(CSRF_STORAGE_KEY);
  } catch (_) {
    // A locked-down browser may disable session storage; the in-memory token still works.
  }
}

function storedCsrfToken() {
  try { return sessionStorage.getItem(CSRF_STORAGE_KEY) || ""; }
  catch (_) { return ""; }
}

const api = {
  async request(path, { method = "GET", body, signal, allowUnauthorized = false } = {}) {
    const upperMethod = method.toUpperCase();
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (!["GET", "HEAD", "OPTIONS"].includes(upperMethod)) {
      const csrf = state.csrfToken || storedCsrfToken();
      if (!csrf && path !== "/api/admin/login") {
        throw new ApiError("The security token is missing. Sign in again before changing data.", 403, { code: "csrf_missing" });
      }
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }

    let response;
    try {
      response = await fetch(path, {
        method: upperMethod,
        headers,
        credentials: "same-origin",
        cache: "no-store",
        signal,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new ApiError("Could not reach the server. Check the connection and try again.", 0);
    }

    let data = {};
    const raw = await response.text();
    if (raw) {
      try { data = JSON.parse(raw); }
      catch (_) { throw new ApiError("The server returned an unreadable response.", response.status); }
    }

    if (response.status === 401 && !allowUnauthorized) {
      enterLogin("Your admin session expired. Sign in again.");
      throw new ApiError("Admin session expired.", 401, data);
    }
    if (!response.ok) {
      const message = pick(data, ["error", "message", "detail"], `Request failed (${response.status})`);
      throw new ApiError(String(message), response.status, data);
    }
    return data;
  },
  get(path, options) { return this.request(path, options); },
  post(path, body, options = {}) { return this.request(path, { ...options, method: "POST", body }); },
};

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

function formatInteger(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(numberValue(value));
}

function formatPercent(value, digits = 0) {
  let number = numberValue(value);
  if (Math.abs(number) <= 1 && number !== 0) number *= 100;
  return `${number.toFixed(digits)}%`;
}

function formatDuration(seconds, compact = true) {
  const total = Math.max(0, Math.round(numberValue(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${compact ? "" : `${total % 60}s`}`.trim();
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  if (hours < 24) return remaining ? `${hours}h ${remaining}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function formatSecondsAsTurnaround(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  return formatDuration(seconds, false);
}

function formatDateTime(value, dateOnly = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, dateOnly
    ? { year: "numeric", month: "short", day: "numeric", timeZone: state.timezone }
    : { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: state.timezone }
  ).format(date);
}

function formatRelative(value) {
  if (!value) return "Never";
  const date = new Date(value);
  const delta = Date.now() - date.getTime();
  if (!Number.isFinite(delta)) return String(value);
  if (delta < 60_000) return "Just now";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`;
  if (delta < 604_800_000) return `${Math.floor(delta / 86_400_000)}d ago`;
  return formatDateTime(value, true);
}

function localDateString(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftDate(dateString, days) {
  const [year, month, day] = dateString.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function applyPreset(range) {
  const today = localDateString(new Date());
  const days = range === "7d" ? 7 : range === "90d" ? 90 : 30;
  if (range === "all") {
    state.dateFrom = "";
    state.dateTo = "";
  } else if (range !== "custom") {
    state.dateFrom = shiftDate(today, -(days - 1));
    state.dateTo = today;
  }
  $("dateFrom").value = state.dateFrom;
  $("dateTo").value = state.dateTo;
}

function commonQuery(extra = {}) {
  const params = new URLSearchParams();
  if (state.range !== "all") {
    if (state.dateFrom) params.set("from", state.dateFrom);
    if (state.dateTo) params.set("to", shiftDate(state.dateTo, 1));
  }
  params.set("timezone", state.timezone);
  params.set("bucket", state.range === "90d" ? "week" : "day");
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  return params;
}

function apiUrl(path, extra = {}) {
  const params = commonQuery(extra);
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

function syncUrl() {
  const url = new URL(location.href);
  url.search = "";
  url.searchParams.set("view", state.view);
  url.searchParams.set("range", state.range);
  if (state.range === "custom") {
    if (state.dateFrom) url.searchParams.set("from", state.dateFrom);
    if (state.dateTo) url.searchParams.set("to", state.dateTo);
  }
  if (state.selectedAnnotatorId) url.searchParams.set("annotator", state.selectedAnnotatorId);
  const corpusQuery = $("corpusSearch")?.value.trim();
  const corpusStatus = $("corpusStatus")?.value;
  if (state.view === "corpus" && corpusQuery) url.searchParams.set("q", corpusQuery);
  if (state.view === "corpus" && corpusStatus) url.searchParams.set("status", corpusStatus);
  if (state.view === "quality" && state.qualityQuery) url.searchParams.set("q", state.qualityQuery);
  if (state.view === "quality" && state.qualityAnnotatorId) url.searchParams.set("annotator_id", state.qualityAnnotatorId);
  if (state.view === "quality" && state.qualitySignal) url.searchParams.set("signal", state.qualitySignal);
  history.replaceState(null, "", url);
}

function restoreUrlState() {
  const params = new URLSearchParams(location.search);
  const view = params.get("view");
  if (VIEWS.has(view)) state.view = view;
  const range = params.get("range");
  if (["7d", "30d", "90d", "all", "custom"].includes(range)) state.range = range;
  if (state.range === "custom") {
    state.dateFrom = params.get("from") || "";
    state.dateTo = params.get("to") || "";
  }
  state.selectedAnnotatorId = params.get("annotator") || "";
  if (state.view === "corpus") {
    $("corpusSearch").value = params.get("q") || "";
    $("corpusStatus").value = params.get("status") || "";
  }
  if (state.view === "quality") {
    state.qualityQuery = params.get("q") || "";
    state.qualityAnnotatorId = params.get("annotator_id") || "";
    state.qualitySignal = params.get("signal") || "";
    $("qualitySearch").value = state.qualityQuery;
    $("qualityAnnotator").value = state.qualityAnnotatorId;
    $("qualitySignal").value = state.qualitySignal;
  }
  $("dateRange").value = state.range;
  $("customDates").hidden = state.range !== "custom";
  applyPreset(state.range);
}

function announce(message) {
  const region = $("liveRegion");
  region.textContent = "";
  requestAnimationFrame(() => { region.textContent = String(message); });
}

function toast(message, type = "success", duration = 3600) {
  const node = element("div", { className: `toast${type === "error" ? " toast-error" : ""}` });
  node.append(element("i", { attrs: { "aria-hidden": "true" } }), element("span", { text: message }));
  $("toastRegion").appendChild(node);
  announce(message);
  setTimeout(() => node.remove(), duration);
}

function showStatus(message, retry = true) {
  text($("statusBannerText"), message);
  $("statusRetryButton").hidden = !retry;
  $("statusBanner").hidden = false;
}

function hideStatus() { $("statusBanner").hidden = true; }

function formError(node, message = "") {
  node.hidden = !message;
  node.textContent = message;
}

function setButtonBusy(button, busy, busyText = "Working…") {
  if (!button) return;
  if (busy) {
    button.dataset.normalText = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.normalText || button.textContent;
    button.disabled = false;
    delete button.dataset.normalText;
  }
}

function enterLogin(message = "") {
  state.authenticated = false;
  setCsrfToken("");
  $("revokeAdminKey").value = "";
  $("deactivateAdminKey").value = "";
  state.pendingRevoke = null;
  state.pendingDeactivate = null;
  closeDialog($("revokeDialog"));
  closeDialog($("deactivateDialog"));
  $("bootScreen").hidden = true;
  $("adminApp").hidden = true;
  $("loginView").hidden = false;
  formError($("loginError"), message);
  $("adminKey").value = "";
  setTimeout(() => $("adminKey").focus(), 0);
}

async function enterApp(session) {
  state.authenticated = true;
  state.keyId = pick(session, ["key_id", "admin_key_id"], "Admin session");
  setCsrfToken(pick(session, ["csrf_token"], storedCsrfToken()));
  text($("sessionKeyId"), state.keyId || "Admin session");
  $("bootScreen").hidden = true;
  $("loginView").hidden = true;
  $("adminApp").hidden = false;
  renderViewShell();
  const results = await Promise.allSettled([loadAnnotators(), loadCurrentView(), loadMetadataFacets()]);
  const failed = results.find((result) => result.status === "rejected" && result.reason?.name !== "AbortError");
  if (failed && failed.reason?.status !== 401) showStatus(failed.reason.message || "Some dashboard data could not be loaded.");
}

async function initialize() {
  setupEvents();
  restoreUrlState();
  state.csrfToken = storedCsrfToken();
  try {
    const session = await api.request("/api/admin/session", { allowUnauthorized: true });
    if (!boolValue(pick(session, ["authenticated"], false))) {
      enterLogin();
      return;
    }
    await enterApp(session);
  } catch (error) {
    if (error.status === 401) enterLogin();
    else enterLogin(error.message || "Could not verify the admin session.");
  }
}

function renderViewShell() {
  for (const section of document.querySelectorAll("[data-page]")) section.hidden = section.dataset.page !== state.view;
  for (const button of document.querySelectorAll("[data-view]")) {
    const active = button.dataset.view === state.view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  text($("pageTitle"), VIEW_LABELS[state.view]);
  text($("breadcrumbView"), VIEW_LABELS[state.view]);
  syncUrl();
  closeSidebar();
}

async function navigate(view, { focus = true } = {}) {
  if (!VIEWS.has(view)) return;
  state.view = view;
  renderViewShell();
  hideStatus();
  await loadCurrentView();
  if (focus) $("adminMain").focus({ preventScroll: true });
}

async function loadCurrentView() {
  const epoch = ++state.requestEpoch;
  $("refreshButton").classList.add("is-loading");
  $("refreshButton").disabled = true;
  try {
    if (state.view === "overview") await loadOverview(epoch);
    else if (state.view === "annotators" && state.selectedAnnotatorId) await loadAnnotator(state.selectedAnnotatorId, epoch);
    else if (state.view === "corpus") await loadCorpus(false, epoch);
    else if (state.view === "quality") await loadQuality(epoch);
    else if (state.view === "activity") await loadAudit(false, epoch);
  } catch (error) {
    if (error.name !== "AbortError" && error.status !== 401) showStatus(error.message || "Could not load this dashboard section.");
    throw error;
  } finally {
    $("refreshButton").classList.remove("is-loading");
    $("refreshButton").disabled = false;
  }
}

function normaliseAnnotator(raw) {
  const id = String(pick(raw, ["id", "annotator_id", "user_id"], ""));
  const username = String(pick(raw, ["username", "name", "annotator"], "Unknown annotator"));
  return {
    ...raw,
    id,
    username,
    status: String(pick(raw, ["status"], "active")),
    lastActiveAt: pick(raw, ["last_active_at", "last_seen_at", "last_activity_at", "history.last_completed_at", "assignment.last_activity_at"], null),
    online: boolValue(pick(raw, ["online", "is_online", "has_active_assignment"], false)),
    currentCount: numberValue(pick(raw, ["current_count", "current_contributions", "annotated"], 0))
      || numberValue(pick(raw, ["current.annotated_count"], 0)) + numberValue(pick(raw, ["current.skipped_count"], 0)),
  };
}

function sceneDisplayName(scene) {
  return scene.label_en || scene.label || scene.label_zh || scene.code;
}

function sceneLabel(code) {
  if (!code || code === "unknown") return "";
  return state.sceneLabels[code] || (window.AnnotationMetadata && AnnotationMetadata.sceneLabel(code)) || code;
}

function fillSelect(select, items, { allText, extra = [] } = {}) {
  if (!select) return;
  const current = select.value;
  select.replaceChildren(element("option", { value: "", text: allText }));
  extra.forEach((item) => select.appendChild(element("option", { value: item.value, text: item.text })));
  items.forEach((item) => select.appendChild(element("option", {
    value: item.value,
    text: item.text,
  })));
  if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
}

function readNamedFilters(prefix) {
  const fields = {
    source_scene: $(`${prefix}SourceScene`),
    source_confidence: $(`${prefix}SourceConfidence`),
    batch_code: $(`${prefix}Batch`),
    review_status: $(`${prefix}ReviewStatus`),
    prediction_scene: $(`${prefix}PredictionScene`),
    human_scene: $(`${prefix}HumanScene`),
  };
  const extra = {};
  for (const [key, node] of Object.entries(fields)) {
    if (node && node.value) extra[key] = node.value;
  }
  return extra;
}

function writeNamedFilters(prefix, filters) {
  const map = {
    SourceScene: filters.source_scene || "",
    SourceConfidence: filters.source_confidence || "",
    Batch: filters.batch_code || "",
    ReviewStatus: filters.review_status || "",
    PredictionScene: filters.prediction_scene || "",
    HumanScene: filters.human_scene || "",
  };
  for (const [suffix, value] of Object.entries(map)) {
    const node = $(prefix + suffix);
    if (node) node.value = value;
  }
}

function syncMetadataFilters(fromPrefix) {
  const filters = readNamedFilters(fromPrefix);
  state.metadataFilters = filters;
  writeNamedFilters(fromPrefix === "corpus" ? "overview" : "corpus", filters);
  return filters;
}

async function loadMetadataFacets() {
  try {
    const data = await api.get("/api/admin/metadata/facets");
    const scenes = listValue(data, ["scenes"]);
    const batches = listValue(data, ["batches"]);
    state.sceneLabels = Object.fromEntries(scenes.map((scene) => [scene.code, sceneDisplayName(scene)]));
    const sceneOptions = scenes.map((scene) => ({ value: scene.code, text: sceneDisplayName(scene) }));
    const batchOptions = batches.map((batch) => ({ value: batch.batch_code, text: batch.name ? `${batch.batch_code} · ${batch.name}` : batch.batch_code }));
    fillSelect($("corpusSourceScene"), sceneOptions, { allText: "All source scenes" });
    fillSelect($("overviewSourceScene"), sceneOptions, { allText: "All source scenes" });
    fillSelect($("corpusBatch"), batchOptions, { allText: "All batches" });
    fillSelect($("overviewBatch"), batchOptions, { allText: "All batches" });
    fillSelect($("corpusPredictionScene"), sceneOptions, { allText: "All model classifications", extra: [{ value: "unknown", text: "No model classification" }] });
    fillSelect($("overviewPredictionScene"), sceneOptions, { allText: "All model classifications", extra: [{ value: "unknown", text: "No model classification" }] });
    fillSelect($("corpusHumanScene"), sceneOptions, { allText: "All human scenes", extra: [{ value: "unknown", text: "No human scene" }] });
    fillSelect($("overviewHumanScene"), sceneOptions, { allText: "All human scenes", extra: [{ value: "unknown", text: "No human scene" }] });
    const grid = $("scopeSceneGrid");
    if (grid && !grid.childElementCount) {
      scenes.forEach((scene) => {
        const label = element("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = scene.code;
        label.appendChild(box);
        label.appendChild(document.createTextNode(sceneDisplayName(scene)));
        grid.appendChild(label);
      });
    }
    const reviewGrid = $("adminReviewScenes");
    if (reviewGrid && !reviewGrid.childElementCount) {
      scenes.forEach((scene) => {
        const label = element("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = scene.code;
        label.appendChild(box);
        label.appendChild(document.createTextNode(sceneDisplayName(scene)));
        reviewGrid.appendChild(label);
      });
    }
    state.scopeCatalogFailed = false;
    tryBindCurrentScopeEditor();
  } catch (_) {
    // Facets are progressive enhancement, but a missing catalog must not
    // leave the claim-scope editor editable with an empty scene list.
    state.scopeCatalogFailed = true;
    tryBindCurrentScopeEditor();
  }
}

async function loadAnnotators() {
  const items = [];
  let cursor = "";
  do {
    const params = new URLSearchParams({ limit: "100" });
    if (cursor) params.set("cursor", cursor);
    const data = await api.get(`/api/admin/annotators?${params.toString()}`);
    items.push(...listValue(data, ["items", "annotators", "results"]));
    cursor = String(pick(data, ["next_cursor", "cursor"], "") || "");
  } while (cursor && items.length < 10000);
  state.annotators = items.map(normaliseAnnotator);
  renderAnnotatorDirectory();
  renderQualityAnnotatorOptions();
  if (state.selectedAnnotatorId && !state.annotators.some((item) => item.id === state.selectedAnnotatorId)) {
    state.selectedAnnotatorId = "";
    syncUrl();
  }
}

function renderQualityAnnotatorOptions() {
  const select = $("qualityAnnotator");
  if (!select) return;
  clear(select);
  select.appendChild(element("option", { value: "", text: "All annotators" }));
  for (const item of state.annotators) {
    select.appendChild(element("option", {
      value: item.id,
      text: item.status === "active" ? item.username : `${item.username} (deactivated)`,
    }));
  }
  select.value = state.qualityAnnotatorId;
}

function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
  return parts.slice(0, 2).map((part) => Array.from(part)[0] || "").join("").toUpperCase() || "?";
}

function renderAnnotatorDirectory() {
  const query = $("sidebarAnnotatorSearch").value.trim().toLocaleLowerCase();
  const items = state.annotators.filter((item) => item.username.toLocaleLowerCase().includes(query));
  text($("annotatorCount"), state.annotators.length);
  clear($("sidebarAnnotatorList"));
  if (!items.length) {
    $("sidebarAnnotatorList").appendChild(element("div", { className: "sidebar-placeholder", text: query ? "No matching annotators." : "No annotators yet." }));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const avatar = element("span", { className: `avatar${item.status !== "active" ? " is-deactivated" : ""}`, text: initials(item.username), attrs: { "aria-hidden": "true" } });
    const copy = element("span", { className: "annotator-list-copy" }, [
      element("strong", { text: item.username }),
      element("small", { text: item.status !== "active" ? "Deactivated" : `${formatInteger(item.currentCount)} current · ${formatRelative(item.lastActiveAt)}` }),
    ]);
    const status = element("span", { className: `online-dot${item.online ? " is-online" : ""}`, attrs: { "aria-hidden": "true" } });
    const button = element("button", {
      className: `annotator-list-button${item.id === state.selectedAnnotatorId ? " is-selected" : ""}`,
      type: "button",
      dataset: { annotatorId: item.id },
      attrs: { "aria-label": `View ${item.username}` },
    }, [avatar, copy, status]);
    fragment.appendChild(button);
  }
  $("sidebarAnnotatorList").appendChild(fragment);
}

async function selectAnnotator(id) {
  if (!id) return;
  state.selectedAnnotatorId = id;
  state.selectedTaskIds.clear();
  renderAnnotatorDirectory();
  await navigate("annotators", { focus: false });
}

function overviewStats(data) {
  return pick(data, ["stats", "summary", "corpus"], data || {});
}

async function loadOverview(epoch = state.requestEpoch) {
  writeNamedFilters("overview", state.metadataFilters);
  const metadata = readNamedFilters("overview");
  state.metadataFilters = metadata;
  writeNamedFilters("corpus", metadata);
  const [overviewResult, seriesResult] = await Promise.allSettled([
    api.get(apiUrl("/api/admin/overview", metadata)),
    api.get(apiUrl("/api/admin/timeseries")),
  ]);
  if (epoch !== state.requestEpoch) return;
  if (overviewResult.status === "fulfilled") {
    state.overview = overviewResult.value;
    renderOverview(state.overview);
  }
  if (seriesResult.status === "fulfilled") {
    state.timeseries = normaliseTimeseries(seriesResult.value);
    drawActivityChart(state.timeseries);
  } else {
    state.timeseries = [];
    drawActivityChart([]);
  }
  const failure = overviewResult.status === "rejected" ? overviewResult.reason : seriesResult.status === "rejected" ? seriesResult.reason : null;
  if (failure && failure.status !== 401) showStatus(`Some overview data is unavailable: ${failure.message}`);
  else hideStatus();
}

function renderOverview(data) {
  const stats = overviewStats(data);
  const total = numberValue(pick(stats, ["total_audio", "total", "total_tasks", "audio_count", "totals.total_audio_count"], 0));
  const totalDuration = numberValue(pick(stats, ["total_duration_seconds", "audio_duration_seconds", "duration_seconds", "totals.total_audio_duration_seconds"], 0));
  const annotated = numberValue(pick(stats, ["annotated", "annotated_count", "current_annotated", "totals.annotated_count"], 0));
  const annotatedDuration = numberValue(pick(stats, ["annotated_duration_seconds", "current_annotated_duration_seconds", "totals.annotated_duration_seconds"], 0));
  const pending = numberValue(pick(stats, ["pending_count", "totals.pending_count", "pending.total_count"], 0));
  const pendingAvailable = numberValue(pick(stats, ["pending_available", "available", "available_count", "pending.available_count"], 0));
  const skipped = numberValue(pick(stats, ["skipped", "skipped_count", "current_skipped", "totals.skipped_count"], 0));
  const skippedDuration = numberValue(pick(stats, ["skipped_duration_seconds", "current_skipped_duration_seconds", "totals.skipped_duration_seconds"], 0));
  const trainable = numberValue(pick(stats, ["trainable_segments", "trainable_segment_count", "segments.trainable_count"], 0));
  const trainableDuration = numberValue(pick(stats, ["trainable_duration_seconds", "usable_segment_duration_seconds", "segments.trainable_duration_seconds"], 0));
  const active = numberValue(pick(stats, ["active_annotators", "active_annotators_7d", "active_users", "activity.active_annotators"], 0));
  const throughput = numberValue(pick(stats, ["daily_throughput_7d", "activity.daily_throughput_7d", "average_daily_throughput", "daily_average_7d"], 0));

  text($("kpiTotalAudio"), formatInteger(total));
  text($("kpiTotalDuration"), formatDuration(totalDuration));
  text($("kpiAnnotated"), formatInteger(annotated));
  text($("kpiAnnotatedDuration"), formatDuration(annotatedDuration));
  text($("kpiPending"), formatInteger(pending));
  text($("kpiPendingAvailable"), formatInteger(pendingAvailable));
  text($("kpiSkipped"), formatInteger(skipped));
  text($("kpiSkippedDuration"), formatDuration(skippedDuration));
  text($("kpiTrainable"), formatInteger(trainable));
  text($("kpiTrainableDuration"), formatDuration(trainableDuration));
  text($("kpiActiveAnnotators"), formatInteger(active));
  text($("kpiDailyThroughput"), throughput.toFixed(throughput % 1 ? 1 : 0));
  const updatedAt = pick(data, ["updated_at", "generated_at"], new Date().toISOString());
  text($("overviewUpdatedAt"), formatRelative(updatedAt));
  $("overviewUpdatedAt").dateTime = String(updatedAt);

  const queue = {
    available: pendingAvailable,
    assigned: numberValue(pick(stats, ["pending_assigned", "assigned", "assigned_count", "pending.assigned_count"], 0)),
    reserved: numberValue(pick(stats, ["pending_reserved", "reserved", "reserved_count", "pending.reserved_count"], 0)),
    blocked: numberValue(pick(stats, ["pending_ineligible", "ineligible", "blocked_count", "pending.ineligible_count"], 0)),
  };
  drawQueueChart(queue, pending);

  const resolved = annotated + skipped;
  const completion = numberValue(pick(stats, ["completion_rate", "resolved_rate"], total ? resolved / total : 0));
  const completionPercent = Math.max(0, Math.min(100, Math.abs(completion) <= 1 ? completion * 100 : completion));
  text($("completionRate"), `${completionPercent.toFixed(1)}%`);
  $("completionProgress").setAttribute("aria-valuenow", String(Math.round(completionPercent)));
  $("completionProgress").dataset.level = String(Math.round(completionPercent / 5) * 5);
  text($("oldestPending"), formatRelative(pick(stats, ["oldest_pending_at", "queue.oldest_pending_at", "pending.oldest_created_at"], null)));
  text($("staleAssignments"), formatInteger(pick(stats, ["stale_assignments", "queue_health.stale_assignments", "long_held_assignments"], 0)));
  text($("estimatedFinish"), formatDateTime(pick(stats, ["estimated_finish_at", "estimated_completion_date"], null), true));

  const distributions = pick(data, ["distributions"], {});
  renderDistribution($("categoryDistribution"), pick(distributions, ["categories", "category"], pick(data, ["categories"], [])));
  renderDistribution($("skipDistribution"), pick(distributions, ["skip_reasons", "skipReasons"], pick(data, ["skip_reasons"], [])));
  renderMetadataGroups(data);
}

function groupRows(items, labelFn) {
  return (items || []).map((item) => ({
    key: item.scene_code || item.batch_code || item.confidence || item.status || item.label,
    label: labelFn(item),
    tasks: numberValue(item.task_count),
    duration: numberValue(item.duration_seconds),
    overlapping: boolValue(item.overlapping, false),
  }));
}

function renderGroupTable(container, rows, emptyText) {
  clear(container);
  if (!rows.length) {
    container.appendChild(element("div", { className: "empty-inline", text: emptyText }));
    return;
  }
  const table = element("table", { className: "metadata-group-table" });
  table.appendChild(element("thead", {}, element("tr", {}, [
    element("th", { text: "Group" }),
    element("th", { className: "num", text: "Tasks" }),
    element("th", { className: "num", text: "Source audio duration" }),
  ])));
  const body = element("tbody");
  for (const row of rows) {
    body.appendChild(element("tr", {}, [
      element("td", { text: row.label }),
      element("td", { className: "num", text: formatInteger(row.tasks) }),
      element("td", { className: "num", text: formatDuration(row.duration, false) }),
    ]));
  }
  table.appendChild(body);
  container.appendChild(table);
}

function renderMetadataGroups(data) {
  renderGroupTable(
    $("sourceSceneGroups"),
    groupRows(listValue(data, ["source_scenes"]), (item) => item.label || sceneLabel(item.scene_code)),
    "No source scene groups.",
  );
  renderGroupTable(
    $("confidenceGroups"),
    groupRows(listValue(data, ["confidence_buckets"]), (item) => CONF_LABEL[item.confidence] || item.confidence || "Unknown"),
    "No source confidence groups.",
  );
  renderGroupTable(
    $("sourceBatchGroups"),
    groupRows(listValue(data, ["source_batches"]), (item) => item.batch_code || "Unknown batch"),
    "No source batch groups.",
  );
  renderGroupTable(
    $("reviewStatusGroups"),
    groupRows(listValue(data, ["review_statuses"]), (item) => REVIEW_STATUS_LABEL[item.status] || item.status),
    "No published review groups.",
  );
}

function renderMatchedStats(data) {
  const node = $("corpusMatchedStats");
  if (!node) return;
  if (!data) {
    node.hidden = true;
    node.replaceChildren();
    return;
  }
  const totals = pick(data, ["totals"], {});
  const tasks = numberValue(pick(totals, ["total_audio_count"], 0));
  const duration = numberValue(pick(totals, ["total_audio_duration_seconds"], 0));
  node.hidden = false;
  node.replaceChildren();
  node.append(
    element("strong", { text: `${formatInteger(tasks)} tasks` }),
    document.createTextNode(" · "),
    element("span", { text: `${formatDuration(duration, false)} source audio` }),
    document.createTextNode(" · Source scene/batch groups below may overlap; source confidence and published review are mutually exclusive."),
  );
}

function normaliseDistribution(raw) {
  if (Array.isArray(raw)) {
    return raw.map((item) => ({
      label: String(pick(item, ["label", "name", "category", "reason"], "Unknown")),
      value: numberValue(pick(item, ["value", "count", "tasks"], 0)),
    }));
  }
  if (raw && typeof raw === "object") return Object.entries(raw).map(([label, value]) => ({ label, value: numberValue(value) }));
  return [];
}

function renderDistribution(container, raw) {
  clear(container);
  const items = normaliseDistribution(raw).sort((a, b) => b.value - a.value).slice(0, 8);
  if (!items.length) {
    container.appendChild(element("div", { className: "empty-inline", text: "No distribution data yet." }));
    return;
  }
  const maximum = Math.max(...items.map((item) => item.value), 1);
  for (const item of items) {
    const fill = element("i");
    const level = Math.max(5, Math.min(100, Math.round(item.value / maximum * 20) * 5));
    const track = element("span", { className: "bar-track", attrs: { "aria-hidden": "true" } }, fill);
    container.appendChild(element("div", { className: "bar-row", title: `${item.label}: ${formatInteger(item.value)}`, dataset: { level } }, [
      element("span", { className: "bar-label", text: item.label }),
      track,
      element("span", { className: "bar-value", text: formatInteger(item.value) }),
    ]));
  }
}

function normaliseTimeseries(data) {
  const points = listValue(data, ["items", "points", "series", "timeseries", "buckets"]).map((item) => ({
    date: String(pick(item, ["date", "day", "bucket", "bucket_start", "period"], "")),
    annotated: numberValue(pick(item, ["annotated", "completed", "annotated_count"], 0)),
    skipped: numberValue(pick(item, ["skipped", "skipped_count"], 0)),
    revoked: numberValue(pick(item, ["revoked", "revoked_count", "admin_revoked"], 0)),
    duration: numberValue(pick(item, ["duration_seconds", "annotated_duration_seconds", "audio_duration_seconds", "training_duration_seconds"], 0)),
  })).filter((item) => item.date);
  if (!state.dateFrom || !state.dateTo || state.range === "all") return points;
  const bucket = String(pick(data, ["bucket"], state.range === "90d" ? "week" : "day"));
  let start = state.dateFrom;
  const step = bucket === "week" ? 7 : 1;
  if (bucket === "week") {
    const [year, month, day] = start.split("-").map(Number);
    const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
    start = shiftDate(start, -(weekday === 0 ? 6 : weekday - 1));
  }
  const byDate = new Map(points.map((point) => [point.date.slice(0, 10), point]));
  const filled = [];
  for (let cursor = start; cursor <= state.dateTo; cursor = shiftDate(cursor, step)) {
    filled.push(byDate.get(cursor) || {
      date: `${cursor}T00:00:00`, annotated: 0, skipped: 0,
      revoked: 0, duration: 0,
    });
  }
  return filled;
}

function canvasContext(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width || Number(canvas.getAttribute("width")) || 300);
  const height = Math.max(1, rect.height || Number(canvas.getAttribute("height")) || 180);
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function drawSeriesChart(canvas, points, definitions, targetState = null) {
  const { context, width, height } = canvasContext(canvas);
  const padding = { top: 15, right: 13, bottom: 29, left: 39 };
  const chartWidth = Math.max(1, width - padding.left - padding.right);
  const chartHeight = Math.max(1, height - padding.top - padding.bottom);
  const maximum = Math.max(1, ...points.flatMap((point) => definitions.map((definition) => numberValue(point[definition.key]))));
  const niceMaximum = maximum <= 5 ? 5 : Math.ceil(maximum / 10) * 10;

  context.strokeStyle = "#e8e9ea";
  context.lineWidth = 1;
  context.fillStyle = "#8b8f95";
  context.font = "9px ui-sans-serif, sans-serif";
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let line = 0; line <= 4; line += 1) {
    const y = padding.top + chartHeight * line / 4;
    context.beginPath();
    context.moveTo(padding.left, y + .5);
    context.lineTo(width - padding.right, y + .5);
    context.stroke();
    const label = Math.round(niceMaximum * (1 - line / 4));
    context.fillText(String(label), padding.left - 7, y);
  }

  const xPositions = points.map((_, index) => padding.left + (points.length === 1 ? chartWidth / 2 : index * chartWidth / (points.length - 1)));
  for (const definition of definitions) {
    context.beginPath();
    context.strokeStyle = definition.color;
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.lineCap = "round";
    points.forEach((point, index) => {
      const x = xPositions[index];
      const y = padding.top + chartHeight - numberValue(point[definition.key]) / niceMaximum * chartHeight;
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
    if (points.length <= 45) {
      context.fillStyle = definition.color;
      points.forEach((point, index) => {
        const x = xPositions[index];
        const y = padding.top + chartHeight - numberValue(point[definition.key]) / niceMaximum * chartHeight;
        context.beginPath();
        context.arc(x, y, 2.25, 0, Math.PI * 2);
        context.fill();
      });
    }
  }

  context.fillStyle = "#8b8f95";
  context.textAlign = "center";
  context.textBaseline = "top";
  const labelCount = Math.min(points.length, width < 500 ? 4 : 6);
  const seen = new Set();
  for (let index = 0; index < labelCount; index += 1) {
    const pointIndex = labelCount === 1 ? 0 : Math.round(index * (points.length - 1) / (labelCount - 1));
    if (seen.has(pointIndex)) continue;
    seen.add(pointIndex);
    const raw = points[pointIndex]?.date;
    const parsed = raw ? new Date(raw) : null;
    const label = parsed && !Number.isNaN(parsed.getTime())
      ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: state.timezone }).format(parsed)
      : String(raw || "");
    context.fillText(label, xPositions[pointIndex], height - 18);
  }
  if (targetState) {
    targetState.points = points;
    targetState.xPositions = xPositions;
    targetState.padding = padding;
  }
}

function drawActivityChart(points) {
  $("activityChartEmpty").hidden = points.length > 0;
  drawSeriesChart($("activityChart"), points, [
    { key: "annotated", color: "#10a37f" },
    { key: "skipped", color: "#98a1ad" },
    { key: "revoked", color: "#d66d75" },
  ], charts.activity);
  const totals = points.reduce((sum, point) => ({ annotated: sum.annotated + point.annotated, skipped: sum.skipped + point.skipped, revoked: sum.revoked + point.revoked }), { annotated: 0, skipped: 0, revoked: 0 });
  $("activityChart").setAttribute("aria-label", `Daily annotation activity. ${formatInteger(totals.annotated)} annotated, ${formatInteger(totals.skipped)} skipped, and ${formatInteger(totals.revoked)} revoked in the selected period.`);
}

function drawQueueChart(queue, pendingTotal) {
  const canvas = $("queueChart");
  const { context, width, height } = canvasContext(canvas);
  const values = [
    { label: "Available", value: queue.available, color: "#10a37f", className: "queue-available" },
    { label: "Assigned", value: queue.assigned, color: "#5b72e7", className: "queue-assigned" },
    { label: "Reserved", value: queue.reserved, color: "#d49a38", className: "queue-reserved" },
    { label: "Ineligible", value: queue.blocked, color: "#9da1a6", className: "queue-ineligible" },
  ];
  const sum = values.reduce((total, item) => total + item.value, 0);
  const centerX = width / 2;
  const centerY = height / 2;
  const radius = Math.min(width, height) * .38;
  context.lineWidth = Math.max(12, radius * .22);
  context.lineCap = "butt";
  if (!sum) {
    context.strokeStyle = "#e8eaea";
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.stroke();
  } else {
    let start = -Math.PI / 2;
    for (const item of values) {
      if (!item.value) continue;
      const end = start + item.value / sum * Math.PI * 2;
      context.strokeStyle = item.color;
      context.beginPath();
      context.arc(centerX, centerY, radius, start, end);
      context.stroke();
      start = end;
    }
  }
  text($("queueTotal"), formatInteger(pendingTotal || sum));
  clear($("queueLegend"));
  for (const item of values) {
    const row = element("div");
    const swatch = element("i", { className: item.className, attrs: { "aria-hidden": "true" } });
    row.append(swatch, element("dt", { text: item.label }), element("dd", { text: formatInteger(item.value) }));
    $("queueLegend").appendChild(row);
  }
  canvas.setAttribute("aria-label", `Pending queue: ${values.map((item) => `${item.label} ${item.value}`).join(", ")}.`);
}

function normaliseTask(raw) {
  const sourceStatus = String(pick(raw, ["status", "task_status", "target_status", "current_status"], "unknown"));
  const poolState = String(pick(raw, ["pool_state"], ""));
  const status = sourceStatus === "pending" && poolState === "assigned" ? "assigned" : sourceStatus;
  const taskId = String(pick(raw, ["task_id", "id"], ""));
  const expectedVersionId = String(pick(raw, ["expected_version_id", "current_version_id", "version_id", "published_version_id"], ""));
  const currentEffective = boolValue(
    pick(raw, ["current_effective", "is_current", "is_current_version"], ["annotated", "skipped", "published"].includes(status)),
  );
  return {
    ...raw,
    taskId,
    expectedVersionId,
    filename: String(pick(raw, ["filename", "name", "rel_path", "audio_name"], taskId || "Unknown task")),
    folder: String(pick(raw, ["folder", "source_folder", "rel_path"], "")),
    category: String(pick(raw, ["category"], "—")),
    status,
    durationSeconds: numberValue(pick(raw, ["duration_seconds", "audio_duration_seconds", "duration"], 0)),
    segmentCount: numberValue(pick(raw, ["segment_count", "segments_count", "segments.length"], 0)),
    submittedAt: pick(raw, ["submitted_at", "completed_at", "created_at", "updated_at", "last_activity_at"], null),
    turnaroundSeconds: pick(raw, ["turnaround_seconds", "wall_clock_seconds"], null),
    annotatorId: String(pick(raw, ["annotator_id", "submitted_by_user_id", "user_id", "annotator.id", "current_submitter.id", "assignment.annotator_id", "annotatorId"], "")),
    annotatorName: String(pick(raw, ["annotator_name", "username", "submitted_by", "annotator.username", "current_submitter.username", "assignment.username", "annotatorName"], "—")),
    revocable: boolValue(pick(raw, ["revocable", "can_revoke"], currentEffective && Boolean(expectedVersionId))),
  };
}

function normaliseAnnotatorDetail(data) {
  const source = pick(data, ["annotator", "user"], data || {});
  const base = normaliseAnnotator({
    ...source,
    last_active_at: pick(data, ["activity.last_seen_at", "history.last_completed_at"], null),
    online: pick(data, ["activity.online"], false),
    assignment: pick(data, ["assignment"], null),
  });
  const current = pick(data, ["current"], {});
  const history = pick(data, ["history"], {});
  const currentCount = numberValue(pick(current, ["annotated_count"], 0)) + numberValue(pick(current, ["skipped_count"], 0));
  const completed = numberValue(pick(history, ["completed_count"], 0));
  const revoked = numberValue(pick(history, ["revoked_count"], 0));
  const stats = {
    ...pick(data, ["stats", "summary"], {}),
    current_contributions: currentCount,
    current_duration_seconds: pick(current, ["trainable_duration_seconds", "annotated_duration_seconds", "duration_seconds"], 0),
    historical_submissions: completed,
    active_days: pick(history, ["active_days"], 0),
    revoked,
    revoke_rate: completed ? revoked / completed : 0,
    annotated: pick(history, ["annotated_count"], pick(current, ["annotated_count"], 0)),
    skipped: pick(history, ["skipped_count"], pick(current, ["skipped_count"], 0)),
    average_turnaround_seconds: pick(data, ["efficiency.turnaround_average_seconds"], null),
    median_turnaround_seconds: pick(data, ["efficiency.turnaround_median_seconds"], null),
    p90_turnaround_seconds: pick(data, ["efficiency.turnaround_p90_seconds"], null),
  };
  return { ...data, annotator: base, stats };
}

function scopeCatalogReady() {
  return Boolean($("scopeSceneGrid")?.querySelector("input[type=checkbox]"));
}

function isCurrentAnnotatorLoad(id, epoch, token) {
  return epoch === state.requestEpoch && id === state.selectedAnnotatorId && token === state.scopeLoadToken;
}

function resetScopeEditorFields() {
  formError($("scopeError"));
  if ($("scopeMode")) $("scopeMode").value = "all";
  if ($("scopeReason")) $("scopeReason").value = "";
  for (const box of document.querySelectorAll("#scopeSceneGrid input[type=checkbox]")) box.checked = false;
}

function setScopeEditorUnavailable(message, { hideFields = true } = {}) {
  const form = $("sceneScopeForm");
  const fields = $("scopeEditorFields");
  const status = $("scopeEditorStatus");
  if (fields) {
    fields.disabled = true;
    fields.hidden = hideFields;
  }
  if (form) {
    form.dataset.scopeReady = "0";
    form.setAttribute("aria-busy", "true");
    delete form.dataset.annotatorId;
    delete form.dataset.revision;
    delete form.dataset.loadToken;
  }
  if (status) {
    status.hidden = !message;
    status.textContent = message || "";
  }
}

function applySceneScopeFields(scope) {
  if (!$("scopeMode")) return;
  $("scopeMode").value = pick(scope, ["mode"], "all");
  $("scopeReason").value = "";
  const allowed = new Set(listValue(scope, ["scene_codes"]));
  if (boolValue(pick(scope, ["allow_unknown"], false))) allowed.add("spoken_languages");
  for (const box of document.querySelectorAll("#scopeSceneGrid input[type=checkbox]")) {
    box.checked = allowed.has(box.value);
  }
}

function bindScopeEditor(id, token, data) {
  const form = $("sceneScopeForm");
  const fields = $("scopeEditorFields");
  const status = $("scopeEditorStatus");
  const scope = pick(data, ["scene_scope", "scope"], {});
  applySceneScopeFields(scope);
  if (!form || !fields) return;
  if (!scopeCatalogReady()) {
    setScopeEditorUnavailable(state.scopeCatalogFailed ? "Could not load claim scope." : "Loading claim scope…");
    return;
  }
  fields.hidden = false;
  fields.disabled = false;
  form.dataset.scopeReady = "1";
  form.dataset.annotatorId = id;
  form.dataset.loadToken = String(token);
  form.dataset.revision = String(pick(scope, ["revision"], 0));
  form.setAttribute("aria-busy", "false");
  if (status) {
    status.hidden = true;
    status.textContent = "";
  }
}

function tryBindCurrentScopeEditor() {
  const bound = state.scopeEditorBound;
  if (!bound || bound.token !== state.scopeLoadToken || bound.id !== state.selectedAnnotatorId) return;
  if (!state.annotatorDetail) return;
  bindScopeEditor(bound.id, bound.token, state.annotatorDetail);
}

async function loadAnnotator(id, epoch = state.requestEpoch) {
  const token = ++state.scopeLoadToken;
  state.scopeEditorBound = null;
  $("annotatorEmptyState").hidden = true;
  $("annotatorDetail").hidden = false;
  setScopeEditorUnavailable("Loading claim scope…");
  resetScopeEditorFields();
  setTaskTableState("annotator", "Loading annotations…");
  state.selectedTaskIds.clear();
  const encoded = encodeURIComponent(id);
  const [detailResult, tasksResult, seriesResult] = await Promise.allSettled([
    api.get(apiUrl(`/api/admin/annotators/${encoded}`)),
    api.get(apiUrl(`/api/admin/annotators/${encoded}/annotations`, { limit: 50, lifecycle: "published" })),
    api.get(apiUrl("/api/admin/timeseries", { annotator_id: id })),
  ]);
  if (!isCurrentAnnotatorLoad(id, epoch, token)) return;
  if (detailResult.status === "fulfilled") {
    state.annotatorDetail = normaliseAnnotatorDetail(detailResult.value);
    if (seriesResult.status === "fulfilled") state.annotatorDetail.timeseries = normaliseTimeseries(seriesResult.value);
    renderAnnotatorDetail(state.annotatorDetail);
    state.scopeEditorBound = { id, token };
    bindScopeEditor(id, token, state.annotatorDetail);
  } else {
    setScopeEditorUnavailable("Could not load claim scope.");
  }
  if (tasksResult.status === "fulfilled") {
    state.annotatorTasks = listValue(tasksResult.value, ["items", "annotations", "tasks", "results"]).map(normaliseTask);
    state.annotatorTaskCursor = pick(tasksResult.value, ["next_cursor", "cursor"], null);
    renderAnnotatorTasks();
  } else setTaskTableState("annotator", tasksResult.reason.message || "Could not load annotations.");
  const failure = detailResult.status === "rejected" ? detailResult.reason : tasksResult.status === "rejected" ? tasksResult.reason : null;
  if (failure && failure.status !== 401) showStatus(failure.message);
  else hideStatus();
}

function renderAnnotatorDetail(data) {
  const item = data.annotator;
  const stats = data.stats || {};
  text($("annotatorAvatar"), initials(item.username));
  $("annotatorAvatar").classList.toggle("is-deactivated", item.status !== "active");
  text($("annotatorName"), item.username);
  text($("annotatorStatus"), item.status);
  $("annotatorStatus").className = `badge${item.status === "active" ? "" : " badge-neutral"}`;
  text($("annotatorMeta"), `Last active ${formatRelative(item.lastActiveAt)} · ${pick(data, ["assignment"], null) ? "Holding a task" : "No active assignment"}`);
  $("deactivateAnnotatorButton").disabled = item.status !== "active";
  text($("annotatorCurrent"), formatInteger(pick(stats, ["current_contributions", "current_count", "current_effective_count"], 0)));
  text($("annotatorCurrentDuration"), `${formatDuration(pick(stats, ["current_duration_seconds", "effective_duration_seconds"], 0))} usable audio`);
  text($("annotatorSubmitted"), formatInteger(pick(stats, ["historical_submissions", "submitted", "completed_actions"], 0)));
  text($("annotatorActiveDays"), formatInteger(pick(stats, ["active_days"], 0)));
  text($("annotatorMedian"), formatSecondsAsTurnaround(pick(stats, ["median_turnaround_seconds", "turnaround_median_seconds"], null)));
  text($("annotatorP90"), formatSecondsAsTurnaround(pick(stats, ["p90_turnaround_seconds", "turnaround_p90_seconds"], null)));
  text($("annotatorRevokeRate"), formatPercent(pick(stats, ["revoke_rate", "admin_revoke_rate"], 0), 1));
  text($("annotatorRevoked"), formatInteger(pick(stats, ["revoked", "admin_revoked"], 0)));

  const series = normaliseTimeseries({ items: listValue(data, ["timeseries", "series", "daily"]) });
  charts.annotator.points = series;
  $("annotatorChartEmpty").hidden = series.length > 0;
  drawSeriesChart($("annotatorChart"), series, [{ key: "annotated", color: "#10a37f" }, { key: "skipped", color: "#98a1ad" }]);
  const distributions = pick(data, ["distributions"], {});
  const labelMix = pick(distributions, ["labels", "statuses", "label_mix"], {
    Annotated: pick(stats, ["annotated"], 0),
    Skipped: pick(stats, ["skipped"], 0),
    Revoked: pick(stats, ["revoked"], 0),
  });
  renderDistribution($("annotatorLabelMix"), labelMix);
}

function statusBadge(status) {
  const normal = String(status || "unknown").toLowerCase();
  let className = "badge badge-neutral";
  if (["annotated", "published", "active", "success", "completed"].includes(normal)) className = "badge";
  else if (["pending", "assigned", "processing"].includes(normal)) className = "badge badge-warning";
  else if (["revoked", "failed", "error", "deactivated"].includes(normal)) className = "badge badge-danger";
  return element("span", { className, text: normal.replaceAll("_", " ") });
}

function taskAudioCell(item) {
  return element("td", { className: "audio-cell" }, [element("strong", { text: item.filename, title: item.filename }), element("small", { text: item.folder || item.category || item.taskId })]);
}

function setTaskTableState(kind, message = "") {
  const node = kind === "annotator" ? $("annotatorTaskState") : kind === "corpus" ? $("corpusTaskState") : $("qualityTaskState");
  node.hidden = !message;
  text(node, message);
}

function renderAnnotatorTasks() {
  const body = $("annotatorTaskBody");
  clear(body);
  setTaskTableState("annotator", state.annotatorTasks.length ? "" : "No current annotations match this period.");
  for (const item of state.annotatorTasks) {
    const row = element("tr");
    const checkbox = element("input", {
      type: "checkbox",
      dataset: { selectTask: item.taskId },
      attrs: { "aria-label": `Select ${item.filename}` },
    });
    checkbox.checked = state.selectedTaskIds.has(item.taskId);
    checkbox.disabled = !item.revocable;
    row.appendChild(element("td", { className: "checkbox-cell" }, checkbox));
    row.appendChild(taskAudioCell(item));
    row.appendChild(element("td", {}, statusBadge(item.status)));
    row.appendChild(element("td", { className: "mono-cell", text: formatDuration(item.durationSeconds) }));
    row.appendChild(element("td", { className: "mono-cell", text: formatInteger(item.segmentCount) }));
    row.appendChild(element("td", { text: formatDateTime(item.submittedAt) }));
    row.appendChild(element("td", { className: "mono-cell", text: formatSecondsAsTurnaround(item.turnaroundSeconds) }));
    const actionCell = element("td", { className: "actions-cell" });
    actionCell.appendChild(element("button", { className: "table-action", type: "button", text: "View", dataset: { taskAction: "view", taskId: item.taskId } }));
    if (item.revocable) actionCell.appendChild(element("button", { className: "table-action", type: "button", text: "Revoke", dataset: { taskAction: "revoke", taskId: item.taskId } }));
    row.appendChild(actionCell);
    body.appendChild(row);
  }
  $("loadMoreAnnotatorTasks").hidden = !state.annotatorTaskCursor;
  updateTaskSelection();
  text($("annotationTableSummary"), `${formatInteger(state.annotatorTasks.length)} current records loaded. Select records to revoke in one transaction.`);
}

function updateTaskSelection() {
  for (const id of Array.from(state.selectedTaskIds)) {
    if (!state.annotatorTasks.some((task) => task.taskId === id && task.revocable)) state.selectedTaskIds.delete(id);
  }
  const count = state.selectedTaskIds.size;
  text($("selectedTaskCount"), `${count} selected`);
  $("selectedTaskCount").hidden = !count;
  $("batchRevokeButton").disabled = !count;
  const eligible = state.annotatorTasks.filter((task) => task.revocable);
  $("selectAllTasks").checked = eligible.length > 0 && eligible.every((task) => state.selectedTaskIds.has(task.taskId));
  $("selectAllTasks").indeterminate = count > 0 && !$("selectAllTasks").checked;
}

async function loadMoreAnnotatorTasks() {
  if (!state.annotatorTaskCursor || !state.selectedAnnotatorId) return;
  setButtonBusy($("loadMoreAnnotatorTasks"), true, "Loading…");
  try {
    const encoded = encodeURIComponent(state.selectedAnnotatorId);
    const data = await api.get(apiUrl(`/api/admin/annotators/${encoded}/annotations`, { limit: 50, cursor: state.annotatorTaskCursor, lifecycle: "published" }));
    const incoming = listValue(data, ["items", "annotations", "tasks", "results"]).map(normaliseTask);
    state.annotatorTasks.push(...incoming);
    state.annotatorTaskCursor = pick(data, ["next_cursor", "cursor"], null);
    renderAnnotatorTasks();
  } catch (error) { toast(error.message, "error"); }
  finally { setButtonBusy($("loadMoreAnnotatorTasks"), false); }
}

async function loadCorpus(append = false, epoch = state.requestEpoch) {
  if (!append) {
    state.corpusTasks = [];
    state.corpusTaskCursor = null;
    setTaskTableState("corpus", "Loading tasks…");
    clear($("corpusTaskBody"));
  }
  if (!append) writeNamedFilters("corpus", state.metadataFilters);
  const metadata = readNamedFilters("corpus");
  state.metadataFilters = metadata;
  writeNamedFilters("overview", metadata);
  const extra = {
    limit: 50,
    cursor: append ? state.corpusTaskCursor : "",
    q: $("corpusSearch").value.trim(),
    status: $("corpusStatus").value,
    ...metadata,
  };
  try {
    const requests = [api.get(apiUrl("/api/admin/tasks", extra))];
    if (!append) requests.push(api.get(apiUrl("/api/admin/overview", {
      q: extra.q,
      status: extra.status,
      ...metadata,
    })));
    const [listResult, overviewResult] = await Promise.allSettled(requests);
    if (epoch !== state.requestEpoch) return;
    if (listResult.status !== "fulfilled") throw listResult.reason;
    const data = listResult.value;
    const incoming = listValue(data, ["items", "annotations", "tasks", "results"]).map(normaliseTask);
    state.corpusTasks = append ? state.corpusTasks.concat(incoming) : incoming;
    state.corpusTaskCursor = pick(data, ["next_cursor", "cursor"], null);
    renderCorpusTasks();
    if (!append) {
      const overviewData = overviewResult && overviewResult.status === "fulfilled"
        ? overviewResult.value
        : null;
      renderMatchedStats({
        totals: {
          total_audio_count: numberValue(pick(data, ["matched_count"], state.corpusTasks.length)),
          total_audio_duration_seconds: numberValue(pick(data, ["matched_duration_seconds"], 0)),
        },
      });
      if (overviewData) renderMetadataGroups(overviewData);
    }
    hideStatus();
  } catch (error) {
    if (error.status === 404) setTaskTableState("corpus", "Global task browsing is not available on this server yet. Use Annotators to inspect current records.");
    else setTaskTableState("corpus", error.message);
    if (error.status !== 401 && error.status !== 404) showStatus(error.message);
  }
}

function renderCorpusTasks() {
  const body = $("corpusTaskBody");
  clear(body);
  setTaskTableState("corpus", state.corpusTasks.length ? "" : "No tasks match these filters.");
  for (const item of state.corpusTasks) {
    const row = element("tr");
    row.appendChild(taskAudioCell(item));
    row.appendChild(element("td", { text: item.annotatorName }));
    row.appendChild(element("td", {}, statusBadge(item.status)));
    row.appendChild(element("td", { className: "mono-cell", text: formatDuration(item.durationSeconds) }));
    const sourceScenes = (item.source_scenes || item.sourceScenes || []).map(sceneLabel);
    row.appendChild(element("td", { text: sourceScenes.join(", ") || "Spoken languages" }));
    row.appendChild(element("td", { text: CONF_LABEL[item.source_confidence || item.sourceConfidence] || item.source_confidence || "Source confidence Unknown" }));
    const humanScenes = (item.human_scenes || item.humanScenes || []).map(sceneLabel);
    const reviewStatus = item.review_status || item.reviewStatus || "pending";
    const reviewText = (REVIEW_STATUS_LABEL[reviewStatus] || reviewStatus) + (humanScenes.length ? ` · ${humanScenes.join(", ")}` : "");
    row.appendChild(element("td", { text: reviewText }));
    row.appendChild(element("td", { text: item.prediction_label || item.predictionLabel || sceneLabel(item.prediction_scene) || "—" }));
    row.appendChild(element("td", { text: formatDateTime(item.submittedAt || item.updated_at) }));
    const actions = element("td", { className: "actions-cell" });
    actions.appendChild(element("button", { className: "table-action", text: "View", type: "button", dataset: { taskAction: "view-corpus", taskId: item.taskId } }));
    if (item.revocable) actions.appendChild(element("button", { className: "table-action", text: "Revoke", type: "button", dataset: { taskAction: "revoke-corpus", taskId: item.taskId } }));
    row.appendChild(actions);
    body.appendChild(row);
  }
  $("loadMoreCorpusTasks").hidden = !state.corpusTaskCursor;
}

async function loadQuality(epoch = state.requestEpoch) {
  setTaskTableState("quality", "Loading quality signals…");
  clear($("qualityTaskBody"));
  try {
    const data = await api.get(apiUrl("/api/admin/quality", {
      limit: 50,
      q: state.qualityQuery,
      annotator_id: state.qualityAnnotatorId,
      signal: state.qualitySignal,
    }));
    if (epoch !== state.requestEpoch) return;
    state.qualityItems = listValue(data, ["items", "signals", "tasks", "results"]);
    renderQuality(data);
    hideStatus();
  } catch (error) {
    const stats = overviewStats(state.overview || {});
    renderQuality({ stats, items: [] });
    if (error.status === 404) setTaskTableState("quality", "No dedicated quality queue is available yet. Summary signals will appear as the server computes them.");
    else if (error.status !== 401) setTaskTableState("quality", error.message);
  }
}

function renderQuality(data) {
  const stats = pick(data, ["stats", "summary"], data || {});
  text($("qualityFastCount"), formatInteger(pick(stats, ["unusually_fast", "fast_task_count"], 0)));
  text($("qualityStaleCount"), formatInteger(pick(stats, ["stale_assignments", "stale_assignment_count"], 0)));
  text($("qualityBadSegments"), formatInteger(pick(stats, ["bad_quality_segments", "excluded_segment_count"], 0)));
  text($("qualityBadShare"), `${formatPercent(pick(stats, ["bad_quality_share", "excluded_segment_rate"], 0), 1)} of submitted segments`);
  text($("qualityRevocations"), formatInteger(pick(stats, ["revocations", "revoked_count"], 0)));
  state.qualityItems = listValue(data, ["items", "signals", "tasks", "results"]);
  const body = $("qualityTaskBody");
  clear(body);
  const hasFilters = state.qualityQuery || state.qualityAnnotatorId || state.qualitySignal;
  setTaskTableState("quality", state.qualityItems.length ? "" : hasFilters ? "No quality signals match these filters." : "No quality signals in this period.");
  for (const raw of state.qualityItems) {
    const task = normaliseTask(raw);
    const row = element("tr");
    row.appendChild(taskAudioCell(task));
    row.appendChild(element("td", { text: task.annotatorName }));
    const signal = pick(raw, ["signal", "signal_type", "type"], "review");
    const signalValue = signal === "unusually_fast"
      ? `${formatSecondsAsTurnaround(pick(raw, ["elapsed_seconds"], null))} < ${formatSecondsAsTurnaround(pick(raw, ["threshold_seconds"], null))}`
      : signal === "stale_assignment"
        ? `Last active ${formatRelative(pick(raw, ["last_activity_at"], null))}`
        : pick(raw, ["value", "signal_value", "description"], "—");
    row.appendChild(element("td", {}, statusBadge(signal)));
    row.appendChild(element("td", { text: signalValue }));
    row.appendChild(element("td", { text: formatDateTime(task.submittedAt) }));
    row.appendChild(element("td", { className: "actions-cell" }, element("button", { className: "table-action", text: "View", type: "button", dataset: { taskAction: "view-quality", taskId: task.taskId } })));
    body.appendChild(row);
  }
}

async function loadAudit(append = false, epoch = state.requestEpoch) {
  if (!append) {
    state.auditItems = [];
    state.auditCursor = null;
    clear($("auditBody"));
    $("auditState").hidden = false;
    text($("auditState"), "Loading activity…");
  }
  const extra = {
    limit: 50,
    cursor: append ? state.auditCursor : "",
    q: $("auditSearch").value.trim(),
    action_type: $("auditAction").value,
  };
  try {
    const data = await api.get(apiUrl("/api/admin/audit", extra));
    if (epoch !== state.requestEpoch) return;
    const incoming = listValue(data, ["items", "actions", "audit", "results"]);
    state.auditItems = append ? state.auditItems.concat(incoming) : incoming;
    state.auditCursor = pick(data, ["next_cursor", "cursor"], null);
    renderAudit();
    hideStatus();
  } catch (error) {
    if (error.status !== 401) {
      text($("auditState"), error.status === 404 ? "The activity log endpoint is not available on this server yet." : error.message);
      $("auditState").hidden = false;
    }
  }
}

function renderAudit() {
  const body = $("auditBody");
  clear(body);
  $("auditState").hidden = state.auditItems.length > 0;
  if (!state.auditItems.length) text($("auditState"), "No admin activity matches these filters.");
  for (const item of state.auditItems) {
    const action = String(pick(item, ["action_type", "action", "event_type"], "unknown"));
    const actionRequest = pick(item, ["request"], {});
    const requestedItems = listValue(actionRequest, ["items", "task_ids"]);
    const target = pick(item, ["target", "username", "task_id", "annotator_id", "summary.target"], null)
      || pick(actionRequest, ["annotator_id", "task_id"], null)
      || (requestedItems.length ? `${formatInteger(requestedItems.length)} task${requestedItems.length === 1 ? "" : "s"}` : "—");
    const status = pick(item, ["status", "result"], "success");
    const row = element("tr");
    row.appendChild(element("td", { text: formatDateTime(pick(item, ["created_at", "completed_at", "timestamp"], null)) }));
    row.appendChild(element("td", {}, statusBadge(action)));
    row.appendChild(element("td", { className: "mono-cell", text: target, title: target }));
    row.appendChild(element("td", { className: "mono-cell", text: pick(item, ["key_id", "admin_key_id"], "—") }));
    row.appendChild(element("td", { text: pick(item, ["reason"], "—"), title: pick(item, ["reason"], "") }));
    row.appendChild(element("td", {}, statusBadge(status)));
    body.appendChild(row);
  }
  $("loadMoreAudit").hidden = !state.auditCursor;
}

async function showTask(taskId, sourceItem = null) {
  if (!taskId) return;
  state.currentTaskDetail = sourceItem ? { ...sourceItem } : null;
  clear($("taskDetail"));
  $("taskDetail").appendChild(element("div", { className: "table-state" }, [element("span", { className: "spinner spinner-small", attrs: { "aria-hidden": "true" } }), "Loading task…"]));
  $("revokeFromDetailButton").hidden = true;
  openDialog($("taskDialog"));
  try {
    const data = await api.get(`/api/admin/annotations/${encodeURIComponent(taskId)}`);
    const raw = pick(data, ["annotation", "task"], data);
    const task = normaliseTask({ ...sourceItem, ...raw });
    state.currentTaskDetail = task;
    renderTaskDetail(data, task);
  } catch (error) {
    clear($("taskDetail"));
    $("taskDetail").appendChild(element("div", { className: "table-state", text: error.message }));
  }
}

function renderTaskDetail(data, task) {
  text($("taskDialogTitle"), task.filename);
  clear($("taskDetail"));
  const summary = element("dl", { className: "detail-summary" });
  const values = [
    ["Status", task.status],
    ["Annotator", task.annotatorName],
    ["Audio duration", formatDuration(task.durationSeconds, false)],
    ["Submitted", formatDateTime(task.submittedAt)],
    ["Folder", task.folder || "—"],
    ["Category", task.category || "—"],
  ];
  for (const [label, value] of values) summary.appendChild(element("div", {}, [element("dt", { text: label }), element("dd", { text: value })]));
  $("taskDetail").appendChild(summary);

  if (task.taskId) {
    const audio = element("audio", { className: "detail-audio", attrs: { controls: "", preload: "metadata", src: `/api/admin/audio/${encodeURIComponent(task.taskId)}` } });
    $("taskDetail").appendChild(audio);
  }

  const segments = listValue(data, ["segments", "annotation.segments", "task.segments"]);
  if (segments.length) {
    const list = element("div", { className: "segment-list", attrs: { "aria-label": "Annotation segments" } });
    segments.forEach((segment, index) => {
      list.appendChild(element("div", { className: "segment-row" }, [
        element("span", { text: `#${index + 1}` }),
        element("span", { text: `${numberValue(pick(segment, ["start", "start_seconds", "start_s"], 0)).toFixed(2)}–${numberValue(pick(segment, ["end", "end_seconds", "end_s"], 0)).toFixed(2)}` }),
        element("p", { text: pick(segment, ["text", "transcript", "asr_text"], "") || "(empty)" }),
        element("span", { text: boolValue(pick(segment, ["exclude_from_training", "bad_quality"], false)) ? "Bad quality" : "Included" }),
      ]));
    });
    $("taskDetail").appendChild(list);
  } else {
    $("taskDetail").appendChild(element("div", { className: "table-state", text: "No segment detail is available." }));
  }
  $("revokeFromDetailButton").hidden = !task.revocable;
  const metadata = pick(data, ["metadata"], null);
  if (metadata && window.AnnotationMetadata) {
    const banner = element("div", { attrs: { id: "adminTaskMetadataBanner" } });
    $("taskDetail").insertBefore(banner, $("taskDetail").firstChild);
    AnnotationMetadata.renderBanner(banner, metadata);
    const details = element("div", { attrs: { id: "adminTaskMetadataDetails" } });
    banner.after(details);
    AnnotationMetadata.renderDetails(details, metadata, { open: false, disclosureId: "adminMetadataDisclosure" });
  }
  const form = $("adminReviewForm");
  if (form) {
    const publishedId = pick(data, ["current_version_id"], "") || "";
    const review = pick(metadata, ["scene_review"], {}) || {};
    const writeEnabled = boolValue(pick(metadata, ["features.scene_review_write", "features.metadata_ui"], true), true);
    form.hidden = !publishedId || !writeEnabled;
    form.dataset.taskId = task.taskId || "";
    form.dataset.versionId = publishedId;
    form.dataset.reviewId = review.id || "";
    formError($("adminReviewError"));
    $("adminReviewStatus").value = review.status || "pending";
    $("adminReviewNote").value = review.note || "";
    $("adminReviewReason").value = "";
    const selected = new Set(listValue(review, ["scene_codes"]));
    for (const box of document.querySelectorAll("#adminReviewScenes input[type=checkbox]")) {
      box.checked = selected.has(box.value);
    }
    const current = $("adminReviewCurrent");
    if (current) {
      const human = (review.scene_codes || []).map(sceneLabel).join("、");
      const sourceText = (metadata?.sources || []).map((item) => item.scene_label || sceneLabel(item.scene_code)).join(", ") || "Spoken languages";
      current.textContent = `Current published review: ${REVIEW_STATUS_LABEL[review.status] || review.status || "Pending"}${human ? " · " + human : ""}. Source scenes: ${sourceText}.`;
    }
  }
}

function revokeItemsForTasks(tasks) {
  return tasks.filter((task) => task?.taskId && task?.expectedVersionId).map((task) => ({
    task_id: task.taskId,
    expected_version_id: task.expectedVersionId,
  }));
}

async function openRevoke(tasks) {
  const validTasks = tasks.filter((task) => task && task.revocable);
  const items = revokeItemsForTasks(validTasks);
  if (!items.length) {
    toast("These records are no longer eligible for revocation.", "error");
    return;
  }
  const annotatorId = validTasks[0].annotatorId || state.selectedAnnotatorId;
  if (!annotatorId) {
    toast("The submitting annotator could not be identified.", "error");
    return;
  }
  const operationId = uuid();
  state.pendingRevoke = { tasks: validTasks, items, annotatorId, operationId, previewReady: false };
  $("revokeForm").reset();
  $("blockReclaim").checked = true;
  const batch = items.length > 1;
  $("revokeAdminKeyField").hidden = !batch;
  $("revokeAdminKey").required = batch;
  $("revokeAdminKey").value = "";
  formError($("revokeError"));
  text($("revokeTitle"), items.length === 1 ? "Revoke annotation" : `Revoke ${items.length} annotations`);
  text($("revokeDescription"), items.length === 1 ? "The task will return to the available pool with a clean draft." : "All selected tasks will be revoked in one transaction and returned to the available pool.");
  clear($("revokePreview"));
  $("revokePreview").classList.remove("impact-error");
  $("revokePreview").append(element("span", { className: "spinner spinner-small", attrs: { "aria-hidden": "true" } }), element("span", { text: "Checking impact…" }));
  $("confirmRevokeButton").disabled = true;
  closeDialog($("taskDialog"));
  openDialog($("revokeDialog"));
  try {
    const preview = await api.post("/api/admin/annotations/revoke/preview", {
      annotator_id: annotatorId,
      items,
      block_reclaim: true,
    });
    if (!state.pendingRevoke || state.pendingRevoke.operationId !== operationId) return;
    state.pendingRevoke.preview = preview;
    state.pendingRevoke.previewReady = true;
    renderRevokePreview(preview, items.length);
  } catch (error) {
    if (!state.pendingRevoke || state.pendingRevoke.operationId !== operationId) return;
    if (error.status === 404) {
      state.pendingRevoke.previewReady = true;
      renderRevokePreview({ summary: { task_count: items.length } }, items.length);
    } else {
      clear($("revokePreview"));
      $("revokePreview").classList.add("impact-error");
      $("revokePreview").appendChild(element("span", { text: error.message }));
    }
  }
}

function renderRevokePreview(preview, fallbackCount) {
  const summary = pick(preview, ["summary", "impact"], preview || {});
  const count = numberValue(pick(summary, ["task_count", "tasks", "eligible_count", "revokeable"], fallbackCount));
  const duration = numberValue(pick(summary, ["duration_seconds", "audio_duration_seconds"], 0));
  const conflicts = numberValue(pick(summary, ["conflict_count", "conflicts"], 0));
  clear($("revokePreview"));
  $("revokePreview").classList.toggle("impact-error", conflicts > 0);
  $("revokePreview").append(
    element("span", { className: "impact-title", text: conflicts ? "Conflicts require attention" : `${count} task${count === 1 ? "" : "s"} will return to pending` }),
    element("span", { className: "impact-details" }, [
      element("span", { text: `${formatDuration(duration)} audio` }),
      element("span", { text: `${conflicts} conflicts` }),
      element("span", { text: "Historical versions remain auditable" }),
    ]),
  );
  if (conflicts) $("confirmRevokeButton").disabled = true;
  if (state.pendingRevoke) state.pendingRevoke.hasConflicts = conflicts > 0;
  validateRevokeForm();
}

function validateRevokeForm() {
  const pending = state.pendingRevoke;
  const reasonReady = Boolean($("revokeReason").value.trim());
  const keyReady = !pending || pending.items.length === 1
    || Boolean($("revokeAdminKey").value.trim());
  $("confirmRevokeButton").disabled = !pending?.previewReady
    || Boolean(pending.hasConflicts) || !reasonReady || !keyReady;
}

async function submitRevoke(event) {
  event.preventDefault();
  const pending = state.pendingRevoke;
  if (!pending?.previewReady) return;
  const reason = $("revokeReason").value.trim();
  if (!reason) {
    formError($("revokeError"), "Enter a reason for the audit log.");
    $("revokeReason").focus();
    return;
  }
  const adminKey = pending.items.length > 1 ? $("revokeAdminKey").value : "";
  if (pending.items.length > 1 && !adminKey.trim()) {
    formError($("revokeError"), "Re-enter the admin key for batch revocation.");
    $("revokeAdminKey").focus();
    return;
  }
  formError($("revokeError"));
  setButtonBusy($("confirmRevokeButton"), true, "Revoking…");
  try {
    const payload = {
      operation_id: pending.operationId,
      annotator_id: pending.annotatorId,
      items: pending.items,
      reason,
      block_reclaim: $("blockReclaim").checked,
      confirm: true,
    };
    if (pending.items.length > 1) payload.admin_key = adminKey;
    const result = await api.post("/api/admin/annotations/revoke", payload);
    const stillCurrent = state.pendingRevoke?.operationId === pending.operationId;
    if (stillCurrent) {
      $("revokeAdminKey").value = "";
      closeDialog($("revokeDialog"));
      state.pendingRevoke = null;
      state.selectedTaskIds.clear();
    }
    toast(pick(result, ["message"], `${pending.items.length} annotation${pending.items.length === 1 ? "" : "s"} revoked.`));
    await Promise.allSettled([loadAnnotators(), loadCurrentView()]);
  } catch (error) {
    if (state.pendingRevoke?.operationId !== pending.operationId) {
      if (error.status !== 401) toast(`A previous revocation failed: ${error.message}`, "error");
      return;
    }
    $("revokeAdminKey").value = "";
    formError($("revokeError"), error.message);
    setButtonBusy($("confirmRevokeButton"), false);
    validateRevokeForm();
  }
}

async function openDeactivate() {
  const annotator = state.annotatorDetail?.annotator;
  if (!annotator || annotator.status !== "active") return;
  const operationId = uuid();
  state.pendingDeactivate = { annotator, operationId, previewReady: false };
  $("deactivateForm").reset();
  text($("confirmUsernameLabel"), annotator.username);
  text($("deactivateTitle"), `Deactivate ${annotator.username}`);
  formError($("deactivateError"));
  clear($("deactivatePreview"));
  $("deactivatePreview").classList.remove("impact-error");
  $("deactivatePreview").append(element("span", { className: "spinner spinner-small", attrs: { "aria-hidden": "true" } }), element("span", { text: "Checking impact…" }));
  $("confirmDeactivateButton").disabled = true;
  openDialog($("deactivateDialog"));
  try {
    const preview = await api.post(`/api/admin/annotators/${encodeURIComponent(annotator.id)}/deactivate/preview`, {});
    if (!state.pendingDeactivate || state.pendingDeactivate.operationId !== operationId) return;
    state.pendingDeactivate.preview = preview;
    state.pendingDeactivate.previewReady = true;
    renderDeactivatePreview(preview);
    validateDeactivateForm();
  } catch (error) {
    if (!state.pendingDeactivate || state.pendingDeactivate.operationId !== operationId) return;
    clear($("deactivatePreview"));
    $("deactivatePreview").classList.add("impact-error");
    $("deactivatePreview").appendChild(element("span", { text: error.message }));
  }
}

function renderDeactivatePreview(preview) {
  const summary = pick(preview, ["summary", "impact"], preview || {});
  const revokeCount = numberValue(pick(summary, ["revoke_count", "current_contributions", "published_count", "published_to_revoke"], 0));
  const assignments = numberValue(pick(summary, ["assignment_count", "active_assignments", "assignments_to_release"], 0));
  const duration = numberValue(pick(summary, ["duration_seconds", "audio_duration_seconds"], 0));
  clear($("deactivatePreview"));
  $("deactivatePreview").append(
    element("span", { className: "impact-title", text: "Access will be disabled immediately" }),
    element("span", { className: "impact-details" }, [
      element("span", { text: `${revokeCount} current contributions returned` }),
      element("span", { text: `${assignments} assignments released` }),
      element("span", { text: `${formatDuration(duration)} audio affected` }),
    ]),
  );
}

function validateDeactivateForm() {
  const pending = state.pendingDeactivate;
  const valid = pending?.previewReady
    && $("deactivateReason").value.trim().length > 0
    && $("confirmUsername").value === pending.annotator.username
    && $("deactivateAdminKey").value.length > 0;
  $("confirmDeactivateButton").disabled = !valid;
  return valid;
}

async function submitDeactivate(event) {
  event.preventDefault();
  const pending = state.pendingDeactivate;
  if (!pending || !validateDeactivateForm()) {
    formError($("deactivateError"), "Complete the reason, username confirmation, and admin key.");
    return;
  }
  formError($("deactivateError"));
  setButtonBusy($("confirmDeactivateButton"), true, "Deactivating…");
  try {
    const result = await api.post(`/api/admin/annotators/${encodeURIComponent(pending.annotator.id)}/deactivate`, {
      operation_id: pending.operationId,
      reason: $("deactivateReason").value.trim(),
      confirm_username: $("confirmUsername").value,
      admin_key: $("deactivateAdminKey").value,
      confirm: true,
    });
    $("deactivateAdminKey").value = "";
    closeDialog($("deactivateDialog"));
    state.pendingDeactivate = null;
    toast(pick(result, ["message"], `${pending.annotator.username} was deactivated and their work was returned.`));
    await loadAnnotators();
    state.selectedAnnotatorId = "";
    syncUrl();
    await navigate("overview");
  } catch (error) {
    $("deactivateAdminKey").value = "";
    formError($("deactivateError"), error.message);
    setButtonBusy($("confirmDeactivateButton"), false);
    validateDeactivateForm();
  }
}

function openDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

function closeDialog(dialog) {
  if (dialog?.open) dialog.close();
}

function selectedAnnotatorTasks() {
  return state.annotatorTasks.filter((task) => state.selectedTaskIds.has(task.taskId));
}

function taskById(id) {
  return state.annotatorTasks.find((task) => task.taskId === id)
    || state.corpusTasks.find((task) => task.taskId === id)
    || state.qualityItems.map(normaliseTask).find((task) => task.taskId === id)
    || (state.currentTaskDetail?.taskId === id ? state.currentTaskDetail : null);
}

function csvCell(value) {
  let string = value === null || value === undefined ? "" : String(value);
  if (/^[=+\-@]/.test(string)) string = `'${string}`;
  return `"${string.replaceAll('"', '""')}"`;
}

function downloadCsv(filename, headers, rows) {
  if (!rows.length) {
    toast("There is no visible data to export.", "error");
    return;
  }
  const lines = [headers.map(csvCell).join(","), ...rows.map((row) => row.map(csvCell).join(","))];
  const blob = new Blob(["\ufeff", lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = element("a", { attrs: { href: url, download: filename } });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Exported ${rows.length} visible rows.`);
}

function exportTasks(tasks, prefix) {
  downloadCsv(`${prefix}-${localDateString(new Date())}.csv`, ["task_id", "filename", "folder", "category", "status", "annotator", "duration_seconds", "segments", "submitted_at", "version_id"], tasks.map((task) => [
    task.taskId, task.filename, task.folder, task.category, task.status, task.annotatorName, task.durationSeconds, task.segmentCount, task.submittedAt, task.expectedVersionId,
  ]));
}

function exportAudit() {
  downloadCsv(`admin-activity-${localDateString(new Date())}.csv`, ["time", "action", "target", "admin_key", "reason", "result", "operation_id"], state.auditItems.map((item) => [
    pick(item, ["created_at", "timestamp"], ""), pick(item, ["action_type", "action"], ""), pick(item, ["target", "task_id", "annotator_id"], ""), pick(item, ["key_id", "admin_key_id"], ""), pick(item, ["reason"], ""), pick(item, ["status", "result"], ""), pick(item, ["operation_id", "id"], ""),
  ]));
}

function openSidebar() {
  $("sidebar").classList.add("is-open");
  $("sidebarScrim").hidden = false;
  updateSidebarAccessibility();
  $("closeSidebarButton").focus();
}

function closeSidebar(restoreFocus = false) {
  $("sidebar").classList.remove("is-open");
  $("sidebarScrim").hidden = true;
  updateSidebarAccessibility();
  if (restoreFocus && matchMedia("(max-width: 780px)").matches) {
    $("openSidebarButton").focus();
  }
}

function updateSidebarAccessibility() {
  const mobile = matchMedia("(max-width: 780px)").matches;
  const open = $("sidebar").classList.contains("is-open");
  $("sidebar").toggleAttribute("inert", mobile && !open);
  if (mobile && !open) $("sidebar").setAttribute("aria-hidden", "true");
  else $("sidebar").removeAttribute("aria-hidden");
  $("openSidebarButton").setAttribute("aria-expanded", String(mobile && open));
}

function handleActivityPointer(event) {
  const chart = charts.activity;
  if (!chart.points.length || !chart.xPositions.length) return;
  const rect = $("activityChart").getBoundingClientRect();
  const x = event.clientX - rect.left;
  let closest = 0;
  let distance = Infinity;
  chart.xPositions.forEach((position, index) => {
    const current = Math.abs(position - x);
    if (current < distance) { distance = current; closest = index; }
  });
  const point = chart.points[closest];
  const tooltip = $("activityTooltip");
  tooltip.textContent = `${formatDateTime(point.date, true)}\nAnnotated  ${formatInteger(point.annotated)}\nSkipped  ${formatInteger(point.skipped)}\nRevoked  ${formatInteger(point.revoked)}`;
  tooltip.hidden = false;
}

function redrawVisibleCharts() {
  if (state.view === "overview") {
    drawActivityChart(state.timeseries);
    if (state.overview) {
      const stats = overviewStats(state.overview);
      drawQueueChart({
        available: numberValue(pick(stats, ["pending_available", "available", "pending.available_count"], 0)),
        assigned: numberValue(pick(stats, ["pending_assigned", "assigned", "pending.assigned_count"], 0)),
        reserved: numberValue(pick(stats, ["pending_reserved", "reserved", "pending.reserved_count"], 0)),
        blocked: numberValue(pick(stats, ["pending_ineligible", "ineligible", "pending.ineligible_count"], 0)),
      }, numberValue(pick(stats, ["pending", "pending_count", "totals.pending_count"], 0)));
    }
  } else if (state.view === "annotators" && charts.annotator.points.length) {
    drawSeriesChart($("annotatorChart"), charts.annotator.points, [{ key: "annotated", color: "#10a37f" }, { key: "skipped", color: "#98a1ad" }]);
  }
}

function setupEvents() {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const key = $("adminKey").value;
    if (!key.trim()) {
      formError($("loginError"), "Enter the admin key.");
      return;
    }
    formError($("loginError"));
    setButtonBusy($("loginButton"), true, "Signing in…");
    try {
      const session = await api.request("/api/admin/login", { method: "POST", body: { key }, allowUnauthorized: true });
      $("adminKey").value = "";
      if (!boolValue(pick(session, ["authenticated"], true))) throw new ApiError("The admin key was not accepted.", 401);
      await enterApp(session);
    } catch (error) {
      formError($("loginError"), error.status === 401 ? "The admin key was not accepted." : error.message);
    } finally { setButtonBusy($("loginButton"), false); }
  });

  $("showLoginKey").addEventListener("click", () => {
    const showing = $("adminKey").type === "text";
    $("adminKey").type = showing ? "password" : "text";
    $("showLoginKey").textContent = showing ? "Show" : "Hide";
    $("showLoginKey").setAttribute("aria-pressed", String(!showing));
    $("showLoginKey").setAttribute("aria-label", `${showing ? "Show" : "Hide"} admin key`);
  });

  $("logoutButton").addEventListener("click", async () => {
    try { await api.post("/api/admin/logout", {}); }
    catch (error) { if (error.status !== 401) toast(error.message, "error"); }
    enterLogin();
  });

  for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => navigate(button.dataset.view));
  $("openSidebarButton").addEventListener("click", openSidebar);
  $("closeSidebarButton").addEventListener("click", () => closeSidebar(true));
  $("sidebarScrim").addEventListener("click", () => closeSidebar(true));
  $("sidebarAnnotatorSearch").addEventListener("input", renderAnnotatorDirectory);
  $("sidebarAnnotatorList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-annotator-id]");
    if (button) selectAnnotator(button.dataset.annotatorId);
  });

  $("dateRange").addEventListener("change", async () => {
    state.range = $("dateRange").value;
    $("customDates").hidden = state.range !== "custom";
    applyPreset(state.range);
    if (state.range !== "custom") {
      syncUrl();
      await loadCurrentView();
    }
  });
  $("applyDatesButton").addEventListener("click", async () => {
    const from = $("dateFrom").value;
    const to = $("dateTo").value;
    if (!from || !to || from > to) {
      toast("Choose a valid start and end date.", "error");
      return;
    }
    state.range = "custom";
    state.dateFrom = from;
    state.dateTo = to;
    syncUrl();
    await loadCurrentView();
  });
  $("refreshButton").addEventListener("click", async () => {
    hideStatus();
    await Promise.allSettled([loadAnnotators(), loadCurrentView()]);
  });
  $("statusRetryButton").addEventListener("click", loadCurrentView);

  $("annotatorTaskBody").addEventListener("change", (event) => {
    const checkbox = event.target.closest("[data-select-task]");
    if (!checkbox) return;
    if (checkbox.checked) state.selectedTaskIds.add(checkbox.dataset.selectTask);
    else state.selectedTaskIds.delete(checkbox.dataset.selectTask);
    updateTaskSelection();
  });
  $("selectAllTasks").addEventListener("change", () => {
    for (const task of state.annotatorTasks.filter((item) => item.revocable)) {
      if ($("selectAllTasks").checked) state.selectedTaskIds.add(task.taskId);
      else state.selectedTaskIds.delete(task.taskId);
    }
    renderAnnotatorTasks();
  });
  $("batchRevokeButton").addEventListener("click", () => openRevoke(selectedAnnotatorTasks()));
  $("loadMoreAnnotatorTasks").addEventListener("click", loadMoreAnnotatorTasks);
  $("deactivateAnnotatorButton").addEventListener("click", openDeactivate);
  $("exportAnnotatorButton").addEventListener("click", () => exportTasks(state.annotatorTasks, `annotations-${state.annotatorDetail?.annotator.username || "annotator"}`));

  const taskActionHandler = (event) => {
    const button = event.target.closest("[data-task-action]");
    if (!button) return;
    const task = taskById(button.dataset.taskId);
    if (button.dataset.taskAction.startsWith("view")) showTask(button.dataset.taskId, task);
    else if (button.dataset.taskAction.startsWith("revoke")) openRevoke([task]);
  };
  $("annotatorTaskBody").addEventListener("click", taskActionHandler);
  $("corpusTaskBody").addEventListener("click", taskActionHandler);
  $("qualityTaskBody").addEventListener("click", taskActionHandler);

  $("qualityFilterForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.qualityQuery = $("qualitySearch").value.trim();
    state.qualityAnnotatorId = $("qualityAnnotator").value;
    state.qualitySignal = $("qualitySignal").value;
    syncUrl();
    await loadQuality(++state.requestEpoch);
  });

  $("corpusFilterForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.corpusTaskCursor = null;
    syncMetadataFilters("corpus");
    syncUrl();
    await loadCorpus(false, ++state.requestEpoch);
  });
  $("overviewFilterForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    state.corpusTaskCursor = null;
    syncMetadataFilters("overview");
    syncUrl();
    await loadCurrentView();
  });
  $("loadMoreCorpusTasks").addEventListener("click", async () => {
    setButtonBusy($("loadMoreCorpusTasks"), true, "Loading…");
    await loadCorpus(true);
    setButtonBusy($("loadMoreCorpusTasks"), false);
  });
  $("exportCorpusButton").addEventListener("click", () => exportTasks(state.corpusTasks, "corpus-tasks"));
  $("sceneScopeForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = $("sceneScopeForm");
    const fields = $("scopeEditorFields");
    const annotatorId = form?.dataset.annotatorId || "";
    const token = Number(form?.dataset.loadToken || 0);
    if (!form || form.dataset.scopeReady !== "1") return;
    if (!annotatorId || annotatorId !== state.selectedAnnotatorId) return;
    if (token !== state.scopeLoadToken) return;
    if ((state.scopeSaving && state.scopeSavingId === annotatorId) || fields?.disabled) return;
    formError($("scopeError"));
    const codes = Array.from(document.querySelectorAll("#scopeSceneGrid input[type=checkbox]:checked")).map((box) => box.value);
    const reason = $("scopeReason").value.trim();
    if (!reason) {
      formError($("scopeError"), "Please enter a reason for this change.");
      $("scopeReason").focus();
      return;
    }
    const revision = Number(form.dataset.revision || 0);
    const mode = $("scopeMode").value;
    const body = {
      operation_id: uuid(),
      expected_revision: revision,
      mode,
      scene_codes: mode === "restricted" ? codes : [],
      allow_unknown: mode === "restricted" && codes.includes("spoken_languages"),
      reason,
    };
    state.scopeSaving = true;
    state.scopeSavingId = annotatorId;
    const saveToken = ++state.scopeLoadToken;
    setScopeEditorUnavailable("Saving claim scope…", { hideFields: false });
    if (fields) fields.disabled = true;
    try {
      await api.request(`/api/admin/annotators/${encodeURIComponent(annotatorId)}/scene-scope`, {
        method: "PUT",
        body,
      });
      toast("Claim scope saved. Existing assignments are not released.");
      if (annotatorId === state.selectedAnnotatorId && state.scopeLoadToken === saveToken) {
        $("scopeReason").value = "";
        await loadAnnotator(annotatorId);
      }
    } catch (error) {
      formError($("scopeError"), error.message);
      toast(error.message, "error");
      if (annotatorId === state.selectedAnnotatorId && state.scopeLoadToken === saveToken) {
        state.scopeEditorBound = { id: annotatorId, token: saveToken };
        form.dataset.scopeReady = "1";
        form.dataset.annotatorId = annotatorId;
        form.dataset.loadToken = String(saveToken);
        form.dataset.revision = String(revision);
        form.setAttribute("aria-busy", "false");
        if (fields) {
          fields.disabled = false;
          fields.hidden = false;
        }
        const status = $("scopeEditorStatus");
        if (status) {
          status.hidden = true;
          status.textContent = "";
        }
      }
    } finally {
      if (state.scopeSavingId === annotatorId) {
        state.scopeSaving = false;
        state.scopeSavingId = "";
      }
    }
  });
  $("adminReviewForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = $("adminReviewForm");
    const taskId = form.dataset.taskId;
    if (!taskId) return;
    formError($("adminReviewError"));
    const codes = Array.from(document.querySelectorAll("#adminReviewScenes input[type=checkbox]:checked")).map((box) => box.value);
    const status = $("adminReviewStatus").value;
    const reason = $("adminReviewReason").value.trim();
    if (!reason) {
      formError($("adminReviewError"), "Please enter a reason for this correction.");
      $("adminReviewReason").focus();
      return;
    }
    const check = window.AnnotationMetadata
      ? AnnotationMetadata.validateReview({ status, scene_codes: codes })
      : { ok: true };
    if (!check.ok) {
      formError($("adminReviewError"), check.error);
      return;
    }
    try {
      await api.post(`/api/admin/annotations/${encodeURIComponent(taskId)}/scene-review`, {
        operation_id: uuid(),
        expected_version_id: form.dataset.versionId,
        expected_review_id: form.dataset.reviewId || null,
        status,
        scene_codes: codes,
        note: $("adminReviewNote").value,
        reason,
      });
      toast("Human review correction appended.");
      await showTask(taskId);
    } catch (error) {
      formError($("adminReviewError"), error.message);
      toast(error.message, "error");
    }
  });

  $("auditFilterForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await loadAudit(false, ++state.requestEpoch);
  });
  $("loadMoreAudit").addEventListener("click", async () => {
    setButtonBusy($("loadMoreAudit"), true, "Loading…");
    await loadAudit(true);
    setButtonBusy($("loadMoreAudit"), false);
  });
  $("exportAuditButton").addEventListener("click", exportAudit);

  $("revokeForm").addEventListener("submit", submitRevoke);
  $("revokeReason").addEventListener("input", validateRevokeForm);
  $("revokeAdminKey").addEventListener("input", validateRevokeForm);
  $("deactivateForm").addEventListener("submit", submitDeactivate);
  $("deactivateReason").addEventListener("input", validateDeactivateForm);
  $("confirmUsername").addEventListener("input", validateDeactivateForm);
  $("deactivateAdminKey").addEventListener("input", validateDeactivateForm);
  $("revokeFromDetailButton").addEventListener("click", () => openRevoke([state.currentTaskDetail]));

  for (const button of document.querySelectorAll("[data-close-dialog]")) {
    button.addEventListener("click", () => closeDialog($(button.dataset.closeDialog)));
  }
  for (const dialog of document.querySelectorAll("dialog")) {
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(dialog); });
    dialog.addEventListener("close", () => {
      if (dialog === $("revokeDialog")) {
        state.pendingRevoke = null;
        $("revokeAdminKey").value = "";
        $("revokeAdminKey").required = false;
      }
      if (dialog === $("deactivateDialog")) {
        state.pendingDeactivate = null;
        $("deactivateAdminKey").value = "";
      }
    });
  }

  $("activityChart").addEventListener("pointermove", handleActivityPointer);
  $("activityChart").addEventListener("pointerleave", () => { $("activityTooltip").hidden = true; });

  let resizeFrame = 0;
  window.addEventListener("resize", () => {
    updateSidebarAccessibility();
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(redrawVisibleCharts);
  });
  updateSidebarAccessibility();
  window.addEventListener("popstate", async () => {
    restoreUrlState();
    renderViewShell();
    await loadCurrentView();
  });
}

initialize();
