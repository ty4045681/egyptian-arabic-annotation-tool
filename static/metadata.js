/* Shared scene provenance UI helpers. Safe text via textContent; links are http(s) only. */
(function (root) {
  "use strict";

  const CONF_ZH = { high: "来源置信度高", medium: "来源置信度中", low: "来源置信度低", unknown: "来源置信度未知" };
  const REVIEW_ZH = {
    pending: "场景待核验", confirmed: "场景已确认", mixed: "多场景",
    out_of_scope: "不属于九场景", uncertain: "无法判断",
  };

  function el(tag, opts, children) {
    const node = document.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = String(opts.text);
    if (opts.href) {
      const href = String(opts.href);
      if (/^https?:\/\//i.test(href)) {
        node.setAttribute("href", href);
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    }
    if (opts.attrs) {
      for (const [key, value] of Object.entries(opts.attrs)) {
        if (value != null) node.setAttribute(key, String(value));
      }
    }
    (Array.isArray(children) ? children : children ? [children] : []).forEach((child) => {
      if (child instanceof Node) node.appendChild(child);
      else if (child != null) node.appendChild(document.createTextNode(String(child)));
    });
    return node;
  }

  function headline(metadata) {
    if (metadata && metadata.headline) return metadata.headline;
    const sources = (metadata && metadata.sources) || [];
    const review = (metadata && metadata.scene_review) || {};
    const scenePart = sources.length
      ? (sources[0].scene_label || sources[0].scene_code)
      : "来源场景未知";
    const confPart = sources.length ? (CONF_ZH[sources[0].confidence] || CONF_ZH.unknown) : CONF_ZH.unknown;
    const reviewPart = REVIEW_ZH[review.status] || REVIEW_ZH.pending;
    return scenePart + " · " + confPart + " · " + reviewPart;
  }

  function renderBanner(container, metadata) {
    if (!container) return;
    container.replaceChildren();
    if (!metadata) return;
    container.appendChild(el("div", { className: "metadata-chip" }, headline(metadata)));
    const notice = el("div", { className: "metadata-notice", text: metadata.notice || "来源判断尚未代表人工核验" });
    container.appendChild(notice);
  }

  function renderDetails(container, metadata) {
    if (!container) return;
    container.replaceChildren();
    if (!metadata) return;
    const sources = metadata.sources || [];
    if (!sources.length) {
      container.appendChild(el("p", { text: "来源场景未知 / 来源置信度未知。旧任务没有来源证据。" }));
      return;
    }
    sources.forEach((source) => {
      const block = el("div", { className: "metadata-details" });
      block.appendChild(el("div", { text: (source.scene_label || "未知场景") + " · " + (CONF_ZH[source.confidence] || CONF_ZH.unknown) }));
      if (source.confidence_basis) block.appendChild(el("div", { text: "依据：" + source.confidence_basis }));
      if (source.batch_code) block.appendChild(el("div", { text: "批次：" + source.batch_code }));
      if (source.source_url) block.appendChild(el("a", { href: source.source_url, text: source.source_url }));
      container.appendChild(block);
    });
    if (metadata.prediction && metadata.prediction.stale) {
      container.appendChild(el("p", { text: "模型预测基于旧转写版本，不是当前人工结论。" }));
    }
  }

  function reviewPayload(status, codes, note) {
    return { status: status, scene_codes: codes || [], note: note || "" };
  }

  function renderReviewControls(container, options) {
    options = options || {};
    const current = options.value || { status: "pending", scene_codes: [], note: "" };
    container.replaceChildren();
    if (options.disabled) {
      container.appendChild(el("p", { text: REVIEW_ZH[current.status] || REVIEW_ZH.pending }));
      return current;
    }
    const state = {
      status: current.status || "pending",
      scene_codes: Array.isArray(current.scene_codes) ? current.scene_codes.slice() : [],
      note: current.note || "",
    };
    const heading = el("h2", { text: "场景核验（可随任务提交，非完成必填）" });
    const actions = el("div", { className: "scene-review-actions" });
    const statuses = [
      ["confirmed", "确认"],
      ["mixed", "调整/多场景"],
      ["out_of_scope", "不属于九场景"],
      ["uncertain", "无法判断"],
      ["pending", "暂不核验"],
    ];
    function emit() {
      if (typeof options.onChange === "function") options.onChange(reviewPayload(state.status, state.scene_codes, state.note));
    }
    statuses.forEach(([value, label]) => {
      const button = el("button", { text: label, attrs: { type: "button", "aria-pressed": String(state.status === value) } });
      button.addEventListener("click", () => {
        state.status = value;
        if (value === "pending" || value === "out_of_scope") state.scene_codes = [];
        if (value === "confirmed" && state.scene_codes.length > 1) state.scene_codes = state.scene_codes.slice(0, 1);
        Array.from(actions.children).forEach((child, index) => child.setAttribute("aria-pressed", String(statuses[index][0] === state.status)));
        sceneBox.hidden = !(value === "confirmed" || value === "mixed" || value === "uncertain");
        emit();
      });
      actions.appendChild(button);
    });
    const sceneBox = el("div", { className: "scene-review-scenes" });
    (options.scenes || []).forEach((scene) => {
      const label = el("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = scene.code;
      box.checked = state.scene_codes.includes(scene.code);
      box.addEventListener("change", () => {
        if (box.checked && !state.scene_codes.includes(scene.code)) state.scene_codes.push(scene.code);
        if (!box.checked) state.scene_codes = state.scene_codes.filter((code) => code !== scene.code);
        emit();
      });
      label.appendChild(box);
      label.appendChild(document.createTextNode(scene.label_zh || scene.code));
      sceneBox.appendChild(label);
    });
    sceneBox.hidden = !(state.status === "confirmed" || state.status === "mixed" || state.status === "uncertain");
    const note = document.createElement("textarea");
    note.placeholder = "备注（可选）";
    note.value = state.note;
    note.addEventListener("input", () => { state.note = note.value; emit(); });
    container.appendChild(heading);
    container.appendChild(actions);
    container.appendChild(sceneBox);
    container.appendChild(note);
    container.appendChild(el("p", { className: "metadata-notice", text: "来源判断尚未代表人工核验。" }));
    return state;
  }

  function renderScenePicker(container, options) {
    options = options || {};
    container.replaceChildren();
    const selected = options.selected || "";
    const counts = {};
    (options.pool && options.pool.by_scene || []).forEach((item) => { counts[item.scene_code] = item.available; });
    const wrap = el("div", { className: "scene-picker" });
    const allBtn = el("button", { attrs: { type: "button", "aria-pressed": String(!selected) } }, [
      el("span", { text: "范围内全部" }),
      el("span", { className: "count", text: String((options.pool && options.pool.available) || 0) + " 条" }),
    ]);
    allBtn.addEventListener("click", () => options.onChange && options.onChange(""));
    wrap.appendChild(allBtn);
    (options.scenes || []).forEach((scene) => {
      const button = el("button", { attrs: { type: "button", "aria-pressed": String(selected === scene.code) } }, [
        el("span", { text: scene.label_zh || scene.code }),
        el("span", { className: "count", text: String(counts[scene.code] || 0) + " 条" }),
      ]);
      button.addEventListener("click", () => options.onChange && options.onChange(scene.code));
      wrap.appendChild(button);
    });
    if (options.pool && (options.pool.scope && (options.pool.scope.mode === "all" || options.pool.scope.allow_unknown))) {
      const unknown = el("button", { attrs: { type: "button", "aria-pressed": String(selected === "unknown") } }, [
        el("span", { text: "来源未知" }),
        el("span", { className: "count", text: String(options.pool.unknown_available || 0) + " 条" }),
      ]);
      unknown.addEventListener("click", () => options.onChange && options.onChange("unknown"));
      wrap.appendChild(unknown);
    }
    container.appendChild(wrap);
  }

  root.AnnotationMetadata = {
    headline, renderBanner, renderDetails, renderReviewControls, renderScenePicker, reviewPayload,
  };
})(window);
