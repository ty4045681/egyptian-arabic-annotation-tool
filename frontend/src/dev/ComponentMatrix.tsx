/**
 * Component state matrix (work packages C/D).
 *
 * Verification-only entry bundled into `dist-p1/` and served at
 * `/frontend-preview/components`. Exercises both role themes with the
 * section-5.1 control set: Select, DatePicker, Tooltip, Modal, Drawer,
 * message, scrolling/fixed-column Table, Input.TextArea, Slider, buttons.
 * Includes compact layout, long errors, themed overlays and a scroll
 * scenario. First-trigger, scroll, resize and reopen paths are covered by
 * the browser test, not by static rendering.
 *
 * Toasts use the provider-context message API so overlay roots keep the
 * active theme and CSP nonce.
 */
import {
  Button,
  DatePicker,
  Drawer,
  Input,
  Modal,
  Select,
  Slider,
  Table,
  Tooltip,
  message,
} from "antd";
import { useState } from "react";
import { RoleTheme } from "../theme/RoleTheme";
import type { ThemeRole } from "../theme/tokens";
import styles from "./ComponentMatrix.module.css";

const LONG_LABEL =
  "Source scene confidence filter with a very long option label for overflow";

const TABLE_COLUMNS = [
  { title: "Audio file", dataIndex: "file", key: "file", fixed: "left" as const, width: 260 },
  { title: "Submitter", dataIndex: "user", key: "user", width: 160 },
  { title: "Status", dataIndex: "status", key: "status", width: 140 },
  { title: "Duration (s)", dataIndex: "duration", key: "duration", width: 120, align: "right" as const },
  { title: "Reviewed", dataIndex: "reviewed", key: "reviewed", width: 200 },
  { title: "Action", dataIndex: "action", key: "action", fixed: "right" as const, width: 120 },
];

const TABLE_ROWS = Array.from({ length: 12 }, (_, index) => ({
  key: `row-${index}`,
  file: `very-long-audio-filename-that-must-not-push-actions-${index}.wav`,
  user: `annotator-with-a-long-username-${index}`,
  status: index % 2 === 0 ? "Published" : "Pending",
  duration: 60 + index,
  reviewed: "2026-09-21 10:00",
  action: "Open",
}));

function ThemeSection({ role }: { role: ThemeRole }): React.JSX.Element {
  const [messageApi, contextHolder] = message.useMessage();
  const [modalOpen, setModalOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  return (
    <RoleTheme role={role}>
      <section className={styles.themeSection} data-theme-section={role} aria-label={`${role} theme matrix`}>
        {contextHolder}
        <h2 className={styles.themeTitle}>{role === "admin" ? "Admin (light)" : "Annotator (dark)"}</h2>
        <div className={styles.grid}>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>{LONG_LABEL}</span>
            <Select
              defaultValue="high"
              aria-label={`${role} confidence select`}
              options={[
                { value: "high", label: "High confidence" },
                { value: "medium", label: "Medium confidence with a long label" },
                { value: "low", label: "Low" },
              ]}
            />
          </div>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Date range</span>
            <DatePicker.RangePicker aria-label={`${role} date range`} />
          </div>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Notes</span>
            <Input.TextArea rows={3} aria-label={`${role} notes input`} placeholder="Transcription notes" />
            <span className={styles.longError} role="alert">
              Error: this is a deliberately long validation message that must wrap inside the
              field grid without disturbing the input baseline or neighboring controls.
            </span>
          </div>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Volume</span>
            <Slider defaultValue={40} aria-label={`${role} volume slider`} />
          </div>
        </div>
        <div className={styles.row}>
          <Tooltip title="Tooltip attached to the primary action">
            <Button
              type="primary"
              aria-label={`${role} primary action`}
              onClick={() => void messageApi.success(`${role}: primary action clicked`)}
            >
              Primary
            </Button>
          </Tooltip>
          <Button aria-label={`${role} secondary action`}>Secondary</Button>
          <Button aria-label={`${role} open modal`} onClick={() => setModalOpen(true)}>
            Open modal
          </Button>
          <Button aria-label={`${role} open drawer`} onClick={() => setDrawerOpen(true)}>
            Open drawer
          </Button>
        </div>
        <div className={styles.scrollBox} data-scroll-x="table">
          <Table
            columns={TABLE_COLUMNS}
            dataSource={TABLE_ROWS}
            pagination={false}
            size="middle"
            scroll={{ x: "max-content", y: 160 }}
            aria-label={`${role} scrolling table`}
          />
        </div>
        <Modal
          title={`${role} confirmation`}
          open={modalOpen}
          onOk={() => setModalOpen(false)}
          onCancel={() => setModalOpen(false)}
        >
          <p>Themed modal content. Focus must return to the trigger on close.</p>
        </Modal>
        <Drawer title={`${role} drawer`} open={drawerOpen} onClose={() => setDrawerOpen(false)}>
          <p>Themed drawer content used for narrow-screen overflow checks.</p>
        </Drawer>
      </section>
    </RoleTheme>
  );
}

export default function ComponentMatrix(): React.JSX.Element {
  return (
    <main className={styles.matrix}>
      <h1>Component state matrix</h1>
      <ThemeSection role="admin" />
      <ThemeSection role="annotator" />
    </main>
  );
}
