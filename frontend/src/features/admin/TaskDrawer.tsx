import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Tag,
} from "antd";
import { QueryState, SceneSelect, Status, WriteError } from "./AdminParts";
import { AdminList } from "./AdminList";
import {
  dateTime,
  duration,
  label,
  useAdmin,
  useAdminQuery,
  useAdminWrite,
} from "./api";
import { taskDetailSchema, type SceneReview, type TaskDetail } from "./schemas";

export function TaskDrawer({
  taskId,
  onClose,
}: {
  taskId: string;
  onClose: () => void;
}): React.JSX.Element {
  const query = useAdminQuery(
    `/api/admin/annotations/${encodeURIComponent(taskId)}`,
    taskDetailSchema,
  );
  const { action } = useAdmin();
  const data = query.data;
  const current = data?.versions.find((version) => version.is_current);
  return (
    <Drawer
      open
      onClose={onClose}
      size={820}
      title={data?.filename ?? "Task details"}
      className="admin-drawer"
      rootClassName="admin-task-drawer"
      destroyOnHidden
      footer={
        <div className="admin-drawer-footer">
          <Button onClick={onClose}>Close</Button>
          <Space>
            {data?.assignment && (
              <Button
                danger
                onClick={() =>
                  action({ kind: "release", taskId, filename: data.filename })
                }
              >
                Release assignment
              </Button>
            )}
            {data?.current_version_id && current && (
              <Button
                danger
                onClick={() =>
                  action({
                    kind: "revoke",
                    items: [
                      {
                        task_id: taskId,
                        expected_version_id: data.current_version_id ?? "",
                        filename: data.filename,
                        annotator_id: current.submitter?.id ?? null,
                      },
                    ],
                  })
                }
              >
                Revoke annotation
              </Button>
            )}
          </Space>
        </div>
      }
    >
      <QueryState query={query}>
        {data && (
          <div className="admin-stack" id="taskDetail">
            <Space wrap>
              <Status value={data.status} />
              <span>{duration(data.duration)}</span>
              <span className="admin-muted">{data.rel_path}</span>
            </Space>
            <audio
              key={taskId}
              className="admin-audio"
              controls
              preload="metadata"
              src={`/api/admin/audio/${encodeURIComponent(taskId)}`}
            />
            {data.metadata && (
              <Alert
                id="adminTaskMetadataBanner"
                type="info"
                title={data.metadata.headline}
                description={data.metadata.notice}
              />
            )}
            <Card title="Transcript">
              <div className="admin-segments">
                {data.segments.map((segment) => (
                  <article key={segment.id} className="admin-segment">
                    <header>
                      <span>
                        {segment.start.toFixed(2)}–{segment.end.toFixed(2)}s
                      </span>
                      {segment.exclude_from_training && (
                        <Tag color="gold">Bad quality · excluded</Tag>
                      )}
                    </header>
                    <div className="admin-transcript" dir="auto">
                      {segment.text || "No transcript"}
                    </div>
                    {segment.asr_text && (
                      <Collapse
                        ghost
                        items={[
                          {
                            key: "asr",
                            label: "Original ASR",
                            children: <p dir="auto">{segment.asr_text}</p>,
                          },
                        ]}
                      />
                    )}
                  </article>
                ))}
              </div>
            </Card>
            <AdminList title="Sources and provenance">
              {data.source_history.length ? (
                <ul className="admin-list-items">
                  {data.source_history.map((source) => (
                    <li
                      key={source.id}
                      className="admin-list-item admin-version"
                    >
                      <Space wrap>
                        <b>
                          {source.scene_label ||
                            source.scene_code ||
                            "Unknown source scene"}
                        </b>
                        <Tag>{source.confidence}</Tag>
                        <span>{source.batch_code}</span>
                        {source.is_current === false && (
                          <Tag>Historical source</Tag>
                        )}
                      </Space>
                      <p className="admin-list-meta">
                        {source.confidence_basis}
                      </p>
                      {safeSourceUrl(source.source_url) && (
                        <a
                          href={safeSourceUrl(source.source_url)}
                          rel="noopener noreferrer"
                          target="_blank"
                        >
                          Open source
                        </a>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="admin-list-item admin-list-meta">
                  No source metadata.
                </p>
              )}
            </AdminList>
            {data.current_version_id &&
              data.metadata?.features.scene_review_write && (
                <Card title="Correct human review">
                  <ReviewCorrection
                    key={`${taskId}:${data.current_version_id}:${data.metadata.scene_review?.id ?? ""}`}
                    data={data}
                  />
                </Card>
              )}
            <AdminList title="Version history">
              <ul className="admin-list-items">
                {data.versions.map((version) => (
                  <li
                    key={version.id}
                    className="admin-list-item admin-version"
                  >
                    <Space wrap>
                      <b>Version {version.version_no}</b>
                      <Status value={version.lifecycle} />
                      {version.is_current && <Tag color="blue">Current</Tag>}
                    </Space>
                    <p className="admin-list-meta">
                      {version.submitter?.username ?? "System / administrator"}{" "}
                      · {dateTime(version.submitted_at)}
                    </p>
                    {version.revoked_reason && <p>{version.revoked_reason}</p>}
                    {version.revoked_by_admin_action_id &&
                      version.lifecycle === "revoked" && (
                        <Button
                          size="small"
                          onClick={() =>
                            action({
                              kind: "restore",
                              taskId,
                              actionId:
                                version.revoked_by_admin_action_id ?? "",
                              filename: data.filename,
                            })
                          }
                        >
                          Restore
                        </Button>
                      )}
                  </li>
                ))}
              </ul>
            </AdminList>
          </div>
        )}
      </QueryState>
    </Drawer>
  );
}

function safeSourceUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function ReviewFields({
  value,
  onChange,
  disabled = false,
}: {
  value: SceneReview;
  onChange: (value: SceneReview) => void;
  disabled?: boolean;
}): React.JSX.Element {
  return (
    <>
      <Form.Item label="Review status">
        <Select
          id="adminReviewStatus"
          aria-label="Review status"
          disabled={disabled}
          value={value.status}
          onChange={(status: string) =>
            onChange({
              ...value,
              status,
              scene_codes: ["confirmed", "mixed"].includes(status)
                ? value.scene_codes
                : [],
            })
          }
          options={[
            { value: "pending", label: "Pending" },
            { value: "confirmed", label: "Confirmed" },
            { value: "mixed", label: "Multiple scenes" },
            { value: "out_of_scope", label: "Outside the ten scenes" },
            { value: "uncertain", label: "Cannot determine" },
          ]}
        />
      </Form.Item>
      {["confirmed", "mixed"].includes(value.status) && (
        <Form.Item label="Human review scenes">
          <SceneSelect
            id="adminReviewScenes"
            value={value.scene_codes}
            onChange={(scene_codes) => onChange({ ...value, scene_codes })}
            multiple
            disabled={disabled}
          />
        </Form.Item>
      )}
      <Form.Item label="Review note">
        <Input.TextArea
          disabled={disabled}
          value={value.note}
          maxLength={4000}
          onChange={(event) => onChange({ ...value, note: event.target.value })}
        />
      </Form.Item>
    </>
  );
}

function ReviewCorrection({ data }: { data: TaskDetail }): React.JSX.Element {
  const [review, setReview] = useState<SceneReview>(
    data.metadata?.scene_review ?? {
      status: "pending",
      scene_codes: [],
      note: "",
    },
  );
  const [reason, setReason] = useState("");
  const write = useAdminWrite(
    `/api/admin/annotations/${encodeURIComponent(data.task_id)}/scene-review`,
  );
  const { message } = App.useApp();
  const valid =
    review.status === "confirmed"
      ? review.scene_codes.length === 1
      : review.status === "mixed"
        ? review.scene_codes.length >= 2
        : review.scene_codes.length === 0;
  return (
    <Form
      id="adminReviewForm"
      layout="vertical"
      onFinish={() =>
        void write
          .submit({
            expected_version_id: data.current_version_id,
            expected_review_id: data.metadata?.scene_review?.id ?? null,
            status: review.status,
            scene_codes: review.scene_codes,
            note: review.note,
            reason: reason.trim(),
          })
          .then((ok) => {
            if (ok) void message.success("Human review correction appended.");
          })
      }
    >
      <p className="admin-muted">
        Appends a review on the published version. The transcript and original
        review history are preserved.
      </p>
      <p id="adminReviewCurrent">
        {label(data.metadata?.scene_review?.status ?? "pending")} ·{" "}
        {data.metadata?.scene_review?.scene_codes.map(label).join(", ")}
      </p>
      <ReviewFields
        value={review}
        onChange={setReview}
        disabled={write.pending || write.frozen}
      />
      {!valid && (
        <Alert
          type="warning"
          title={
            review.status === "confirmed"
              ? "Choose exactly one scene."
              : "Choose at least two scenes for multiple scenes."
          }
        />
      )}
      <Form.Item label="Reason for correction" required>
        <Input
          id="adminReviewReason"
          value={reason}
          disabled={write.pending || write.frozen}
          onChange={(event) => setReason(event.target.value)}
          maxLength={4000}
        />
      </Form.Item>
      <WriteError error={write.error} frozen={write.frozen} />
      <Button
        type="primary"
        htmlType="submit"
        disabled={!valid || !reason.trim()}
        loading={write.pending}
      >
        {write.frozen ? "Retry same correction" : "Save human review"}
      </Button>
    </Form>
  );
}
