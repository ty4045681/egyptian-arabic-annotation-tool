import type { ReactNode } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Radio,
  Select,
  Switch,
  Tag,
} from "antd";
import { CheckOutlined } from "@ant-design/icons";
import { duration, label, useAdminQuery } from "./api";
import {
  facetsSchema,
  taskDetailSchema,
  type CrossDetail,
  type SceneReview,
} from "./schemas";
import { copyManuscript, type ReviewDraft } from "./crossCheckReviewModel";
import {
  reviewForVerdict,
  sameValues,
  sceneReviewsAgree,
  sceneVerdict,
  type SceneVerdict,
} from "./crossCheckSubmissionModel";

export function CrossCheckSubmission({
  data,
  draft,
  locked,
  personName,
  onChange,
  children,
}: {
  data: CrossDetail;
  draft: ReviewDraft;
  locked: boolean;
  personName: (id: string | null) => string;
  onChange: (draft: ReviewDraft) => void;
  children: ReactNode;
}): React.JSX.Element {
  const editable = data.state === "awaiting_review";
  const catalog = useAdminQuery("/api/admin/metadata/facets", facetsSchema);
  const task = useAdminQuery(
    `/api/admin/annotations/${encodeURIComponent(data.task_id)}`,
    taskDetailSchema,
  );
  const sources =
    task.data?.source_history.filter((source) => source.is_current !== false) ??
    [];
  const sourceCodes = [
    ...new Set(
      sources.flatMap((source) =>
        source.scene_code ? [source.scene_code] : [],
      ),
    ),
  ];
  const sceneName = (code: string) =>
    catalog.data?.scenes.find((scene) => scene.code === code)?.label_en ??
    sources.find((source) => source.scene_code === code)?.scene_label ??
    label(code);
  const sourceLabel =
    sourceCodes.map(sceneName).join(", ") ||
    (task.isPending
      ? "Loading…"
      : task.isError
        ? "Unavailable"
        : "No source scene");
  const defaultEditor = copyManuscript(data, draft.editor?.base ?? "original");
  const editor = draft.editor ?? defaultEditor;
  const baseName = editor.base === "original" ? "A" : "B";
  const baseVersion = data[`${editor.base}_version_id`];
  const baseReview =
    (task.data?.display_version_id === baseVersion
      ? task.data.metadata?.scene_review
      : data[`${editor.base}_review`]) ?? null;
  const statusAgrees =
    data.original_target_status === data.secondary_target_status &&
    (data.original_target_status !== "skipped" ||
      sameValues(data.original_skip_reasons, data.secondary_skip_reasons));
  const agrees =
    statusAgrees &&
    sceneReviewsAgree(data.original_review, data.secondary_review);
  const statusChanged = editor.target !== defaultEditor.target;
  const reasonsChanged =
    editor.target === "skipped" &&
    !sameValues(editor.skipReasons, defaultEditor.skipReasons);
  const verdict = sceneVerdict(draft.review, sourceCodes);
  const singleSource =
    sourceCodes.length === 1 &&
    catalog.data?.scenes.some((scene) => scene.code === sourceCodes[0]);
  const sceneEditing = Boolean(
    catalog.data &&
      !catalog.isError &&
      !task.isError &&
      task.data?.metadata?.features.scene_review_write,
  );
  const choices =
    catalog.data?.scenes.map((scene) => ({
      value: scene.code,
      label: scene.label_en,
    })) ?? [];
  const inheritedReview = baseReview ?? {
    status: "pending",
    scene_codes: [],
    note: "",
  };
  const neitherReviewed = [data.original_review, data.secondary_review].every(
    (review) => !review || review.status === "pending",
  );
  const sourceHint = task.isPending
    ? "Loading the source scene."
    : task.isError
      ? "The source scene could not be loaded."
      : sourceCodes.length
        ? `Currently using the source scene: ${sourceLabel}.`
        : "No source scene is available.";
  const sceneHint =
    !baseReview || baseReview.status === "pending"
      ? `${neitherReviewed ? "Neither annotator reviewed the scene." : `No scene review from ${baseName}.`} ${sourceHint}`
      : `Currently using the scene review from ${baseName}: ${reviewText(baseReview)}.`;
  const changes: string[] = [];
  if (statusChanged)
    changes.push(
      `Status: ${label(defaultEditor.target)} → ${label(editor.target)}`,
    );
  if (reasonsChanged)
    changes.push(
      `Skip reasons: ${editor.skipReasons.map(label).join(", ") || "Select a reason"}`,
    );
  if (draft.override)
    changes.push(
      `Scene review: ${reviewText(inheritedReview)} → ${reviewText(draft.review)}`,
    );
  const published = Boolean(
    data.final_version_id &&
      task.data?.display_version_id === data.final_version_id,
  );
  const finalSide =
    data.decision === "original" || data.decision === "secondary"
      ? data.decision
      : null;
  const hasFinal = editable || published || finalSide !== null;
  const missingFinal = data.final_version_id
    ? task.isFetching
      ? "Loading result…"
      : "Result details unavailable"
    : "Not decided";
  const finalStatus = editable
    ? editor.target
    : published
      ? task.data?.status
      : finalSide
        ? data[`${finalSide}_target_status`]
        : null;
  const finalReview = editable
    ? draft.override
      ? draft.review
      : baseReview
    : published
      ? (task.data?.metadata?.scene_review ?? null)
      : finalSide
        ? data[`${finalSide}_review`]
        : null;
  const finalSceneOrigin =
    editable && draft.override
      ? verdict === "match"
        ? "Confirmed by admin"
        : verdict === "reassign"
          ? "Reassigned by admin"
          : "Changed by admin"
      : !finalReview || finalReview.status === "pending"
        ? "Source scene"
        : editable
          ? `Kept from ${baseName}`
          : "Published review";

  function reviewText(review: SceneReview | null): string {
    if (!review || review.status === "pending") return "Not reviewed";
    if (review.status === "confirmed")
      return review.scene_codes.map(sceneName).join(", ") || "Select a scene";
    if (review.status === "mixed")
      return `Multiple scenes${review.scene_codes.length ? `: ${review.scene_codes.map(sceneName).join(", ")}` : " (select at least two)"}`;
    if (review.status === "uncertain") return "Cannot determine";
    if (review.status === "out_of_scope") return "Outside the supported scenes";
    return label(review.status);
  }

  function updateReview(review: SceneReview) {
    onChange({ ...draft, editor, review });
  }

  function reset() {
    onChange({
      ...draft,
      editor: {
        ...editor,
        target: defaultEditor.target,
        skipReasons: [...defaultEditor.skipReasons],
      },
      override: false,
      review: inheritedReview,
    });
  }

  return (
    <section
      id="ccSubmissionReview"
      className="cc-submission-panel"
      aria-labelledby="ccSubmissionTitle"
    >
      <header className="cc-submission-heading">
        <div>
          <h3 id="ccSubmissionTitle">Submission status &amp; scene review</h3>
          <p>
            {data.filename} · Round {data.round_id.slice(0, 8)} · Source scene:{" "}
            {sourceLabel}
          </p>
        </div>
        <Tag
          color={
            !data.secondary_target_status ? "blue" : agrees ? "green" : "gold"
          }
          icon={agrees ? <CheckOutlined /> : undefined}
        >
          {!data.secondary_target_status
            ? "Awaiting B"
            : agrees
              ? "A and B agree"
              : "A and B differ"}
        </Tag>
      </header>
      <section
        className="cc-submission-section"
        aria-labelledby="ccComparisonTitle"
      >
        <h4 id="ccComparisonTitle">Comparison</h4>
        <table
          className="cc-submission-comparison"
          aria-label="Submission and scene comparison"
        >
          <thead>
            <tr>
              <th scope="col">
                <span className="cc-sr-only">Field</span>
              </th>
              <th scope="col">A · {personName(data.original_annotator_id)}</th>
              <th scope="col">B · {personName(data.secondary_annotator_id)}</th>
              <th scope="col">Final</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Submission</th>
              {(["original", "secondary"] as const).map((side) => (
                <td key={side} data-submission-side={side}>
                  <Tag
                    color={
                      data[`${side}_target_status`] === "annotated"
                        ? "green"
                        : data[`${side}_target_status`] === "skipped"
                          ? "orange"
                          : "default"
                    }
                  >
                    {label(data[`${side}_target_status`] ?? "Not submitted")}
                  </Tag>
                  {data[`${side}_skip_reasons`].length > 0 && (
                    <small>
                      {data[`${side}_skip_reasons`].map(label).join(", ")}
                    </small>
                  )}
                </td>
              ))}
              <td
                className="cc-submission-final"
                data-final-status
                aria-live="polite"
              >
                {finalStatus ? (
                  <>
                    <Tag color={finalStatus === "skipped" ? "orange" : "green"}>
                      {label(finalStatus)}
                    </Tag>
                    <small
                      className={
                        editable && (statusChanged || reasonsChanged)
                          ? "cc-submission-changed"
                          : ""
                      }
                    >
                      {editable
                        ? statusChanged || reasonsChanged
                          ? "Changed by admin"
                          : statusAgrees
                            ? "Kept from A & B"
                            : `Kept from ${baseName}`
                        : "Published result"}
                    </small>
                  </>
                ) : (
                  <span className="cc-submission-empty">{missingFinal}</span>
                )}
                {editable && editor.target === "skipped" && (
                  <small>
                    {editor.skipReasons.map(label).join(", ") ||
                      "Select a skip reason"}
                  </small>
                )}
              </td>
            </tr>
            <tr>
              <th scope="row">Scene review</th>
              {(["original", "secondary"] as const).map((side) => {
                const review = data[`${side}_review`];
                return (
                  <td key={side} data-scene-side={side}>
                    <span
                      className={
                        !review || review.status === "pending"
                          ? "cc-submission-empty"
                          : ""
                      }
                    >
                      {reviewText(review)}
                    </span>
                    {(!review || review.status === "pending") && (
                      <small>
                        {sourceCodes.length
                          ? "Using source scene"
                          : "No source scene available"}
                      </small>
                    )}
                    {review?.note && (
                      <small className="cc-submission-note" dir="auto">
                        {review.note}
                      </small>
                    )}
                  </td>
                );
              })}
              <td
                className="cc-submission-final"
                data-final-scene
                aria-live="polite"
              >
                {hasFinal ? (
                  <>
                    <span>
                      {!finalReview || finalReview.status === "pending"
                        ? sourceLabel
                        : reviewText(finalReview)}
                    </span>
                    <small
                      className={
                        editable && draft.override
                          ? "cc-submission-changed"
                          : ""
                      }
                    >
                      {finalSceneOrigin}
                    </small>
                    {finalReview?.note && (
                      <small className="cc-submission-note" dir="auto">
                        {finalReview.note}
                      </small>
                    )}
                  </>
                ) : (
                  <span className="cc-submission-empty">{missingFinal}</span>
                )}
              </td>
            </tr>
          </tbody>
        </table>
        {task.isError && (
          <Alert
            type="warning"
            title="Source and published review details are unavailable."
            action={
              <Button onClick={() => void task.refetch()}>Retry details</Button>
            }
          />
        )}
      </section>
      {editable && (
        <section
          className="cc-submission-section cc-submission-decision"
          aria-labelledby="ccYourDecisionTitle"
        >
          <div className="cc-submission-section-heading">
            <h4 id="ccYourDecisionTitle">Your decision</h4>
            {changes.length > 0 && (
              <Button
                type="link"
                size="small"
                disabled={locked}
                onClick={reset}
              >
                Reset to defaults
              </Button>
            )}
          </div>
          <div id="ccEditor">
            <div id="ccEditorFields">
              <fieldset>
                <legend>Final submission status</legend>
                <Radio.Group
                  className="cc-final-status"
                  aria-label="Final submission status"
                  optionType="button"
                  value={editor.target}
                  disabled={locked}
                  onChange={(event) => {
                    const value: unknown = event.target.value;
                    if (value === "annotated" || value === "skipped")
                      onChange({
                        ...draft,
                        editor: { ...editor, target: value },
                      });
                  }}
                  options={[
                    { value: "annotated", label: "Annotated" },
                    { value: "skipped", label: "Skipped" },
                  ]}
                />
                <p className="cc-submission-hint">
                  Default: <b>{label(defaultEditor.target)}</b> (
                  {statusAgrees
                    ? "both annotators agree"
                    : `from ${baseName}; A and B differ`}
                  ).
                </p>
                {editor.target === "skipped" && (
                  <div className="cc-skip-details">
                    <Alert
                      id="ccSkipWarning"
                      showIcon
                      type="warning"
                      title={`Skipped excludes the whole audio (${duration(data.duration_seconds)}) from training exports.`}
                      description="Segment choices stay in your draft but are not used for training exports."
                    />
                    <Form.Item label="Skip reasons" required>
                      <Checkbox.Group
                        aria-label="Skip reasons"
                        value={editor.skipReasons}
                        disabled={locked}
                        onChange={(skipReasons) =>
                          onChange({
                            ...draft,
                            editor: { ...editor, skipReasons },
                          })
                        }
                        options={[
                          { value: "noisy", label: "Noisy" },
                          { value: "not_egyptian", label: "Not Egyptian" },
                          { value: "poor_quality", label: "Poor quality" },
                        ]}
                      />
                    </Form.Item>
                  </div>
                )}
              </fieldset>
              <fieldset>
                <div className="cc-scene-switch-row">
                  <div>
                    <span id="ccOverrideLegend" className="cc-scene-legend">
                      Override scene review
                    </span>
                    <p className="cc-submission-hint">{sceneHint}</p>
                  </div>
                  <Switch
                    id="ccSceneOverride"
                    aria-labelledby="ccOverrideLegend"
                    aria-controls="ccSceneOverrideBody"
                    checked={draft.override}
                    disabled={locked || !sceneEditing}
                    onChange={(override) => {
                      const previous =
                        draft.review.status === "pending"
                          ? inheritedReview
                          : draft.review;
                      const review =
                        previous.status === "pending"
                          ? reviewForVerdict(
                              draft.review,
                              singleSource ? "match" : "reassign",
                              sourceCodes,
                            )
                          : previous;
                      onChange({
                        ...draft,
                        editor,
                        override,
                        review: override ? review : draft.review,
                      });
                    }}
                  />
                </div>
                {draft.override && (
                  <div
                    id="ccSceneOverrideBody"
                    className="cc-scene-override-body"
                  >
                    <Form.Item label="Scene verdict" htmlFor="ccSceneVerdict">
                      <Select<SceneVerdict>
                        id="ccSceneVerdict"
                        aria-label="Scene verdict"
                        value={verdict}
                        disabled={locked || !sceneEditing}
                        onChange={(value) =>
                          updateReview(
                            reviewForVerdict(draft.review, value, sourceCodes),
                          )
                        }
                        options={[
                          {
                            value: "match",
                            label: "Matches the source scene",
                            disabled: !singleSource,
                          },
                          {
                            value: "reassign",
                            label: "Doesn't match — reassign",
                          },
                          { value: "mixed", label: "Multiple scenes" },
                          { value: "uncertain", label: "Cannot determine" },
                          {
                            value: "out_of_scope",
                            label: "Outside the supported scenes",
                          },
                          { value: "pending", label: "Not reviewed" },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item label="Final scene" htmlFor="ccFinalScene">
                      {draft.review.status === "mixed" ? (
                        <Select<string[]>
                          id="ccFinalScene"
                          aria-label="Final scene"
                          mode="multiple"
                          value={draft.review.scene_codes}
                          options={choices}
                          disabled={locked || !sceneEditing}
                          onChange={(scene_codes) =>
                            updateReview({ ...draft.review, scene_codes })
                          }
                        />
                      ) : (
                        <Select<string>
                          id="ccFinalScene"
                          aria-label="Final scene"
                          value={draft.review.scene_codes[0] ?? null}
                          placeholder={
                            verdict === "reassign"
                              ? "Select a scene"
                              : "No scene assigned"
                          }
                          options={choices}
                          disabled={
                            locked || !sceneEditing || verdict !== "reassign"
                          }
                          onChange={(code) =>
                            updateReview({
                              ...draft.review,
                              scene_codes: [code],
                            })
                          }
                        />
                      )}
                    </Form.Item>
                    <Form.Item
                      className="cc-scene-review-note"
                      label="Review note (optional)"
                      htmlFor="ccSceneReviewNote"
                    >
                      <Input.TextArea
                        id="ccSceneReviewNote"
                        value={draft.review.note}
                        rows={2}
                        maxLength={4000}
                        disabled={locked || !sceneEditing}
                        onChange={(event) =>
                          updateReview({
                            ...draft.review,
                            note: event.target.value,
                          })
                        }
                      />
                    </Form.Item>
                  </div>
                )}
                {catalog.isError && (
                  <Alert
                    type="warning"
                    title="Scene catalog unavailable; a review override cannot be submitted."
                    action={
                      <Button onClick={() => void catalog.refetch()}>
                        Retry catalog
                      </Button>
                    }
                  />
                )}
                {task.data?.metadata?.features.scene_review_write === false && (
                  <p className="cc-submission-hint">
                    Scene review editing is disabled.
                  </p>
                )}
              </fieldset>
            </div>
          </div>
          {children}
          {changes.length > 0 && (
            <div className="cc-submission-changes" aria-live="polite">
              <p>Pending changes</p>
              <ul>
                {changes.map((text) => (
                  <li key={text}>{text}</li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}
    </section>
  );
}
