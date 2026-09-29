import { useState } from "react";
import { Alert, App, Button, Form, Input, Modal, Switch } from "antd";
import { QueryState, WriteError } from "./AdminParts";
import { useAdminQuery, useAdminWrite } from "./api";
import { settingsSchema } from "./schemas";
import { samplingBps } from "./crossCheckModel";
import { UnsavedChangesGuard } from "./UnsavedChangesGuard";
import type { z } from "zod";
import { ApiError } from "../../api/client";

export function SamplingSettings({
  onClose,
}: {
  onClose: () => void;
}): React.JSX.Element {
  const query = useAdminQuery(
    "/api/admin/cross-check-settings",
    settingsSchema,
  );
  if (query.data)
    return (
      <SettingsForm
        data={query.data}
        onClose={onClose}
        reload={() => void query.refetch()}
        loading={query.isFetching}
        loadError={query.error}
      />
    );
  return (
    <Modal
      open
      title="Cross-check sampling"
      className="cc-sampling-modal"
      rootClassName="cc-sampling-layer"
      width={480}
      centered
      onCancel={onClose}
      footer={null}
      destroyOnHidden
    >
      <QueryState query={query}>{null}</QueryState>
    </Modal>
  );
}

function SettingsForm({
  data,
  onClose,
  reload,
  loading,
  loadError,
}: {
  data: z.infer<typeof settingsSchema>;
  onClose: () => void;
  reload: () => void;
  loading: boolean;
  loadError: unknown;
}): React.JSX.Element {
  const [enabled, setEnabled] = useState(data.enabled);
  const [percent, setPercent] = useState(String(data.sampling_rate_bps / 100));
  const [reason, setReason] = useState("");
  const [conflict, setConflict] = useState(false);
  const write = useAdminWrite("/api/admin/cross-check-settings", "PUT");
  const { message } = App.useApp();
  const bps = samplingBps(percent);
  const locked = write.pending || write.frozen;
  const dirty =
    enabled !== data.enabled ||
    bps !== data.sampling_rate_bps ||
    reason.trim() !== "";
  const close = () => {
    if (
      !locked &&
      (!dirty || window.confirm("Discard unsaved sampling settings?"))
    )
      onClose();
  };
  return (
    <Modal
      open
      title="Cross-check sampling"
      className="cc-sampling-modal"
      rootClassName="cc-sampling-layer"
      width={480}
      centered
      onCancel={close}
      footer={null}
      closable={!locked}
      mask={{ closable: !locked }}
      keyboard={!locked}
      destroyOnHidden
    >
      <Form
        id="ccSamplingForm"
        layout="vertical"
        disabled={locked}
        onFinish={() =>
          void write
            .submit(
              {
                expected_revision: data.revision,
                enabled,
                sampling_rate_bps: bps,
                reason: reason.trim(),
              },
              (error) => {
                if (error instanceof ApiError && error.status === 409) {
                  setConflict(true);
                  reload();
                }
              },
            )
            .then((ok) => {
              if (ok) {
                onClose();
                void message.success("Sampling settings saved.");
              }
            })
        }
      >
        <p className="cc-sampling-description">
          Applies to new claims only. Open rounds stay in the queue.
        </p>
        <UnsavedChangesGuard dirty={dirty} pending={locked} />
        {conflict && (
          <Alert
            id="ccSettingsStale"
            type="warning"
            title="Settings changed. Your inputs have been preserved."
            description={`Server: ${data.enabled ? "enabled" : "disabled"}, ${data.sampling_rate_bps / 100}%, revision ${data.revision}. Your draft: ${enabled ? "enabled" : "disabled"}, ${percent}%. Review the values before saving again.`}
          />
        )}
        {loadError !== null && (
          <Alert
            type="error"
            title="Could not reload current settings. Your draft is preserved."
            action={<Button onClick={reload}>Retry loading settings</Button>}
          />
        )}
        <Form.Item className="cc-sampling-enabled">
          <label className="cc-sampling-toggle" htmlFor="ccSamplingEnabled">
            <Switch
              id="ccSamplingEnabled"
              size="small"
              checked={enabled}
              onChange={setEnabled}
            />
            Enable cross-check sampling
          </label>
        </Form.Item>
        <Form.Item label="Sampling rate" htmlFor="ccSamplingPercent">
          <div className="cc-sampling-rate">
            <Input
              id="ccSamplingPercent"
              inputMode="decimal"
              disabled={locked || !enabled}
              aria-describedby="ccSamplingHint"
              aria-invalid={bps === null}
              value={percent}
              onChange={(event) => setPercent(event.target.value)}
            />
            <span className="admin-muted">%</span>
            <span id="ccSamplingHint" className="admin-muted">
              {!enabled || bps === 0
                ? "No new rounds"
                : bps !== null
                  ? `≈ 1 in ${Math.round(10000 / bps)} claims`
                  : ""}
            </span>
          </div>
          <p className="cc-sampling-hint">
            If one pool is empty the server falls back to the other, so the live
            mix can differ from this rate.
          </p>
          {bps === null && (
            <p className="admin-muted">
              Enter 0 to 100 with at most two decimal places.
            </p>
          )}
        </Form.Item>
        <p className="cc-sampling-impact">
          Rounds go to review when word difference exceeds{" "}
          <strong>{data.word_difference_threshold_bps / 100}%</strong>.
        </p>
        <Form.Item label="Reason" required>
          <Input.TextArea
            id="ccSamplingReason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={4000}
            rows={3}
            placeholder="Why are you changing the sampling rate?"
          />
        </Form.Item>
        <div id="ccSettingsError">
          <WriteError error={write.error} frozen={write.frozen} />
        </div>
        <div className="cc-sampling-footer">
          <Button disabled={locked} onClick={close}>
            Cancel
          </Button>
          <Button
            id="ccSettingsSave"
            type="primary"
            htmlType="submit"
            loading={write.pending}
            disabled={
              bps === null || !reason.trim() || loading || loadError !== null
            }
          >
            {write.frozen ? "Retry same settings" : "Save settings"}
          </Button>
        </div>
      </Form>
    </Modal>
  );
}
