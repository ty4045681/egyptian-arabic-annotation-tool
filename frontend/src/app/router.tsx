import { Suspense, lazy } from "react";
import {
  Route,
  Routes,
  createBrowserRouter,
  RouterProvider,
} from "react-router-dom";

// Build-time flag from vite.config.ts `define`.
// Standard `dist/` builds set it to false so the dev component matrix is
// tree-shaken out; `dist-p1/` sets it to true. Never gate by hiding a menu.
declare const __FRONTEND_BUILD_P1__: boolean;

const IS_P1_BUILD: boolean =
  typeof __FRONTEND_BUILD_P1__ !== "undefined" && __FRONTEND_BUILD_P1__;

// Representative pages live in src/features/** and are lazy-loaded per
// route so entries stay split: the admin list never pulls the workspace
// editor and vice versa.
const CorpusPreview = lazy(
  () => import("../features/admin/corpus/CorpusPreview"),
);
const AdminConsole = lazy(() => import("../features/admin/AdminConsole"));
const WorkspacePreview = lazy(
  () => import("../features/workspace/preview/WorkspacePreview"),
);

// Dev component matrix (P1 verification build only): real two-theme matrix
// from src/dev/*. The standard build sets the flag to false so this chunk
// is tree-shaken out; Flask additionally gates the route server-side.
const ComponentMatrix = IS_P1_BUILD
  ? lazy(() => import("../dev/ComponentMatrix"))
  : null;

function LoadingFallback(): React.JSX.Element {
  return <main style={{ padding: 24 }}>Loading…</main>;
}

/**
 * Flask serves the admin shell and gates the additional preview entries.
 * The admin console handles session expiry and login inside the React app.
 */
function RouteContents(): React.JSX.Element {
  return (
    <Suspense fallback={<LoadingFallback />}>
      <Routes>
        <Route path="/admin" element={<AdminConsole />} />
        <Route path="/admin/login" element={<AdminConsole />} />
        <Route path="/admin/preview" element={<CorpusPreview />} />
        <Route
          path="/frontend-preview/workspace"
          element={<WorkspacePreview />}
        />
        {IS_P1_BUILD && ComponentMatrix !== null ? (
          <Route
            path="/frontend-preview/components"
            element={<ComponentMatrix />}
          />
        ) : null}
        <Route
          path="*"
          element={
            <main style={{ padding: 24 }}>
              <h1>Page not found</h1>
              <p>
                <a href="/">Back to workspace</a> ·{" "}
                <a href="/admin">Back to admin</a>
              </p>
            </main>
          }
        />
      </Routes>
    </Suspense>
  );
}

const router = createBrowserRouter([{ path: "*", element: <RouteContents /> }]);

export function AppRoutes(): React.JSX.Element {
  return <RouterProvider router={router} />;
}
