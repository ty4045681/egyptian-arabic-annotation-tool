import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  App as AntApp,
  Alert,
  Avatar,
  Badge,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  Menu,
  Skeleton,
  Space,
} from "antd";
import {
  AppstoreOutlined,
  AuditOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  LogoutOutlined,
  MenuOutlined,
  ReloadOutlined,
  SwapOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { z } from "zod";
import { ApiError, requestJson } from "../../api/client";
import { RoleTheme } from "../../theme/RoleTheme";
import {
  AdminContext,
  createAdminClient,
  messageFor,
  useAdminQuery,
  useAdmin,
} from "./api";
import {
  overviewSchema,
  sessionSchema,
  writeResultSchema,
  type AdminAction,
  type AdminIdentity,
} from "./schemas";
import { OverviewPage } from "./OverviewPage";
import { AnnotatorsPage } from "./AnnotatorsPage";
import { QualityPage, ActivityPage } from "./Queues";
import { CrossChecksPage } from "./CrossChecksPage";
import { clearReviewDrafts } from "./crossCheckReviewModel";
import { CorpusPage } from "./corpus/CorpusPage";
import type { CorpusApiClient } from "./corpus/api";
import { TaskDrawer } from "./TaskDrawer";
import { ActionDialog } from "./ActionDialog";
import "./admin-console.css";

const views = [
  { key: "overview", label: "Overview", icon: <AppstoreOutlined /> },
  { key: "annotators", label: "Annotators", icon: <TeamOutlined /> },
  { key: "corpus", label: "Tasks & corpus", icon: <DatabaseOutlined /> },
  { key: "quality", label: "Quality", icon: <AuditOutlined /> },
  { key: "cross-checks", label: "Cross-checks", icon: <SwapOutlined /> },
  { key: "activity", label: "Activity log", icon: <HistoryOutlined /> },
];

type SessionState =
  | { kind: "loading" }
  | { kind: "signed-out"; message: string }
  | { kind: "authenticated"; identity: AdminIdentity };

export default function AdminConsole(): React.JSX.Element {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<SessionState>({ kind: "loading" });
  const [bootError, setBootError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [dark, setDark] = useState(
    () => matchMedia("(prefers-color-scheme: dark)").matches,
  );
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const change = () => setDark(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.adminMode = dark ? "dark" : "light";
    document.title = "Annotation · Admin console";
    return () => {
      delete document.documentElement.dataset.adminMode;
    };
  }, [dark]);
  const expire = useCallback(() => {
    clearReviewDrafts();
    setSession({
      kind: "signed-out",
      message: "Your admin session expired. Sign in again.",
    });
    void queryClient.cancelQueries({ queryKey: ["admin"] });
    queryClient.removeQueries({ queryKey: ["admin"] });
  }, [queryClient]);
  useEffect(() => {
    const abort = new AbortController();
    void requestJson<unknown>("/api/admin/session", { signal: abort.signal })
      .then((data) => {
        if (abort.signal.aborted) return;
        const identity = sessionSchema.parse(data);
        queryClient.setQueryData(["admin", "session"], identity);
        setSession({ kind: "authenticated", identity });
      })
      .catch((error: unknown) => {
        if (abort.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          clearReviewDrafts();
          setSession({ kind: "signed-out", message: "" });
        } else setBootError(messageFor(error));
      });
    return () => abort.abort();
  }, [attempt, queryClient]);

  async function login({ key }: { key: string }): Promise<void> {
    setBusy(true);
    try {
      const identity = sessionSchema.parse(
        await requestJson<unknown>("/api/admin/login", {
          method: "POST",
          body: { key },
        }),
      );
      clearReviewDrafts();
      queryClient.removeQueries({ queryKey: ["admin"] });
      queryClient.setQueryData(["admin", "session"], identity);
      setSession({ kind: "authenticated", identity });
    } catch (error) {
      setSession({ kind: "signed-out", message: messageFor(error) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <RoleTheme role="admin" mode={dark ? "dark" : "light"}>
      <AntApp>
        {session.kind === "authenticated" ? (
          <AuthenticatedAdmin
            identity={session.identity}
            expire={expire}
            logout={() => {
              clearReviewDrafts();
              setSession({ kind: "signed-out", message: "" });
            }}
          />
        ) : (
          <main className="admin-login" id="loginView">
            <Card className="admin-login-card">
              <span className="admin-brand-mark">A</span>
              <h1>Admin console</h1>
              <p className="admin-muted">
                Manage annotation quality, people and your corpus.
              </p>
              {session.kind === "loading" ? (
                bootError ? (
                  <Alert
                    type="error"
                    title={bootError}
                    action={
                      <Button
                        onClick={() => {
                          setBootError("");
                          setAttempt((value) => value + 1);
                        }}
                      >
                        Retry
                      </Button>
                    }
                  />
                ) : (
                  <Skeleton active />
                )
              ) : (
                <Form
                  layout="vertical"
                  onFinish={(values: { key: string }) => void login(values)}
                >
                  {session.message && (
                    <Alert role="alert" type="error" title={session.message} />
                  )}
                  <Form.Item
                    name="key"
                    label="Admin key"
                    rules={[
                      { required: true, message: "Enter your admin key." },
                    ]}
                  >
                    <Input.Password
                      id="adminKey"
                      autoComplete="current-password"
                      autoFocus
                    />
                  </Form.Item>
                  <Button
                    id="loginButton"
                    type="primary"
                    htmlType="submit"
                    loading={busy}
                    block
                  >
                    Continue
                  </Button>
                </Form>
              )}
              <p>
                <a href="/login.html">Return to annotator sign in</a>
              </p>
            </Card>
          </main>
        )}
      </AntApp>
    </RoleTheme>
  );
}

function AuthenticatedAdmin({
  identity,
  expire,
  logout,
}: {
  identity: AdminIdentity;
  expire: () => void;
  logout: () => void;
}): React.JSX.Element {
  const [search, setSearch] = useSearchParams();
  const [action, setAction] = useState<AdminAction | null>(null);
  const refreshGuard = useRef<(() => boolean) | null>(null);
  const client = useMemo(
    () => createAdminClient(identity, expire),
    [identity, expire],
  );
  const openTask = useCallback(
    (taskId: string) =>
      setSearch((previous) => {
        const next = new URLSearchParams(previous);
        next.set("task", taskId);
        return next;
      }),
    [setSearch],
  );
  const context = useMemo(
    () => ({
      client,
      identity,
      openTask,
      action: setAction,
      expire,
      refreshGuard,
    }),
    [client, identity, openTask, expire],
  );
  return (
    <AdminContext.Provider value={context}>
      <AdminFrame logout={logout} />
      {search.get("task") && (
        <TaskDrawer
          key={search.get("task")}
          taskId={search.get("task") ?? ""}
          onClose={() =>
            setSearch((previous) => {
              const next = new URLSearchParams(previous);
              next.delete("task");
              return next;
            })
          }
        />
      )}
      {action && (
        <ActionDialog action={action} onClose={() => setAction(null)} />
      )}
    </AdminContext.Provider>
  );
}

function AdminFrame({ logout }: { logout: () => void }): React.JSX.Element {
  const [search, setSearch] = useSearchParams();
  const [mobile, setMobile] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const { client, identity, openTask, action, expire, refreshGuard } =
    useAdmin();
  const summary = useAdminQuery("/api/admin/overview", overviewSchema);
  const view =
    views.find((item) => item.key === search.get("view")) ?? views[0];
  const key = view?.key ?? "overview";
  const corpusClient = useMemo<CorpusApiClient>(
    () => ({
      get: (path, options) => client.read(path, z.unknown(), options?.signal),
    }),
    [client],
  );
  const syncCorpusSearch = useCallback(
    (value: string) =>
      setSearch(
        (old) => {
          const next = new URLSearchParams(value);
          const task = old.get("task");
          if (task) next.set("task", task);
          return next;
        },
        { replace: true },
      ),
    [setSearch],
  );
  const navigate = (next: string) => {
    setSearch({ view: next });
    setMobile(false);
  };
  async function signOut() {
    setLogoutBusy(true);
    try {
      await client.send("/api/admin/logout", writeResultSchema, {});
      queryClient.removeQueries({ queryKey: ["admin"] });
      logout();
    } catch (error) {
      void message.error(messageFor(error));
    } finally {
      setLogoutBusy(false);
    }
  }
  const nav = (
    <>
      <div className="admin-brand">
        <span className="admin-brand-mark">A</span>
        <div>
          <strong>Annotation</strong>
          <small>Admin console</small>
        </div>
      </div>
      <Menu
        mode="inline"
        selectedKeys={[key]}
        onClick={(item) => navigate(item.key)}
        items={views.map((item) => ({
          ...item,
          label: (
            <span data-view={item.key}>
              {item.label}
              {item.key === "cross-checks" &&
                !!summary.data?.cross_check.pending_review_count && (
                  <Badge
                    count={summary.data.cross_check.pending_review_count}
                  />
                )}
            </span>
          ),
        }))}
      />
      <div className="admin-sidebar-footer">
        <Avatar>AD</Avatar>
        <div>
          <strong>Administrator</strong>
          <small>Key · {identity.key_id}</small>
        </div>
        <Button
          type="text"
          icon={<LogoutOutlined />}
          aria-label="Log out"
          onClick={() => void signOut()}
          loading={logoutBusy}
        />
      </div>
    </>
  );
  return (
    <div className="admin-console" id="adminApp" data-admin-framework="react">
      <aside className="admin-sidebar" aria-label="Admin navigation">
        {nav}
      </aside>
      <Drawer
        title="Navigation"
        placement="left"
        size={272}
        open={mobile}
        onClose={() => setMobile(false)}
        className="admin-mobile-nav"
      >
        {nav}
      </Drawer>
      <div className="admin-workarea">
        <header className="admin-topbar">
          <Space>
            <Button
              className="admin-mobile-menu"
              icon={<MenuOutlined />}
              aria-label="Open navigation"
              aria-expanded={mobile}
              onClick={() => setMobile(true)}
            />
            <span className="admin-muted">
              Admin / <strong>{view?.label ?? "Overview"}</strong>
            </span>
          </Space>
          <Space>
            <span className="admin-session-dot">Admin session</span>
            <Button
              id="refreshButton"
              icon={<ReloadOutlined />}
              onClick={() => {
                if (!refreshGuard.current || refreshGuard.current())
                  void queryClient.invalidateQueries({ queryKey: ["admin"] });
              }}
            >
              Refresh
            </Button>
          </Space>
        </header>
        <main className="admin-content" id="adminMain">
          {key === "overview" && <OverviewPage />}
          {key === "annotators" && <AnnotatorsPage />}
          {key === "corpus" && (
            <CorpusPage
              apiClient={corpusClient}
              onSessionInvalid={expire}
              onViewTask={openTask}
              onSearchChange={syncCorpusSearch}
              onRevoke={(task) => {
                if (task.current_version_id)
                  action({
                    kind: "revoke",
                    items: [
                      {
                        task_id: task.task_id,
                        expected_version_id: task.current_version_id,
                        filename: task.filename,
                        annotator_id: task.current_submitter?.id ?? null,
                      },
                    ],
                  });
              }}
            />
          )}
          {key === "quality" && <QualityPage />}
          {key === "cross-checks" && <CrossChecksPage />}
          {key === "activity" && <ActivityPage />}
        </main>
      </div>
    </div>
  );
}
