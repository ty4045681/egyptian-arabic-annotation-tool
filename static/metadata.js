/* Shared scene provenance UI helpers. Safe text via textContent; links are http(s) only. */
(function (root) {
  "use strict";

  const CONF_ZH = {
    high: "来源置信度高", medium: "来源置信度中",
    low: "来源置信度低", unknown: "来源置信度未知",
  };
  const REVIEW_ZH = {
    pending: "场景待核验", confirmed: "场景已确认", mixed: "多场景",
    out_of_scope: "不属于九场景", uncertain: "无法判断",
  };
  const SCENE_ZH = {
    airport: "机场", tourism_information: "旅游信息", shopping: "购物",
    clinic: "诊所", emergencies: "紧急情况",
    business_negotiation: "商务谈判", restaurant: "餐厅", hotel: "酒店",
    taxi: "出租车",
  };
  const STATUS_BUTTONS = [
    ["confirmed", "确认"],
    ["mixed", "调整/多场景"],
    ["out_of_scope", "不属于九场景"],
    ["uncertain", "无法判断"],
    ["pending", "暂不核验"],
  ];

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

  function sceneLabel(code) {
    if (!code) return "未知场景";
    return SCENE_ZH[code] || code;
  }

  function humanSceneSuffix(review) {
    const codes = (review && review.scene_codes) || [];
    if (!codes.length) return "";
    return "（" + codes.map(sceneLabel).join("、") + "）";
  }

  function claimPrimary(metadata) {
    const claim = (metadata && metadata.claim_context) || {};
    if (claim.source && (claim.source.scene_code || claim.source.confidence)) return claim.source;
    if (claim.scene_code) {
      return {
        scene_code: claim.scene_code,
        scene_label: sceneLabel(claim.scene_code),
        confidence: claim.confidence || "unknown",
      };
    }
    const sources = (metadata && metadata.sources) || [];
    if (sources.length === 1) return sources[0];
    return null;
  }

  function reviewHeadlinePart(metadata) {
    const review = (metadata && metadata.scene_review) || {};
    const draft = metadata && metadata.draft_review;
    const submitted = Boolean(review.submitted);
    if (draft != null && !submitted) {
      const status = draft.status || "pending";
      if (status === "pending" && !(draft.id || (draft.scene_codes || []).length)) {
        return REVIEW_ZH.pending;
      }
      return "本人已保存，未提交" + humanSceneSuffix(draft);
    }
    if (!submitted) {
      const status = review.status || "pending";
      if (status === "pending") return REVIEW_ZH.pending;
      return (REVIEW_ZH[status] || REVIEW_ZH.pending) + humanSceneSuffix(review) + " · 未提交";
    }
    return (REVIEW_ZH[review.status] || REVIEW_ZH.pending) + humanSceneSuffix(review);
  }

  function headline(metadata) {
    if (!metadata) return "来源场景未知 · 来源置信度未知 · 场景待核验";
    const sources = metadata.sources || [];
    const claim = metadata.claim_context || {};
    const primary = claimPrimary(metadata);
    const distinct = new Set(sources.map((item) => item.scene_code || "unknown")).size;
    let scenePart;
    let confPart;
    if (!sources.length && !primary) {
      scenePart = "来源场景未知";
      confPart = CONF_ZH.unknown;
    } else if (primary) {
      scenePart = primary.scene_label || sceneLabel(primary.scene_code);
      confPart = CONF_ZH[primary.confidence || claim.confidence] || CONF_ZH.unknown;
      if (distinct > 1) scenePart = scenePart + "（" + distinct + " 个来源场景）";
    } else {
      scenePart = sources.map((item) =>
        (item.scene_label || sceneLabel(item.scene_code)) + "·" + (CONF_ZH[item.confidence] || CONF_ZH.unknown)
      ).join("；");
      confPart = "来源置信度按场景";
    }
    return scenePart + " · " + confPart + " · " + reviewHeadlinePart(metadata);
  }

  function chipClass(metadata) {
    const primary = claimPrimary(metadata);
    const conf = (primary && primary.confidence) || "unknown";
    if (conf === "high" || conf === "medium" || conf === "low") return "metadata-chip " + conf;
    return "metadata-chip unknown";
  }

  function renderBanner(container, metadata) {
    if (!container) return;
    container.replaceChildren();
    if (!metadata) return;
    container.appendChild(el("div", {
      className: chipClass(metadata),
      text: headline(metadata),
      attrs: { "data-metadata-headline": "1" },
    }));
  }

  function appendSafeUrl(parent, url) {
    if (url == null || url === "") return;
    const text = String(url);
    if (/^https?:\/\//i.test(text)) {
      parent.appendChild(el("a", { href: text, text: text }));
      return;
    }
    parent.appendChild(el("div", { text: "链接（未使用）：" + text }));
  }

  function sourceBlock(source, kindLabel) {
    const block = el("div", {
      className: "metadata-source",
      attrs: { "data-source-kind": kindLabel || "source" },
    });
    block.appendChild(el("div", {
      className: "metadata-source-title",
      text: (source.scene_label || sceneLabel(source.scene_code)) + " · " +
        (CONF_ZH[source.confidence] || CONF_ZH.unknown),
    }));
    if (source.confidence_basis) {
      block.appendChild(el("div", { text: "依据：" + source.confidence_basis }));
    }
    if (source.video_id) block.appendChild(el("div", { text: "视频 ID：" + source.video_id }));
    if (source.batch_code) block.appendChild(el("div", { text: "批次：" + source.batch_code }));
    if (source.source_type) block.appendChild(el("div", { text: "来源类型：" + source.source_type }));
    if (source.channel_title) block.appendChild(el("div", { text: "频道：" + source.channel_title }));
    if (source.provider) block.appendChild(el("div", { text: "平台：" + source.provider }));
    appendSafeUrl(block, source.source_url);
    return block;
  }

  function reviewSummaryText(review) {
    if (!review) return REVIEW_ZH.pending;
    const status = REVIEW_ZH[review.status] || REVIEW_ZH.pending;
    const suffix = humanSceneSuffix(review);
    const actor = review.actor_kind === "admin" ? "（管理员）" :
      review.actor_kind === "annotator" ? "（标注员）" : "";
    const note = review.note ? " · " + review.note : "";
    return status + suffix + actor + note;
  }

  function renderReviewReference(container, metadata) {
    if (!container || !metadata || !metadata.reference_review) return;
    const box = el("aside", {
      className: "review-reference",
      attrs: { "data-review-kind": "reference" },
    });
    box.appendChild(el("div", {
      className: "review-reference-title",
      text: "上次已发布的核验（仅供参考，不是当前草稿；可能含提交后的管理员修正）",
    }));
    box.appendChild(el("div", { text: reviewSummaryText(metadata.reference_review) }));
    container.appendChild(box);
  }

  function renderDetails(container, metadata, options) {
    if (!container) return;
    container.replaceChildren();
    if (!metadata) return;
    options = options || {};
    const details = el("details", {
      className: "metadata-disclosure",
      attrs: { id: options.disclosureId || "metadataDisclosure" },
    });
    if (options.open) details.setAttribute("open", "");
    details.appendChild(el("summary", { text: options.summaryText || "来源、依据与核验详情" }));
    const body = el("div", { className: "metadata-disclosure-body" });
    body.appendChild(el("p", {
      className: "metadata-notice",
      text: metadata.notice || "来源判断尚未代表人工核验。",
    }));

    const claim = metadata.claim_context || {};
    if (claim.historical) {
      body.appendChild(el("h3", { text: "领取时的来源（当时依据，之后已更新）" }));
      if (claim.source) body.appendChild(sourceBlock(claim.source, "historical"));
      else {
        body.appendChild(el("p", {
          text: sceneLabel(claim.scene_code) + " · " + (CONF_ZH[claim.confidence] || CONF_ZH.unknown),
        }));
      }
      body.appendChild(el("h3", { text: "当前来源" }));
      const current = metadata.sources || [];
      if (!current.length) body.appendChild(el("p", { text: "当前没有匹配的来源证据。" }));
      current.forEach((source) => body.appendChild(sourceBlock(source, "current")));
    } else {
      body.appendChild(el("h3", { text: "来源证据" }));
      const sources = metadata.sources || [];
      if (!sources.length) {
        body.appendChild(el("p", { text: "来源场景未知 / 来源置信度未知。旧任务没有来源证据。" }));
      } else {
        sources.forEach((source) => body.appendChild(sourceBlock(source, "source")));
      }
    }

    body.appendChild(el("h3", { text: "模型分类" }));
    if (metadata.prediction) {
      const pred = metadata.prediction;
      const predLine = (pred.predicted_label || pred.predicted_scene_code || "未分类") +
        (pred.predicted_scene_code ? "（" + sceneLabel(pred.predicted_scene_code) + "）" : "");
      body.appendChild(el("div", { attrs: { "data-kind": "model" }, text: predLine }));
      if (pred.model_name) body.appendChild(el("div", { text: "模型：" + pred.model_name }));
      if (pred.created_at) body.appendChild(el("div", { text: "生成时间：" + pred.created_at }));
      if (pred.stale) {
        body.appendChild(el("p", { text: "模型预测基于旧转写版本，不是当前人工结论。" }));
      }
    } else {
      body.appendChild(el("p", { text: "没有模型分类。" }));
    }

    body.appendChild(el("h3", { text: "人工核验" }));
    const working = metadata.draft_review || metadata.scene_review;
    const published = metadata.scene_review;
    if (metadata.draft_review) {
      body.appendChild(el("div", {
        attrs: { "data-review-kind": "working" },
        text: "当前草稿：" + reviewSummaryText(metadata.draft_review) +
          (metadata.draft_review.submitted ? "" : " · 未提交"),
      }));
      if (metadata.reference_review) {
        body.appendChild(el("div", {
          attrs: { "data-review-kind": "reference" },
          text: "上次已发布：" + reviewSummaryText(metadata.reference_review),
        }));
      }
    } else if (published && published.submitted) {
      body.appendChild(el("div", {
        attrs: { "data-review-kind": "published" },
        text: "已提交：" + reviewSummaryText(published),
      }));
    } else if (working) {
      body.appendChild(el("div", {
        attrs: { "data-review-kind": "working" },
        text: "工作草稿：" + reviewSummaryText(working) +
          (working.submitted ? "" : " · 未提交"),
      }));
    } else {
      body.appendChild(el("p", { text: REVIEW_ZH.pending }));
    }

    details.appendChild(body);
    container.appendChild(details);
  }

  function reviewPayload(status, codes, note) {
    return { status: status, scene_codes: codes || [], note: note || "" };
  }

  function validateReview(review) {
    if (!review) return { ok: true, error: "" };
    const status = review.status || "pending";
    const codes = Array.isArray(review.scene_codes) ? review.scene_codes : [];
    if (status === "confirmed" && codes.length !== 1) {
      return { ok: false, error: "确认需要恰好选择一个场景。" };
    }
    if (status === "mixed" && codes.length < 2) {
      return { ok: false, error: "多场景需要至少选择两个场景。" };
    }
    if (status === "out_of_scope" && codes.length) {
      return { ok: false, error: "不属于九场景时不要选择场景标签。" };
    }
    if (status === "pending" && codes.length) {
      return { ok: false, error: "暂不核验时不要选择场景标签。" };
    }
    return { ok: true, error: "" };
  }

  function renderReviewControls(container, options) {
    options = options || {};
    const current = options.value || { status: "pending", scene_codes: [], note: "" };
    container.replaceChildren();
    if (options.disabled) {
      container.appendChild(el("h2", { text: "场景核验（当前为只读）" }));
      container.appendChild(el("p", {
        attrs: { "data-review-kind": "readonly" },
        text: reviewSummaryText(current),
      }));
      return current;
    }
    const state = {
      status: current.status || "pending",
      scene_codes: Array.isArray(current.scene_codes) ? current.scene_codes.slice() : [],
      note: current.note || "",
    };
    const heading = el("h2", { text: "场景核验（可随任务提交，非完成必填）" });
    const actions = el("div", { className: "scene-review-actions" });
    const validation = el("p", {
      className: "scene-review-validation",
      attrs: { id: "sceneReviewValidation", role: "status", "aria-live": "polite" },
    });
    const sceneBox = el("div", { className: "scene-review-scenes" });

    function syncValidation() {
      const result = validateReview(state);
      validation.textContent = result.error || "";
      validation.hidden = result.ok;
      container.classList.toggle("is-invalid", !result.ok);
      return result;
    }
    function emit() {
      syncValidation();
      if (typeof options.onChange === "function") {
        options.onChange(reviewPayload(state.status, state.scene_codes, state.note));
      }
    }
    function setStatus(value) {
      state.status = value;
      if (value === "pending" || value === "out_of_scope") state.scene_codes = [];
      Array.from(actions.children).forEach((child, index) => {
        child.setAttribute("aria-pressed", String(STATUS_BUTTONS[index][0] === state.status));
      });
      const showScenes = value === "confirmed" || value === "mixed" || value === "uncertain";
      sceneBox.hidden = !showScenes;
      Array.from(sceneBox.querySelectorAll("input[type=checkbox]")).forEach((box) => {
        box.checked = state.scene_codes.includes(box.value);
      });
      emit();
    }

    STATUS_BUTTONS.forEach(([value, label]) => {
      const button = el("button", {
        text: label,
        attrs: { type: "button", "aria-pressed": String(state.status === value) },
      });
      button.addEventListener("click", () => setStatus(value));
      actions.appendChild(button);
    });

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
      label.appendChild(document.createTextNode(scene.label_zh || sceneLabel(scene.code)));
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
    container.appendChild(validation);
    syncValidation();
    if (options.reference) renderReviewReference(container, { reference_review: options.reference });
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
    if (options.scopeText) {
      container.appendChild(el("p", { className: "metadata-notice", text: options.scopeText }));
    }
  }

  root.AnnotationMetadata = {
    headline, renderBanner, renderDetails, renderReviewControls, renderScenePicker,
    reviewPayload, validateReview, reviewSummaryText, renderReviewReference,
    sceneLabel, CONF_ZH, REVIEW_ZH, SCENE_ZH,
  };
})(window);
