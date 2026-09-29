import { AppRoutes } from "./router";

/**
 * P1 preview app shell (work packages A/B).
 * Real layout shells (AdminShell/AnnotatorShell) land with work package D;
 * this shell only mounts the preview router inside providers (see main.tsx).
 */
export function App(): React.JSX.Element {
  return <AppRoutes />;
}
