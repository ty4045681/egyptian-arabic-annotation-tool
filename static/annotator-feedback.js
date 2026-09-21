/* Task feedback shared by the annotator workspace and submission history. */
(function (root) {
  "use strict";
  const FLASH_KEY = "annotator.taskFlash";
  const LEGACY_FLASH_KEY = "annotator.crossCheckFlash";

  function normalize(payload) {
    return {
      kind: payload && payload.kind === "abandon" ? "abandon" : "complete",
      status: payload && payload.status === "skipped" ? "skipped" : "annotated",
    };
  }

  function message(payload) {
    const task = normalize(payload);
    if (task.kind === "abandon") return "Task released";
    return task.status === "skipped" ? "Task skipped" : "Task completed";
  }

  function setFlash(payload) {
    try { sessionStorage.setItem(FLASH_KEY, JSON.stringify(normalize(payload))); }
    catch (_) { /* Storage can be disabled. */ }
  }

  function consumeFlash() {
    try {
      const raw = sessionStorage.getItem(FLASH_KEY) || sessionStorage.getItem(LEGACY_FLASH_KEY);
      sessionStorage.removeItem(FLASH_KEY);
      sessionStorage.removeItem(LEGACY_FLASH_KEY);
      return raw ? normalize(JSON.parse(raw)) : null;
    } catch (_) { return null; }
  }

  function errorMessage(message, body) {
    if (body && body.code === "cross_check_active") {
      return "This task is currently unavailable for correction.";
    }
    const text = String(message || "Request failed");
    if (/cross[-_ ]?check|independent transcript|original (version|transcript)|secondary (version|transcript)/i.test(text) ||
        /cross[_-]?check/i.test(String(body && body.code || ""))) {
      return "This task has changed. Please reload and try again.";
    }
    return text;
  }

  root.AnnotatorFeedback = { message, setFlash, consumeFlash, errorMessage };
})(window);
