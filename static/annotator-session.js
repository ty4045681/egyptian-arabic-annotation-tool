/* Annotator web-session UX: activity heartbeat, structured 401, freeze writes. */
(function (global) {
  "use strict";

  const TRUSTED_ACTIVITY = { pointerdown: 1, keydown: 1, input: 1 };
  const state = {
    username: "",
    heartbeatSeconds: 30,
    idleWarningSeconds: 120,
    retentionDays: 7,
    idleExpiresAt: null,
    absoluteExpiresAt: null,
    activitySinceHeartbeat: false,
    frozen: false,
    overlayCode: "",
    timer: null,
    beatPromise: null,
    persist: null,
    hasDirty: null,
    onFrozen: null,
    onExport: null,
    onOnline: null,
    onRelogin: null
  };

  function trustedActivity(event) {
    if (!event || event.isTrusted === false) return false;
    if (TRUSTED_ACTIVITY[event.type]) return true;
    const target = event.target;
    if (!target || !target.closest) return false;
    if (event.type === "click" && target.closest("button, a, [data-activity]")) return true;
    return false;
  }

  function markActivity() {
    state.activitySinceHeartbeat = true;
  }

  function writesFrozen() {
    return state.frozen;
  }

  function freezeWrites() {
    state.frozen = true;
    if (typeof state.onFrozen === "function") state.onFrozen();
  }

  function overlayEl() {
    return document.getElementById("sessionOverlay");
  }

  function setNetworkFlag(offline) {
    const node = document.getElementById("networkFlag");
    if (!node) return;
    node.hidden = !offline;
    node.textContent = offline ? "Offline — edits stay on this device until you reconnect." : "";
  }

  function showOverlay(code, options) {
    options = options || {};
    state.overlayCode = code;
    const root = overlayEl();
    if (!root) return;
    const title = document.getElementById("sessionOverlayTitle");
    const body = document.getElementById("sessionOverlayBody");
    const copy = {
      session_replaced: {
        title: "This session was replaced",
        body: "Another device signed in with this name. This page can no longer save, submit, claim, abandon, or reopen tasks. Copy any local unsaved text before you leave."
      },
      idle_timeout: {
        title: "Session expired",
        body: "This session ended after a period without real activity. Local unsaved text is kept on this device. Sign in again to continue the same assignment."
      },
      absolute_timeout: {
        title: "Session reached its time limit",
        body: "This session reached its 20-hour limit. Local unsaved text is kept on this device. Sign in again to continue."
      },
      account_deactivated: {
        title: "Account deactivated",
        body: "This account is no longer active. This page can no longer save or submit. Copy any local unsaved text before leaving."
      },
      not_authenticated: {
        title: "Sign in required",
        body: "You are not signed in. Local unsaved text on this device can still be copied before you leave."
      }
    };
    const text = copy[code] || copy.not_authenticated;
    if (title) title.textContent = text.title;
    if (body) body.textContent = options.message || text.body;
    root.classList.add("show");
    root.setAttribute("aria-hidden", "false");
    const exportBtn = document.getElementById("sessionExportButton");
    if (exportBtn) exportBtn.hidden = !options.allowExport;
  }

  async function persistLocal() {
    if (typeof state.persist === "function") {
      try { await state.persist(); } catch (_) {}
    }
  }

  async function handleUnauthorized(data, options) {
    options = options || {};
    const code = (data && data.code) || "not_authenticated";
    const hasDirty = Object.prototype.hasOwnProperty.call(options, "hasDirty")
      ? Boolean(options.hasDirty)
      : (typeof state.hasDirty === "function" && Boolean(state.hasDirty()));
    await persistLocal();
    if (code === "session_replaced" || code === "account_deactivated") {
      freezeWrites();
      showOverlay(code, { allowExport: hasDirty });
      return code;
    }
    if (code === "idle_timeout" || code === "absolute_timeout") {
      freezeWrites();
      showOverlay(code, { allowExport: hasDirty });
      return code;
    }
    if (hasDirty) {
      freezeWrites();
      showOverlay("not_authenticated", { allowExport: true });
      return code;
    }
    const redirect = (data && data.redirect) || "/login.html";
    location.href = redirect;
    return code;
  }

  async function beat() {
    if (state.frozen) return false;
    if (state.beatPromise) return state.beatPromise;
    const activity = document.visibilityState !== "hidden" && state.activitySinceHeartbeat;
    if (activity) state.activitySinceHeartbeat = false;
    state.beatPromise = (async function () {
      try {
        const response = await fetch("/api/session/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ activity: Boolean(activity) })
        });
        let data = {};
        try { data = await response.json(); } catch (_) {}
        if (response.status === 401 ||
            (response.status === 403 && data.code === "account_deactivated")) {
          await handleUnauthorized(data);
          return false;
        }
        if (!response.ok) {
          if (activity) state.activitySinceHeartbeat = true;
          setNetworkFlag(true);
          return false;
        }
        setNetworkFlag(false);
        state.idleExpiresAt = data.idle_expires_at;
        state.absoluteExpiresAt = data.absolute_expires_at;
        return true;
      } catch (_) {
        if (activity) state.activitySinceHeartbeat = true;
        setNetworkFlag(true);
        return false;
      } finally {
        state.beatPromise = null;
      }
    })();
    return state.beatPromise;
  }

  function startHeartbeat() {
    stopHeartbeat();
    const seconds = Math.max(15, Number(state.heartbeatSeconds) || 30);
    state.timer = setInterval(beat, seconds * 1000);
  }

  function stopHeartbeat() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  function configure(session, username, hooks) {
    hooks = hooks || {};
    state.username = username || "";
    if (session) {
      state.heartbeatSeconds = session.heartbeat_seconds || 30;
      state.idleWarningSeconds = session.idle_warning_seconds || 120;
      state.retentionDays = session.offline_draft_retention_days || 7;
      state.idleExpiresAt = session.idle_expires_at;
      state.absoluteExpiresAt = session.absolute_expires_at;
    }
    state.persist = hooks.persist || null;
    state.hasDirty = hooks.hasDirty || null;
    state.onFrozen = hooks.onFrozen || null;
    state.onExport = hooks.onExport || null;
    state.onOnline = hooks.onOnline || null;
    state.onRelogin = hooks.onRelogin || function () {
      const params = state.overlayCode ? ("?reason=" + encodeURIComponent(state.overlayCode)) : "";
      location.href = "/login.html" + params;
    };
    startHeartbeat();
  }

  function bindUi() {
    ["pointerdown", "keydown", "input"].forEach(function (type) {
      document.addEventListener(type, function (event) {
        if (trustedActivity(event)) markActivity();
      }, true);
    });
    document.addEventListener("play", function (event) {
      if (event && event.isTrusted !== false && event.target && event.target.id === "audio") markActivity();
    }, true);
    document.addEventListener("seeked", function (event) {
      if (event && event.isTrusted !== false && event.target && event.target.id === "audio") markActivity();
    }, true);
    window.addEventListener("online", async function () {
      const active = await beat();
      if (active && typeof state.onOnline === "function") {
        try { await state.onOnline(); } catch (_) { setNetworkFlag(true); }
      }
    });
    window.addEventListener("offline", function () { setNetworkFlag(true); });
    const exportBtn = document.getElementById("sessionExportButton");
    if (exportBtn) {
      exportBtn.addEventListener("click", async function () {
        await persistLocal();
        if (typeof state.onExport === "function") await state.onExport();
      });
    }
    const relogin = document.getElementById("sessionReloginButton");
    if (relogin) {
      relogin.addEventListener("click", function () {
        if (typeof state.onRelogin === "function") state.onRelogin();
      });
    }
  }

  bindUi();

  global.AnnotatorSession = {
    configure: configure,
    markActivity: markActivity,
    startHeartbeat: startHeartbeat,
    stopHeartbeat: stopHeartbeat,
    handleUnauthorized: handleUnauthorized,
    freezeWrites: freezeWrites,
    writesFrozen: writesFrozen,
    showOverlay: showOverlay,
    persistLocal: persistLocal,
    setNetworkFlag: setNetworkFlag,
    beat: beat,
    state: state
  };
})(window);
