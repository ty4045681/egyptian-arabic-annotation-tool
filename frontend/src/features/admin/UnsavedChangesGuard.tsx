import { useEffect } from "react";
import { App, Button, Modal } from "antd";
import { useBlocker } from "react-router-dom";
import { useAdmin } from "./api";

export function UnsavedChangesGuard({
  dirty,
  pending,
  onDiscard,
  onSave,
}: {
  dirty: boolean;
  pending: boolean;
  onDiscard?: () => void;
  onSave?: () => boolean;
}): React.JSX.Element {
  const blocker = useBlocker(dirty || pending);
  const { refreshGuard } = useAdmin();
  const { message } = App.useApp();
  useEffect(() => {
    const guard = () => {
      if (pending) {
        void message.warning(
          "Retry the pending request to confirm its result before refreshing.",
        );
        return false;
      }
      if (dirty && !window.confirm("Discard unsaved changes and refresh?"))
        return false;
      onDiscard?.();
      return true;
    };
    refreshGuard.current = guard;
    return () => {
      if (refreshGuard.current === guard) refreshGuard.current = null;
    };
  }, [dirty, pending, onDiscard, refreshGuard, message]);
  useEffect(() => {
    if (!dirty && !pending) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, pending]);
  return (
    <Modal
      open={blocker.state === "blocked"}
      title={
        pending
          ? "Confirm the pending request first"
          : "Discard unsaved changes?"
      }
      okText="Discard and leave"
      okButtonProps={{ disabled: pending, danger: true }}
      cancelText="Keep editing"
      footer={
        onSave && !pending
          ? (_, { OkBtn, CancelBtn }) => (
              <>
                <CancelBtn />
                <OkBtn />
                <Button
                  type="primary"
                  onClick={() => {
                    if (blocker.state === "blocked" && onSave())
                      blocker.proceed();
                  }}
                >
                  Save draft and leave
                </Button>
              </>
            )
          : undefined
      }
      onCancel={() => {
        if (blocker.state === "blocked") blocker.reset();
      }}
      onOk={() => {
        if (blocker.state === "blocked" && !pending) blocker.proceed();
      }}
    >
      {pending
        ? "The request result is not yet confirmed. Retry the same request before leaving."
        : "Your changes have not been saved. Leaving this page will discard them."}
    </Modal>
  );
}
