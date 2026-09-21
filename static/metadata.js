/* Shared scene provenance UI helpers. Safe text via textContent; links are http(s) only. */
(function (root) {
  "use strict";

  const CONF_LABEL = {
    high: "Source confidence High", medium: "Source confidence Medium",
    low: "Source confidence Low", unknown: "Source confidence Unknown",
  };
  const REVIEW_LABEL = {
    pending: "Scene review pending", confirmed: "Scene confirmed",
    mixed: "Multiple scenes",
    out_of_scope: "Outside the ten scenes", uncertain: "Cannot determine",
  };
  const SCENE_LABEL = {
    restaurant: "Restaurant", hotel: "Hotel", taxi: "Taxi", airport: "Airport",
    clinic: "Clinic", tourism_information: "Tourism information",
    emergencies: "Emergencies", spoken_languages: "Spoken languages",
    business_negotiation: "Business negotiation", shopping: "Shopping",
  };
  const STATUS_BUTTONS = [
    ["confirmed", "Confirm"],
    ["mixed", "Mixed / multiple scenes"],
    ["out_of_scope", "Outside the ten scenes"],
    ["uncertain", "Cannot determine"],
    ["pending", "Skip review for now"],
  ];
  const STATUS_META = {
    confirmed: { title: "Confirm scene", detail: "Choose one scene", icon: "✓", tone: "confirmed", badge: "Choose one scene" },
    mixed: { title: "Multiple scenes", detail: "Choose two or more", icon: "≋", tone: "mixed", badge: "Choose 2+ scenes" },
    out_of_scope: { title: "Outside the list", detail: "No matching scene", icon: "↗", tone: "out_of_scope", badge: "Outside the list" },
    uncertain: { title: "Needs another look", detail: "Keep it uncertain", icon: "?", tone: "uncertain", badge: "Needs review" },
    pending: { title: "Skip for now", detail: "You can revisit it later", icon: "→", tone: "pending", badge: "Not reviewed" },
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

  function sceneDisplayLabel(scene) {
    if (!scene) return sceneLabel(null);
    return scene.label_en || scene.label || scene.label_zh || sceneLabel(scene.code);
  }

  function sceneLabel(code) {
    if (!code || code === "unknown") return "Spoken languages";
    return SCENE_LABEL[code] || code;
  }

  function humanSceneSuffix(review) {
    const codes = (review && review.scene_codes) || [];
    if (!codes.length) return "";
    return " (" + codes.map(sceneLabel).join(", ") + ")";
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
        return REVIEW_LABEL.pending;
      }
      return "Saved, not submitted" + humanSceneSuffix(draft);
    }
    if (!submitted) {
      const status = review.status || "pending";
      if (status === "pending") return REVIEW_LABEL.pending;
      return (REVIEW_LABEL[status] || REVIEW_LABEL.pending) + humanSceneSuffix(review) + " · not submitted";
    }
    return (REVIEW_LABEL[review.status] || REVIEW_LABEL.pending) + humanSceneSuffix(review);
  }

  function headline(metadata) {
    if (!metadata) return "Spoken languages · Source confidence Unknown · Scene review pending";
    const sources = metadata.sources || [];
    const claim = metadata.claim_context || {};
    let primary = claimPrimary(metadata);
    if (!primary && sources.length === 1) primary = sources[0];
    const distinct = new Set(sources.map((item) => item.scene_code || "spoken_languages")).size;
    let scenePart;
    let confPart;
    if (!sources.length && !primary) {
      const server = String(metadata.headline || "");
      const parts = server.split(" · ");
      if (parts.length >= 3) {
        return parts.slice(0, -1).join(" · ") + " · " + reviewHeadlinePart(metadata);
      }
      scenePart = "Spoken languages";
      confPart = CONF_LABEL.unknown;
    } else if (primary) {
      scenePart = primary.scene_label || sceneLabel(primary.scene_code);
      confPart = CONF_LABEL[primary.confidence || claim.confidence] || CONF_LABEL.unknown;
      if (distinct > 1) scenePart = scenePart + " (" + distinct + " source scenes)";
    } else {
      scenePart = sources.map((item) =>
        (item.scene_label || sceneLabel(item.scene_code)) + "·" + (CONF_LABEL[item.confidence] || CONF_LABEL.unknown)
      ).join("; ");
      confPart = "Source confidence by scene";
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
    parent.appendChild(el("div", { text: "Link (not used): " + text }));
  }

  function sourceBlock(source, kindLabel) {
    const block = el("div", {
      className: "metadata-source",
      attrs: { "data-source-kind": kindLabel || "source" },
    });
    block.appendChild(el("div", {
      className: "metadata-source-title",
      text: (source.scene_label || sceneLabel(source.scene_code)) + " · " +
        (CONF_LABEL[source.confidence] || CONF_LABEL.unknown),
    }));
    if (source.confidence_basis) {
      block.appendChild(el("div", { text: "Basis: " + source.confidence_basis }));
    }
    if (source.video_id) block.appendChild(el("div", { text: "Video ID: " + source.video_id }));
    if (source.batch_code) block.appendChild(el("div", { text: "Batch: " + source.batch_code }));
    if (source.source_type) block.appendChild(el("div", { text: "Source type: " + source.source_type }));
    if (source.channel_title) block.appendChild(el("div", { text: "Channel: " + source.channel_title }));
    if (source.provider) block.appendChild(el("div", { text: "Provider: " + source.provider }));
    appendSafeUrl(block, source.source_url);
    return block;
  }

  function reviewSummaryText(review) {
    if (!review) return REVIEW_LABEL.pending;
    const status = REVIEW_LABEL[review.status] || REVIEW_LABEL.pending;
    const suffix = humanSceneSuffix(review);
    const actor = review.actor_kind === "admin" ? " (administrator)" :
      review.actor_kind === "annotator" ? " (annotator)" : "";
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
      text: "Last published review (reference only, not the current draft; may include a later administrator correction)",
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
    details.appendChild(el("summary", { text: options.summaryText || "Source, basis, and review details" }));
    const body = el("div", { className: "metadata-disclosure-body" });
    body.appendChild(el("p", {
      className: "metadata-notice",
      text: metadata.notice || "Source classification is not a human review.",
    }));

    const claim = metadata.claim_context || {};
    if (claim.historical) {
      body.appendChild(el("h3", { text: "Source at claim time (the evidence used then; later updated)" }));
      if (claim.source) body.appendChild(sourceBlock(claim.source, "historical"));
      else {
        body.appendChild(el("p", {
          text: sceneLabel(claim.scene_code) + " · " + (CONF_LABEL[claim.confidence] || CONF_LABEL.unknown),
        }));
      }
      body.appendChild(el("h3", { text: "Current sources" }));
      const current = metadata.sources || [];
      if (!current.length) body.appendChild(el("p", { text: "No matching current source evidence." }));
      current.forEach((source) => body.appendChild(sourceBlock(source, "current")));
    } else {
      body.appendChild(el("h3", { text: "Source evidence" }));
      const sources = metadata.sources || [];
      if (!sources.length) {
        body.appendChild(el("p", { text: "Spoken languages · Source confidence Unknown. This older task has no source evidence." }));
      } else {
        sources.forEach((source) => body.appendChild(sourceBlock(source, "source")));
      }
    }

    body.appendChild(el("h3", { text: "Model classification" }));
    if (metadata.prediction) {
      const pred = metadata.prediction;
      const predLine = (pred.predicted_label || pred.predicted_scene_code || "Unclassified") +
        (pred.predicted_scene_code ? " (" + sceneLabel(pred.predicted_scene_code) + ")" : "");
      body.appendChild(el("div", { attrs: { "data-kind": "model" }, text: predLine }));
      if (pred.model_name) body.appendChild(el("div", { text: "Model: " + pred.model_name }));
      if (pred.created_at) body.appendChild(el("div", { text: "Generated at: " + pred.created_at }));
      if (pred.stale) {
        body.appendChild(el("p", { text: "This model prediction is based on an older transcript and is not the current human conclusion." }));
      }
    } else {
      body.appendChild(el("p", { text: "No model classification." }));
    }

    body.appendChild(el("h3", { text: "Human review" }));
    const working = metadata.draft_review || metadata.scene_review;
    const published = metadata.scene_review;
    if (metadata.draft_review) {
      body.appendChild(el("div", {
        attrs: { "data-review-kind": "working" },
        text: "Current draft: " + reviewSummaryText(metadata.draft_review) +
          (metadata.draft_review.submitted ? "" : " · not submitted"),
      }));
      if (metadata.reference_review) {
        body.appendChild(el("div", {
          attrs: { "data-review-kind": "reference" },
          text: "Last published: " + reviewSummaryText(metadata.reference_review),
        }));
      }
    } else if (published && published.submitted) {
      body.appendChild(el("div", {
        attrs: { "data-review-kind": "published" },
        text: "Submitted: " + reviewSummaryText(published),
      }));
    } else if (working) {
      body.appendChild(el("div", {
        attrs: { "data-review-kind": "working" },
        text: "Working draft: " + reviewSummaryText(working) +
          (working.submitted ? "" : " · not submitted"),
      }));
    } else {
      body.appendChild(el("p", { text: REVIEW_LABEL.pending }));
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
      return { ok: false, error: "Confirm requires exactly one scene." };
    }
    if (status === "mixed" && codes.length < 2) {
      return { ok: false, error: "Mixed scenes require at least two scene labels." };
    }
    if (status === "out_of_scope" && codes.length) {
      return { ok: false, error: "Do not select scene labels when marking outside the ten scenes." };
    }
    if (status === "pending" && codes.length) {
      return { ok: false, error: "Do not select scene labels when skipping review." };
    }
    return { ok: true, error: "" };
  }

  function renderReviewControls(container, options) {
    options = options || {};
    const current = options.value || { status: "pending", scene_codes: [], note: "" };
    container.replaceChildren();
    if (options.disabled) {
      container.appendChild(el("h2", { text: "Scene review (read-only)" }));
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
    const heading = el("div", { className: "scene-review-heading" });
    const title = el("div", { className: "scene-review-title" }, [
      el("p", { className: "scene-review-eyebrow", text: "OPTIONAL QUALITY CHECK" }),
      el("h2", { text: "Scene review" }),
    ]);
    const statusBadge = el("span", { className: "scene-review-status" });
    heading.appendChild(title);
    heading.appendChild(statusBadge);
    const intro = el("p", {
      className: "scene-review-intro",
      text: "Confirm the source scene when you are confident. You can finish the task without reviewing it.",
    });
    const actions = el("div", {
      className: "scene-review-actions",
      attrs: { role: "group", "aria-label": "Scene review result" },
    });
    const validation = el("p", {
      className: "scene-review-validation",
      attrs: { id: "sceneReviewValidation", role: "status", "aria-live": "polite" },
    });
    const sceneBox = el("div", { className: "scene-review-scenes" });
    const sceneHead = el("div", { className: "scene-review-section-head" }, [
      el("strong", { text: "Scene label(s)" }),
      el("span", { text: "One for Confirm · two or more for Multiple" }),
    ]);
    const sceneSection = el("div", { className: "scene-review-scene-section" }, [sceneHead, sceneBox]);

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
    function updateStatusUi() {
      const meta = STATUS_META[state.status] || STATUS_META.pending;
      const showScenes = state.status === "confirmed" || state.status === "mixed" || state.status === "uncertain";
      const selectedCount = showScenes ? state.scene_codes.length : 0;
      statusBadge.textContent = selectedCount
        ? selectedCount + (selectedCount === 1 ? " scene selected" : " scenes selected")
        : meta.badge;
      statusBadge.dataset.status = state.status;
      Array.from(actions.children).forEach((child, index) => {
        child.setAttribute("aria-pressed", String(STATUS_BUTTONS[index][0] === state.status));
      });
      sceneSection.hidden = !showScenes;
      sceneBox.hidden = !showScenes;
      Array.from(sceneBox.querySelectorAll("input[type=checkbox]")).forEach((box) => {
        box.checked = state.scene_codes.includes(box.value);
      });
    }
    function setStatus(value) {
      state.status = value;
      if (value === "pending" || value === "out_of_scope") state.scene_codes = [];
      updateStatusUi();
      emit();
    }

    STATUS_BUTTONS.forEach(([value, label]) => {
      const meta = STATUS_META[value] || STATUS_META.pending;
      const button = el("button", {
        className: "scene-review-choice tone-" + meta.tone,
        attrs: {
          type: "button",
          "aria-pressed": String(state.status === value),
          "aria-label": label,
          "data-review-status": value,
        },
      }, [
        el("span", { className: "scene-review-choice-icon", text: meta.icon, attrs: { "aria-hidden": "true" } }),
        el("span", { className: "scene-review-choice-copy", attrs: { "aria-hidden": "true" } }, [
          el("strong", { text: meta.title }),
          el("small", { text: meta.detail }),
        ]),
      ]);
      button.addEventListener("click", () => setStatus(value));
      actions.appendChild(button);
    });

    (options.scenes || []).forEach((scene) => {
      const label = el("label", { className: "scene-review-scene" });
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = scene.code;
      box.checked = state.scene_codes.includes(scene.code);
      box.addEventListener("change", () => {
        if (box.checked && !state.scene_codes.includes(scene.code)) state.scene_codes.push(scene.code);
        if (!box.checked) state.scene_codes = state.scene_codes.filter((code) => code !== scene.code);
        updateStatusUi();
        emit();
      });
      label.appendChild(box);
      label.appendChild(document.createTextNode(sceneDisplayLabel(scene)));
      sceneBox.appendChild(label);
    });

    const noteWrap = el("div", { className: "scene-review-note" });
    const noteLabel = el("label", { className: "scene-review-note-label", attrs: { for: "sceneReviewNote" } }, [
      el("strong", { text: "Reviewer note" }),
      el("span", { text: "Optional" }),
    ]);
    const note = document.createElement("textarea");
    note.id = "sceneReviewNote";
    note.setAttribute("aria-label", "Reviewer note (optional)");
    note.placeholder = "Add context for the next reviewer…";
    note.value = state.note;
    note.addEventListener("input", () => { state.note = note.value; emit(); });
    noteWrap.appendChild(noteLabel);
    noteWrap.appendChild(note);

    const footer = el("div", { className: "scene-review-footer" }, [
      validation,
      el("span", { className: "scene-review-save-hint", text: "Saved with your task after you choose a result." }),
    ]);

    container.appendChild(heading);
    container.appendChild(intro);
    container.appendChild(actions);
    container.appendChild(sceneSection);
    container.appendChild(noteWrap);
    container.appendChild(footer);
    updateStatusUi();
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
      el("span", { text: "All in scope" }),
      el("span", { className: "count", text: String((options.pool && options.pool.available) || 0) + " available" }),
    ]);
    allBtn.addEventListener("click", () => options.onChange && options.onChange(""));
    wrap.appendChild(allBtn);
    (options.scenes || []).forEach((scene) => {
      const button = el("button", { attrs: { type: "button", "aria-pressed": String(selected === scene.code) } }, [
        el("span", { text: sceneDisplayLabel(scene) }),
        el("span", { className: "count", text: String(counts[scene.code] || 0) + " available" }),
      ]);
      button.addEventListener("click", () => options.onChange && options.onChange(scene.code));
      wrap.appendChild(button);
    });
    container.appendChild(wrap);
    if (options.scopeText) {
      container.appendChild(el("p", { className: "metadata-notice", text: options.scopeText }));
    }
  }

  root.AnnotationMetadata = {
    headline, renderBanner, renderDetails, renderReviewControls, renderScenePicker,
    reviewPayload, validateReview, reviewSummaryText, renderReviewReference,
    sceneLabel, sceneDisplayLabel, CONF_LABEL, REVIEW_LABEL, SCENE_LABEL,
    CONF_ZH: CONF_LABEL, REVIEW_ZH: REVIEW_LABEL, SCENE_ZH: SCENE_LABEL,
  };
})(window);
