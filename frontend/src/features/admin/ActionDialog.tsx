import { useState } from "react";
import { Alert, App, Button, Checkbox, Form, Input, Modal, Space } from "antd";
import { useQuery } from "@tanstack/react-query";
import { QueryState, WriteError } from "./AdminParts";
import { duration, label, useAdmin, useAdminWrite } from "./api";
import {
  deactivatePreviewSchema,
  revokePreviewSchema,
  type AdminAction,
  type RevokeTarget,
} from "./schemas";

export function ActionDialog({
  action,
  onClose,
}: {
  action: AdminAction;
  onClose: () => void;
}): React.JSX.Element {
  switch (action.kind) {
    case "revoke":
      return <RevokeDialog items={action.items} onClose={onClose} />;
    case "deactivate":
      return (
        <DeactivateDialog annotator={action.annotator} onClose={onClose} />
      );
    case "release":
    case "restore":
      return <SimpleActionDialog action={action} onClose={onClose} />;
  }
}

function RevokeDialog({
  items,
  onClose,
}: {
  items: RevokeTarget[];
  onClose: () => void;
}): React.JSX.Element {
  const { client } = useAdmin();
  const { message } = App.useApp();
  const annotator = items[0]?.annotator_id ?? null;
  const [block, setBlock] = useState(annotator !== null);
  const [release, setRelease] = useState(false);
  const [reason, setReason] = useState("");
  const [key, setKey] = useState("");
  const payload = {
    annotator_id: annotator,
    items: items.map(({ task_id, expected_version_id }) => ({
      task_id,
      expected_version_id,
    })),
    block_reclaim: block,
    release_conflicts: release,
  };
  const preview = useQuery({
    queryKey: ["admin", "revoke-preview", payload],
    queryFn: () =>
      client.send(
        "/api/admin/annotations/revoke/preview",
        revokePreviewSchema,
        payload,
      ),
    retry: false,
  });
  const write = useAdminWrite("/api/admin/annotations/revoke");
  const locked = write.pending || write.frozen;
  const ready =
    !!preview.data &&
    !preview.isFetching &&
    !preview.isError &&
    preview.data.summary.conflicts === 0;
  return (
    <Modal
      open
      title={
        items.length === 1
          ? "Revoke annotation"
          : `Revoke ${items.length} annotations`
      }
      onCancel={() => {
        if (!locked) onClose();
      }}
      closable={!locked}
      mask={{ closable: !locked }}
      keyboard={!locked}
      footer={null}
      destroyOnHidden
    >
      <div className="admin-stack">
        <p>The selected annotations will return to the task pool.</p>
        <QueryState query={preview}>
          {preview.data && (
            <>
              <Alert
                type={preview.data.summary.conflicts ? "warning" : "info"}
                title={`${preview.data.summary.revokeable} annotations · ${duration(preview.data.summary.duration_seconds)} affected`}
                description={
                  preview.data.items
                    .filter((item) => item.conflict)
                    .map(
                      (item) =>
                        `${item.filename}: ${label(item.conflict ?? "")}`,
                    )
                    .join("; ") ||
                  "Versions and reclaim restrictions have been checked."
                }
              />
              {!!preview.data.summary.open_cross_check_rounds && (
                <Alert
                  type="warning"
                  title={`${preview.data.summary.open_cross_check_rounds} open cross-check rounds are affected`}
                />
              )}
            </>
          )}
        </QueryState>
        <Form
          layout="vertical"
          disabled={locked}
          onFinish={() =>
            void write
              .submit({
                ...payload,
                reason: reason.trim(),
                confirm: true,
                ...(items.length > 1 ? { admin_key: key } : {}),
              })
              .then((ok) => {
                setKey("");
                if (ok) {
                  onClose();
                  void message.success("Annotations revoked.");
                }
              })
          }
        >
          <Form.Item label="Reason" required>
            <Input.TextArea
              id="revokeReason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              rows={3}
            />
          </Form.Item>
          {items.length > 1 && (
            <Form.Item label="Re-enter admin key" required>
              <Input.Password
                id="revokeAdminKey"
                value={key}
                onChange={(event) => setKey(event.target.value)}
                autoComplete="current-password"
              />
            </Form.Item>
          )}
          {annotator !== null && (
            <Form.Item>
              <Checkbox
                checked={block}
                onChange={(event) => setBlock(event.target.checked)}
              >
                Prevent the original annotator from reclaiming these tasks
              </Checkbox>
            </Form.Item>
          )}
          <Form.Item>
            <Checkbox
              checked={release}
              onChange={(event) => setRelease(event.target.checked)}
            >
              Release conflicting assignments and invalidate open cross-checks
            </Checkbox>
          </Form.Item>
          <WriteError error={write.error} frozen={write.frozen} />
          <div className="admin-pager">
            <Button disabled={locked} onClick={onClose}>
              Cancel
            </Button>
            <Button
              danger
              type="primary"
              htmlType="submit"
              loading={write.pending}
              disabled={
                !ready ||
                !reason.trim() ||
                (items.length > 1 && !key && !write.frozen)
              }
            >
              {write.frozen ? "Retry same revocation" : "Revoke"}
            </Button>
          </div>
        </Form>
      </div>
    </Modal>
  );
}

function DeactivateDialog({
  annotator,
  onClose,
}: {
  annotator: { id: string; username: string };
  onClose: () => void;
}): React.JSX.Element {
  const { client } = useAdmin();
  const { message } = App.useApp();
  const [reason, setReason] = useState("");
  const [username, setUsername] = useState("");
  const [key, setKey] = useState("");
  const path = `/api/admin/annotators/${encodeURIComponent(annotator.id)}/deactivate`;
  const preview = useQuery({
    queryKey: ["admin", "deactivate-preview", annotator.id],
    queryFn: () => client.send(`${path}/preview`, deactivatePreviewSchema, {}),
    retry: false,
  });
  const write = useAdminWrite(path);
  const locked = write.pending || write.frozen;
  return (
    <Modal
      open
      title={`Deactivate ${annotator.username}`}
      onCancel={() => {
        if (!locked) onClose();
      }}
      closable={!locked}
      mask={{ closable: !locked }}
      keyboard={!locked}
      footer={null}
    >
      <div className="admin-stack">
        <QueryState query={preview}>
          {preview.data && (
            <Alert
              type="warning"
              showIcon
              title="Access will be disabled immediately"
              description={`${preview.data.summary.published_to_revoke} contributions returned · ${preview.data.summary.assignments_to_release} assignments released · ${preview.data.summary.sessions_to_revoke} sessions ended · ${preview.data.summary.cross_check_in_progress_to_cancel} cross-checks cancelled. ${preview.data.summary.cross_check_awaiting_review_kept} submitted cross-checks remain available for review.`}
            />
          )}
        </QueryState>
        <Form
          layout="vertical"
          disabled={locked}
          onFinish={() =>
            void write
              .submit({
                reason: reason.trim(),
                confirm_username: username,
                admin_key: key,
                confirm: true,
              })
              .then((ok) => {
                setKey("");
                if (ok) {
                  onClose();
                  void message.success(
                    `${annotator.username} was deactivated.`,
                  );
                }
              })
          }
        >
          <Form.Item label="Reason" required>
            <Input.TextArea
              id="deactivateReason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              rows={3}
            />
          </Form.Item>
          <Form.Item label={`Type ${annotator.username} to confirm`} required>
            <Input
              id="confirmUsername"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item label="Re-enter admin key" required>
            <Input.Password
              id="deactivateAdminKey"
              value={key}
              onChange={(event) => setKey(event.target.value)}
              autoComplete="current-password"
            />
          </Form.Item>
          <WriteError error={write.error} frozen={write.frozen} />
          <div className="admin-pager">
            <Button disabled={locked} onClick={onClose}>
              Cancel
            </Button>
            <Button
              danger
              type="primary"
              htmlType="submit"
              loading={write.pending}
              disabled={
                !preview.data ||
                preview.isError ||
                !reason.trim() ||
                username !== annotator.username ||
                (!key && !write.frozen)
              }
            >
              {write.frozen
                ? "Retry same deactivation"
                : "Deactivate and return work"}
            </Button>
          </div>
        </Form>
      </div>
    </Modal>
  );
}

function SimpleActionDialog({
  action,
  onClose,
}: {
  action: Extract<AdminAction, { kind: "release" | "restore" }>;
  onClose: () => void;
}): React.JSX.Element {
  const write = useAdminWrite(
    action.kind === "release"
      ? `/api/admin/assignments/${encodeURIComponent(action.taskId)}/release`
      : "/api/admin/annotations/restore",
  );
  const [reason, setReason] = useState("");
  const locked = write.pending || write.frozen;
  return (
    <Modal
      open
      title={`${label(action.kind)} ${action.filename}`}
      onCancel={() => {
        if (!locked) onClose();
      }}
      footer={null}
      closable={!locked}
      mask={{ closable: !locked }}
      keyboard={!locked}
    >
      <Form
        layout="vertical"
        onFinish={() =>
          void write
            .submit({
              reason: reason.trim(),
              confirm: true,
              ...(action.kind === "restore"
                ? {
                    admin_action_id: action.actionId,
                    task_ids: [action.taskId],
                  }
                : {}),
            })
            .then((ok) => {
              if (ok) onClose();
            })
        }
      >
        <p>
          {action.kind === "release"
            ? "The current assignment will be released so the task can be claimed again."
            : "The revoked contribution will be restored if its version is still eligible."}
        </p>
        <Form.Item label="Reason" required>
          <Input.TextArea
            value={reason}
            disabled={locked}
            onChange={(event) => setReason(event.target.value)}
            maxLength={500}
          />
        </Form.Item>
        <WriteError error={write.error} frozen={write.frozen} />
        <Space>
          <Button onClick={onClose} disabled={locked}>
            Cancel
          </Button>
          <Button
            htmlType="submit"
            type="primary"
            danger={action.kind === "release"}
            loading={write.pending}
            disabled={!reason.trim()}
          >
            {write.frozen ? "Retry same request" : `Confirm ${action.kind}`}
          </Button>
        </Space>
      </Form>
    </Modal>
  );
}
