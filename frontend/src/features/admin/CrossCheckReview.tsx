import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Drawer,
  Form,
  Input,
  Popconfirm,
  Select,
  Space,
  Tag,
  Tooltip,
} from "antd";
import {
  CloseOutlined,
  ReloadOutlined,
  LeftOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { ApiError } from "../../api/client";
import { QueryState, Status, WriteError } from "./AdminParts";
import { duration, label, useAdmin, useAdminQuery, useAdminWrite } from "./api";
import {
  crossDetailSchema,
  facetsSchema,
  type CrossDetail,
  type Segment,
} from "./schemas";
import type { Decision } from "./crossCheckModel";
import {
  copyManuscript,
  emptyReviewDraft,
  exportReviewDraft,
  loadReviewDraft,
  pickReviewSegment,
  removeReviewDraft,
  reviewDecisionBody,
  reviewRows,
  saveReviewDraft,
  unresolvedRows,
  type ReviewDraft,
  type ReviewRow,
} from "./crossCheckReviewModel";
import { CrossCheckPlayer, type ReviewPlayer } from "./CrossCheckPlayer";
import { CrossCheckSegments } from "./CrossCheckSegments";
import { CrossCheckSubmission } from "./CrossCheckSubmission";
import { UnsavedChangesGuard } from "./UnsavedChangesGuard";
import "./cross-check-review.css";

export function CrossCheckReview({
  roundId,
  personName,
  onBack,
  onNext,
  autoNext,
  onAutoNextChange,
}: {
  roundId: string;
  personName: (id: string | null) => string;
  onBack: () => void;
  onNext: () => Promise<void>;
  autoNext: boolean;
  onAutoNextChange: (value: boolean) => void;
}): React.JSX.Element {
  const query = useAdminQuery(
    `/api/admin/cross-checks/${encodeURIComponent(roundId)}`,
    crossDetailSchema,
  );
  const [retained, setRetained] = useState("");
  const { refreshGuard } = useAdmin();
  const { message } = App.useApp();
  const data = query.data;
  return (
    <Drawer
      open
      onClose={onBack}
      destroyOnHidden
      closable={false}
      size="min(1200px, 96vw)"
      rootClassName="cc-review-layer"
      className="cc-review-drawer"
      title={
        <div className="cc-review-heading">
          <div>
            <div className="cc-review-title">
              <span id="ccReviewTitle">
                {data?.filename ?? "Cross-check review"}
              </span>
              {data && <Status value={data.state} />}
              {data && data.state !== "awaiting_review" && <Tag>Read only</Tag>}
            </div>
            {data && (
              <p>
                A: {personName(data.original_annotator_id)} · B:{" "}
                {personName(data.secondary_annotator_id)} ·{" "}
                {duration(data.duration_seconds)} · Round {roundId.slice(0, 8)}
              </p>
            )}
          </div>
          <Space>
            <Button
              type="text"
              icon={<ReloadOutlined />}
              aria-label="Refresh review"
              loading={query.isFetching}
              onClick={() => {
                if (!refreshGuard.current || refreshGuard.current())
                  void query.refetch();
              }}
            />
            <Button
              type="text"
              id="ccBackToList"
              icon={<CloseOutlined />}
              aria-label="Close review"
              onClick={onBack}
            />
          </Space>
        </div>
      }
    >
      <div id="ccReviewPanel" className="cc-review-panel">
        {retained && (
          <section id="ccRetainedDraft" className="cc-retained-draft">
            <Alert
              type="warning"
              title="The round changed before your decision was saved."
              description="Your local draft is kept below for copying. It has not been published."
            />
            <Input.TextArea
              aria-label="Retained decision draft"
              readOnly
              value={retained}
              autoSize={{ minRows: 2, maxRows: 4 }}
            />
            <Space>
              <Button
                id="ccCopyRetainedDraft"
                onClick={() =>
                  void navigator.clipboard
                    .writeText(retained)
                    .then(() => message.success("Review draft copied."))
                    .catch(() =>
                      message.error(
                        "Clipboard unavailable. Use Export retained draft.",
                      ),
                    )
                }
              >
                Copy retained draft
              </Button>
              <Button onClick={() => exportReviewDraft(roundId, retained)}>
                Export retained draft
              </Button>
            </Space>
          </section>
        )}
        {retained &&
          data &&
          !["awaiting_review", "in_progress"].includes(data.state) && (
            <UnsavedChangesGuard
              dirty
              pending={false}
              onDiscard={() => setRetained("")}
            />
          )}
        <div id="ccReviewBody" className="cc-review-body">
          <QueryState query={query}>
            {data && (
              <ReviewWorkspace
                key={`${roundId}:${data.revision}`}
                data={data}
                personName={personName}
                onBack={onBack}
                onNext={onNext}
                autoNext={autoNext}
                onAutoNextChange={onAutoNextChange}
                onConflict={(body) => {
                  setRetained(
                    JSON.stringify({ round_id: roundId, ...body }, null, 2),
                  );
                  void query.refetch();
                  void message.warning(
                    "The round changed. Your local draft was retained.",
                  );
                }}
              />
            )}
          </QueryState>
        </div>
      </div>
    </Drawer>
  );
}

function ReviewWorkspace({
  data,
  personName,
  onBack,
  onNext,
  onConflict,
  autoNext,
  onAutoNextChange,
}: {
  data: CrossDetail;
  personName: (id: string | null) => string;
  onBack: () => void;
  onNext: () => Promise<void>;
  onConflict: (body: object) => void;
  autoNext: boolean;
  onAutoNextChange: (value: boolean) => void;
}): React.JSX.Element {
  const { identity } = useAdmin();
  const { message } = App.useApp();
  const editable = data.state === "awaiting_review";
  const [saved] = useState(() =>
    loadReviewDraft(identity.key_id, data.round_id),
  );
  const canRestore =
    saved &&
    editable &&
    saved.revision === data.revision &&
    saved.originalVersion === data.original_version_id &&
    saved.secondaryVersion === data.secondary_version_id;
  const [draft, setDraft] = useState<ReviewDraft>(() => {
    const initial = canRestore ? saved.draft : emptyReviewDraft();
    return {
      ...initial,
      editor: initial.editor ?? copyManuscript(data, "original"),
    };
  });
  const [savedDraft, setSavedDraft] = useState(draft);
  const [confirmed, setConfirmed] = useState(false);
  const [validation, setValidation] = useState("");
  const [onlyDifferences, setOnlyDifferences] = useState(false);
  const [follow, setFollow] = useState(true);
  const [rate, setRate] = useState(1);
  const rows = useMemo(() => reviewRows(data), [data]);
  const [active, setActive] = useState(
    () => unresolvedRows(rows, draft)[0]?.id ?? rows[0]?.id ?? "",
  );
  const [diffIndex, setDiffIndex] = useState(-1);
  const player = useRef<ReviewPlayer>(null);
  const write = useAdminWrite(
    `/api/admin/cross-checks/${encodeURIComponent(data.round_id)}/decision`,
  );
  const catalog = useAdminQuery("/api/admin/metadata/facets", facetsSchema);
  const locked = write.pending || write.frozen;
  const dirty =
    editable && JSON.stringify(draft) !== JSON.stringify(savedDraft);
  const remaining = unresolvedRows(rows, draft);
  const differences = (data.diff_ops ?? []).flatMap((op, index) =>
    op.op === "match" ? [] : [{ op, index }],
  );
  const activate = useCallback((id: string, scroll: boolean) => {
    setActive(id);
    if (scroll)
      document
        .getElementById(`ccSegment-${id}`)
        ?.scrollIntoView({ block: "nearest" });
  }, []);
  function change(next: ReviewDraft) {
    setDraft(next);
    setConfirmed(false);
    setValidation("");
  }
  function patchSegment(
    id: string,
    patch: Partial<Pick<Segment, "text" | "exclude_from_training">>,
  ) {
    if (!draft.editor || locked) return;
    change({
      ...draft,
      editor: {
        ...draft.editor,
        segments: draft.editor.segments.map((segment) =>
          String(segment.id) === id ? { ...segment, ...patch } : segment,
        ),
      },
      picks: { ...draft.picks, [id]: "edited" },
    });
  }
  function pick(row: ReviewRow, choice: Decision) {
    if (!editable || locked) return;
    activate(row.id, false);
    change(pickReviewSegment(data, draft, row, choice));
    if (choice === "edited")
      requestAnimationFrame(() =>
        document
          .querySelector<HTMLTextAreaElement>(
            `[data-review-segment="${row.id}"] textarea`,
          )
          ?.focus(),
      );
  }
  function undo(row: ReviewRow) {
    if (!draft.editor || locked) return;
    const base = data[`${draft.editor.base}_segments`].find(
      (segment) => String(segment.id) === row.id,
    );
    const picks = { ...draft.picks };
    delete picks[row.id];
    change({
      ...draft,
      picks,
      editor: {
        ...draft.editor,
        segments: draft.editor.segments.map((segment) =>
          String(segment.id) === row.id && base ? { ...base } : segment,
        ),
      },
    });
  }
  function playRow(row: ReviewRow) {
    activate(row.id, true);
    player.current?.playRange(row.start, row.end);
  }
  function playDifference(index: number) {
    const op = data.diff_ops?.[index];
    const mapping = op?.original ?? op?.secondary;
    if (!mapping) return;
    activate(String(mapping.segment_id), true);
    player.current?.playRange(mapping.start_s, mapping.end_s);
    setDiffIndex(differences.findIndex((item) => item.index === index));
    requestAnimationFrame(() =>
      document
        .querySelector(`[data-diff-index="${index}"]`)
        ?.scrollIntoView({ block: "nearest" }),
    );
  }
  function stepDifference(direction: number) {
    const index =
      diffIndex < 0
        ? direction > 0
          ? 0
          : differences.length - 1
        : (diffIndex + direction + differences.length) % differences.length;
    const difference = differences[index];
    if (difference) playDifference(difference.index);
  }
  function nextUnresolved() {
    const next =
      remaining.find(
        (row) =>
          row.start > (rows.find((item) => item.id === active)?.start ?? -1),
      ) ?? remaining[0];
    if (next) {
      setOnlyDifferences(false);
      activate(next.id, true);
      requestAnimationFrame(() => activate(next.id, true));
    }
  }
  function saveDraft(): boolean {
    try {
      saveReviewDraft(identity.key_id, data, draft);
      setSavedDraft(draft);
      void message.success("Review draft saved in this browser tab.");
      return true;
    } catch {
      void message.error(
        "The browser could not save this draft. Use Export draft to keep a copy.",
      );
      return false;
    }
  }
  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      if (
        event.defaultPrevented ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        event.isComposing
      )
        return;
      if (
        document.querySelector('.ant-modal-wrap:not([style*="display: none"])')
      )
        return;
      if (
        event.target instanceof HTMLElement &&
        event.target.closest(
          'input, textarea, select, [contenteditable="true"], [role="combobox"], [role="slider"]',
        )
      )
        return;
      const key = event.key.toLowerCase();
      if (
        key === " " &&
        event.target instanceof HTMLElement &&
        event.target.closest('button, [role="button"]')
      )
        return;
      const row = rows.find((item) => item.id === active);
      if (key === " ") {
        event.preventDefault();
        player.current?.toggle();
      } else if (key === "r" && row) playRow(row);
      else if (key === "n") nextUnresolved();
      else if (
        key === "j" ||
        key === "k" ||
        key === "arrowdown" ||
        key === "arrowup"
      ) {
        event.preventDefault();
        const visible = rows.filter(
          (item) => !onlyDifferences || item.different,
        );
        const index = visible.findIndex((item) => item.id === active);
        const next =
          visible[
            Math.max(
              0,
              Math.min(
                visible.length - 1,
                index + (key === "j" || key === "arrowdown" ? 1 : -1),
              ),
            )
          ];
        if (next) activate(next.id, true);
      } else if (row && editable && !locked && ["1", "2", "e"].includes(key)) {
        event.preventDefault();
        pick(
          row,
          key === "1" ? "original" : key === "2" ? "secondary" : "edited",
        );
      }
    }
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  });
  async function submit() {
    if (!draft.editor) {
      setValidation("Use A, use B, or edit a segment before submitting.");
      return;
    }
    if (!draft.reason.trim()) {
      setValidation("A reason is required.");
      return;
    }
    if (draft.editor.target === "annotated" && remaining.length) {
      setValidation(
        `Resolve ${remaining.length} remaining differing segments before submitting.`,
      );
      nextUnresolved();
      return;
    }
    if (draft.editor.target === "skipped" && !draft.editor.skipReasons.length) {
      setValidation("Select at least one skip reason.");
      return;
    }
    if (
      draft.editor.target === "annotated" &&
      draft.editor.segments.some(
        (segment) => !segment.text.trim() && !segment.exclude_from_training,
      )
    ) {
      setValidation("Add text or mark Bad quality for every segment.");
      return;
    }
    if (
      draft.override &&
      (!catalog.data ||
        catalog.isError ||
        (draft.review.status === "confirmed"
          ? draft.review.scene_codes.length !== 1
          : draft.review.status === "mixed"
            ? draft.review.scene_codes.length < 2
            : draft.review.scene_codes.length !== 0))
    ) {
      setValidation(
        "Choose valid human review scenes after the catalog loads.",
      );
      return;
    }
    setValidation("");
    if (!confirmed && !write.frozen) {
      setConfirmed(true);
      return;
    }
    const body = reviewDecisionBody(data, draft);
    const ok = await write.submit(body, (error) => {
      if (error instanceof ApiError && error.status === 409) {
        removeReviewDraft(identity.key_id, data.round_id);
        onConflict(body);
      }
    });
    if (ok) {
      player.current?.pause();
      removeReviewDraft(identity.key_id, data.round_id);
      void message.success("Cross-check decision saved.");
      if (autoNext) await onNext();
    }
  }
  const readOnlyResult = !["awaiting_review", "in_progress"].includes(
    data.state,
  );
  return (
    <div className="cc-review-workspace">
      {editable && (
        <UnsavedChangesGuard
          dirty={dirty}
          pending={locked}
          {...(!locked ? { onSave: saveDraft } : {})}
          onDiscard={() => {
            change(savedDraft);
          }}
        />
      )}
      <div className="cc-review-toolbar">
        <div className="cc-review-stats">
          <span className="cc-review-chip cc-review-chip-danger">
            Word difference{" "}
            <b>
              {data.word_difference_rate === null
                ? "Unavailable"
                : `${(data.word_difference_rate * 100).toFixed(1)}%`}
            </b>
          </span>
          <span className="cc-review-chip">
            Replace <b>{data.substitutions ?? "—"}</b>
          </span>
          <span className="cc-review-chip">
            Delete <b>{data.deletions ?? "—"}</b>
          </span>
          <span className="cc-review-chip">
            Insert <b>{data.insertions ?? "—"}</b>
          </span>
          <span className="cc-review-chip">
            Differing segments{" "}
            <b>
              {rows.filter((row) => row.different).length}/{rows.length}
            </b>
          </span>
        </div>
        <div className="cc-review-view-options">
          <Checkbox
            checked={onlyDifferences}
            onChange={(event) => setOnlyDifferences(event.target.checked)}
          >
            Only differences
          </Checkbox>
          <Checkbox
            checked={follow}
            onChange={(event) => setFollow(event.target.checked)}
          >
            Follow playback
          </Checkbox>
          <Select
            aria-label="Playback speed"
            value={rate}
            onChange={setRate}
            options={[0.75, 1, 1.25, 1.5, 2].map((value) => ({
              value,
              label: `${value}×`,
            }))}
          />
        </div>
      </div>
      <CrossCheckPlayer
        data={data}
        rows={rows}
        follow={follow}
        rate={rate}
        active={active}
        onActive={activate}
        playerRef={player}
      />
      <div className="cc-review-scroll">
        {saved && !canRestore && (
          <Alert
            type="warning"
            title="A saved draft belongs to an earlier revision. It has not been applied."
            action={
              <Button
                size="small"
                onClick={() => exportReviewDraft(data.round_id, saved.draft)}
              >
                Export saved draft
              </Button>
            }
          />
        )}
        {data.comparison_unavailable && (
          <Alert
            type="warning"
            title="Comparison unavailable"
            description={`${data.comparison_unavailable_reason ?? "The transcripts could not be compared"}. You can still listen and adjudicate.`}
          />
        )}
        {data.state === "in_progress" && (
          <Alert
            type="info"
            title="This cross-check is still in progress. The secondary transcript is not final and cannot be adjudicated yet."
          />
        )}
        {data.reason_codes.length > 0 && (
          <p className="cc-review-reasons">
            {data.reason_codes.map(label).join(" · ")}
          </p>
        )}
        <CrossCheckSegments
          data={data}
          rows={rows}
          draft={draft}
          active={active}
          onlyDifferences={onlyDifferences}
          locked={locked}
          personName={personName}
          onActive={activate}
          onPlay={playRow}
          onDifference={playDifference}
          onPick={pick}
          onUndo={undo}
          onPatch={patchSegment}
        />
        <Form
          id={editable ? "ccDecisionForm" : undefined}
          layout="vertical"
          disabled={locked || !editable}
          onFinish={() => void submit()}
          className="cc-review-decision"
        >
          <CrossCheckSubmission
            data={data}
            draft={draft}
            locked={locked}
            personName={personName}
            onChange={change}
          >
            {editable && (
              <>
                <Form.Item label="Reason for decision" required>
                  <Input.TextArea
                    id="ccDecisionReason"
                    rows={2}
                    maxLength={4000}
                    value={draft.reason}
                    onChange={(event) =>
                      change({ ...draft, reason: event.target.value })
                    }
                  />
                </Form.Item>
                {validation && (
                  <Alert role="alert" type="error" title={validation} />
                )}
                {confirmed && draft.editor && (
                  <Alert
                    id="ccDecisionSummary"
                    type="warning"
                    title="Confirm this decision: Edit and publish"
                    description={`Publish ${draft.editor.segments.length} segments as ${draft.editor.target}. This round will no longer block training export. Reason: ${draft.reason}`}
                  />
                )}
                <div id="ccDecisionError">
                  <WriteError error={write.error} frozen={write.frozen} />
                </div>
              </>
            )}
          </CrossCheckSubmission>
        </Form>
        {data.state === "in_progress" && <CancelForm data={data} />}
        {readOnlyResult && (
          <Card title="Review result">
            <Status value={data.state} />
            {data.state === "adjudicated" && <p>Adjudication is complete.</p>}
            <p>
              {data.decision
                ? `Decision: ${label(data.decision)}`
                : data.termination_reason}
            </p>
            <p>{data.decision_reason}</p>
          </Card>
        )}
      </div>
      <footer className="cc-review-footer">
        <div className="cc-review-legend">
          <span>
            <i className="cc-diff-replace" />
            Replace
          </span>
          <span>
            <i className="cc-diff-delete" />
            Delete
          </span>
          <span>
            <i className="cc-diff-insert" />
            Insert
          </span>
          <Tooltip title="Space: play/pause · J/K: next/previous segment · N: next unresolved · 1/2: use A/B · E: edit · R: replay segment · Esc: close">
            <span tabIndex={0}>Keyboard shortcuts</span>
          </Tooltip>
        </div>
        <div className="cc-review-navigation">
          <Button
            id="ccPrevDiff"
            size="small"
            icon={<LeftOutlined />}
            aria-label="Previous difference"
            disabled={!differences.length}
            onClick={() => stepDifference(-1)}
          />
          <Button
            id="ccNextDiff"
            size="small"
            icon={<RightOutlined />}
            aria-label="Next difference"
            disabled={!differences.length}
            onClick={() => stepDifference(1)}
          />
          <span id="ccDiffPos">
            {diffIndex < 0 ? 0 : diffIndex + 1}/{differences.length}
          </span>
        </div>
        {editable ? (
          <>
            <Button
              type="text"
              size="small"
              onClick={nextUnresolved}
              disabled={!remaining.length}
              className="cc-review-progress"
            >
              {remaining.length
                ? `${remaining.length} unresolved`
                : "All differences resolved"}
            </Button>
            <Checkbox
              checked={autoNext}
              disabled={locked}
              onChange={(event) => onAutoNextChange(event.target.checked)}
            >
              Open next after saving
            </Checkbox>
            <Button
              id="ccCopyDraft"
              size="small"
              onClick={() =>
                void navigator.clipboard
                  .writeText(
                    JSON.stringify(
                      {
                        round_id: data.round_id,
                        ...reviewDecisionBody(data, draft),
                      },
                      null,
                      2,
                    ),
                  )
                  .then(() => message.success("Review draft copied."))
                  .catch(() =>
                    message.error("Clipboard unavailable. Use Export draft."),
                  )
              }
            >
              Copy draft
            </Button>
            <Button
              size="small"
              onClick={() =>
                exportReviewDraft(data.round_id, {
                  round_id: data.round_id,
                  ...reviewDecisionBody(data, draft),
                })
              }
            >
              Export draft
            </Button>
            <Button id="ccSaveDraft" disabled={locked} onClick={saveDraft}>
              Save draft
            </Button>
            <Button
              id="ccConfirmDecision"
              type="primary"
              loading={write.pending}
              disabled={write.pending}
              onClick={() => {
                void submit();
                requestAnimationFrame(() =>
                  document
                    .getElementById("ccDecisionReason")
                    ?.scrollIntoView({ block: "nearest" }),
                );
              }}
            >
              {write.frozen ? "Retry same decision" : "Confirm decision"}
            </Button>
          </>
        ) : (
          <Button onClick={onBack}>Close</Button>
        )}
      </footer>
    </div>
  );
}

function CancelForm({ data }: { data: CrossDetail }): React.JSX.Element {
  const [reason, setReason] = useState("");
  const write = useAdminWrite(
    `/api/admin/cross-checks/${encodeURIComponent(data.round_id)}/cancel`,
  );
  const locked = write.pending || write.frozen;
  return (
    <Card title="Cancel cross-check">
      <UnsavedChangesGuard dirty={!!reason.trim()} pending={locked} />
      <Form id="ccCancelForm" layout="vertical">
        <p>
          The secondary assignment will be released. Its draft will not be
          published.
        </p>
        <Form.Item label="Reason" required>
          <Input.TextArea
            id="ccCancelReason"
            value={reason}
            disabled={locked}
            onChange={(event) => setReason(event.target.value)}
            maxLength={4000}
          />
        </Form.Item>
        <WriteError error={write.error} frozen={write.frozen} />
        <Popconfirm
          title="Cancel this cross-check?"
          description="The secondary transcript will not be published."
          okText="Confirm cancellation"
          onConfirm={() =>
            write.submit({
              expected_revision: data.revision,
              reason: reason.trim(),
              confirm: true,
            })
          }
        >
          <Button
            id="ccCancelButton"
            danger
            disabled={!reason.trim()}
            loading={write.pending}
          >
            {write.frozen ? "Retry same cancellation" : "Cancel cross-check"}
          </Button>
        </Popconfirm>
      </Form>
    </Card>
  );
}
