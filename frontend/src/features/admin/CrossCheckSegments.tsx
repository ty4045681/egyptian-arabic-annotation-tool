import { Button, Checkbox, Empty, Input, Tag } from "antd";
import { CaretRightOutlined, UndoOutlined } from "@ant-design/icons";
import type { CrossDetail, Segment } from "./schemas";
import { highlightParts, type Decision } from "./crossCheckModel";
import type { ReviewDraft, ReviewRow } from "./crossCheckReviewModel";

export function CrossCheckSegments({
  data,
  rows,
  draft,
  active,
  onlyDifferences,
  locked,
  personName,
  onActive,
  onPlay,
  onDifference,
  onPick,
  onUndo,
  onPatch,
}: {
  data: CrossDetail;
  rows: ReviewRow[];
  draft: ReviewDraft;
  active: string;
  onlyDifferences: boolean;
  locked: boolean;
  personName: (id: string | null) => string;
  onActive: (id: string, scroll: boolean) => void;
  onPlay: (row: ReviewRow) => void;
  onDifference: (index: number) => void;
  onPick: (row: ReviewRow, choice: Decision) => void;
  onUndo: (row: ReviewRow) => void;
  onPatch: (
    id: string,
    patch: Partial<Pick<Segment, "text" | "exclude_from_training">>,
  ) => void;
}): React.JSX.Element {
  const visible = rows.filter((row) => !onlyDifferences || row.different);
  const readOnly = data.state !== "awaiting_review";
  const ops = data.diff_ops ?? [];
  return (
    <div className="cc-review-segments" id="ccReviewSegments">
      {!visible.length && <Empty description="No differing segments." />}
      {visible.map((row) => {
        const editedIndex =
          draft.editor?.segments.findIndex(
            (segment) => String(segment.id) === row.id,
          ) ?? -1;
        const edited = draft.editor?.segments[editedIndex];
        const pick = draft.picks[row.id] ?? null;
        const final =
          pick === "original" || pick === "secondary"
            ? row[pick]
            : pick === "edited"
              ? edited
              : row.different
                ? null
                : row.original;
        const isEditing = Boolean(
          edited && pick === "edited",
        );
        const counts = { replace: 0, delete: 0, insert: 0 };
        for (const index of row.differences) {
          const kind = ops[index]?.op;
          if (kind === "replace" || kind === "delete" || kind === "insert")
            counts[kind]++;
        }
        const canEdit =
          (draft.editor?.base === "secondary"
            ? row.secondary
            : row.original) !== null;
        return (
          <article
            key={row.id}
            id={`ccSegment-${row.id}`}
            data-review-segment={row.id}
            className={`cc-review-segment ${row.different ? "cc-review-different" : ""} ${pick ? "cc-review-resolved" : ""} ${active === row.id ? "cc-review-active" : ""}`}
            onPointerDown={() => onActive(row.id, false)}
          >
            <header className="cc-segment-heading">
              <span className="cc-segment-index">#{row.id}</span>
              <Button
                size="small"
                icon={<CaretRightOutlined />}
                aria-label={`Play segment ${row.id}`}
                onClick={() => onPlay(row)}
              >
                {row.start.toFixed(2)}–{row.end.toFixed(2)}s
              </Button>
              {row.different ? (
                <Tag color="red">
                  Replace {counts.replace} · Delete {counts.delete} · Insert{" "}
                  {counts.insert}
                </Tag>
              ) : (
                <Tag>Matching</Tag>
              )}
              {row.original?.exclude_from_training !==
                row.secondary?.exclude_from_training && (
                <Tag color="orange">Quality differs</Tag>
              )}
              {!readOnly && row.different && (
                <Tag color={pick ? "green" : "orange"}>
                  {pick
                    ? pick === "edited"
                      ? "Edited"
                      : `Use ${pick === "original" ? "A" : "B"}`
                    : "Unresolved"}
                </Tag>
              )}
            </header>
            <div className="cc-segment-pair">
              {(["original", "secondary"] as const).map((side) => {
                const segment = row[side];
                return (
                  <section
                    key={side}
                    data-cc-side={side}
                    className={`cc-segment-copy ${pick === side ? "cc-segment-picked" : ""}`}
                  >
                    <header>
                      <span>
                        {side === "original"
                          ? "A · Original"
                          : "B · Cross-check"}{" "}
                        · <bdi>{personName(data[`${side}_annotator_id`])}</bdi>
                      </span>
                      {segment?.exclude_from_training && (
                        <Tag color="red">Bad quality</Tag>
                      )}
                    </header>
                    {segment ? (
                      <p className="admin-transcript cc-transcript" dir="auto">
                        {highlightParts(segment, side, ops).map(
                          (part, index) =>
                            part.changed ? (
                              <mark
                                key={index}
                                className={`cc-mark cc-diff-${part.index === null ? "replace" : (ops[part.index]?.op ?? "replace")}`}
                                data-diff-index={part.index ?? undefined}
                                role="button"
                                tabIndex={0}
                                aria-label="Play difference"
                                onClick={() => {
                                  if (part.index !== null)
                                    onDifference(part.index);
                                }}
                                onKeyDown={(event) => {
                                  if (
                                    (event.key === "Enter" ||
                                      event.key === " ") &&
                                    part.index !== null
                                  ) {
                                    event.preventDefault();
                                    event.stopPropagation();
                                    onDifference(part.index);
                                  }
                                }}
                              >
                                {part.text}
                              </mark>
                            ) : (
                              <span key={index}>{part.text}</span>
                            ),
                        )}
                        {!segment.text && (
                          <span className="cc-empty-transcript">
                            Empty transcript
                          </span>
                        )}
                      </p>
                    ) : (
                      <p className="admin-muted">No corresponding segment.</p>
                    )}
                  </section>
                );
              })}
            </div>
            {!row.compatible && (
              <p className="cc-segment-warning">
                Segment boundaries differ. Edit the available starting
                segment.
              </p>
            )}
            {!readOnly && (
              <div className="cc-segment-verdict">
                <div className="cc-segment-actions">
                  {(["original", "secondary"] as const).map((side) => (
                    <Button
                      key={side}
                      size="small"
                      className={pick === side ? "cc-choice-selected" : ""}
                      disabled={locked || !row.compatible || !canEdit}
                      onClick={() => onPick(row, side)}
                    >
                      Use {side === "original" ? "A" : "B"}
                      <kbd>{side === "original" ? "1" : "2"}</kbd>
                    </Button>
                  ))}
                  <Button
                    size="small"
                    disabled={locked || !canEdit}
                    className={isEditing ? "cc-choice-selected" : ""}
                    onClick={() => onPick(row, "edited")}
                  >
                    Edit<kbd>E</kbd>
                  </Button>
                  {draft.picks[row.id] && (
                    <Button
                      size="small"
                      type="text"
                      aria-label="Undo"
                      icon={<UndoOutlined />}
                      disabled={locked}
                      onClick={() => onUndo(row)}
                    >
                      Undo
                    </Button>
                  )}
                </div>
                {!isEditing && (
                  <div className="cc-segment-final">
                    <small>Final</small>
                    <span dir="auto">
                      {final?.exclude_from_training
                        ? "Bad quality · excluded from training"
                        : final?.text ||
                          (row.different ? "Unresolved" : "Empty transcript")}
                    </span>
                  </div>
                )}
              </div>
            )}
            {!readOnly && isEditing && edited && (
              <div className="cc-segment-editor">
                <Input.TextArea
                  data-editor-index={editedIndex}
                  aria-label={`Edit segment ${row.id}`}
                  dir="auto"
                  rows={2}
                  value={edited.text}
                  disabled={locked}
                  onChange={(event) =>
                    onPatch(row.id, { text: event.target.value })
                  }
                />
                <Checkbox
                  data-editor-bq={editedIndex}
                  checked={edited.exclude_from_training}
                  disabled={locked}
                  onChange={(event) =>
                    onPatch(row.id, {
                      exclude_from_training: event.target.checked,
                    })
                  }
                >
                  Bad quality
                </Checkbox>
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
