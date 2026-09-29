/**
 * RoleTheme: one ConfigProvider per role entry (work packages C/D).
 *
 * - Selects the shared admin (light) or annotator (dark) theme generated
 *   from `tokens.ts`. No user-facing theme switch in P1.
 * - Passes the per-response CSP nonce to Ant Design so runtime-injected
 *   styles satisfy the Flask `style-src` policy without `unsafe-inline`.
 * - Sets `data-theme` so business CSS uses the same semantic variables.
 */
import { ConfigProvider } from "antd";
import type { ReactNode } from "react";
import { adminTheme, adminDarkTheme, annotatorTheme } from "./antdThemes";
import { readCspNonce } from "./csp";
import type { ThemeRole } from "./tokens";
import "./tokens.css";

export function RoleTheme({
  role,
  children,
  mode = "light",
}: {
  role: ThemeRole;
  children: ReactNode;
  mode?: "light" | "dark";
}): React.JSX.Element {
  const nonce = readCspNonce();
  return (
    <ConfigProvider
      theme={
        role === "admin"
          ? mode === "dark"
            ? adminDarkTheme
            : adminTheme
          : annotatorTheme
      }
      {...(nonce === undefined ? {} : { csp: { nonce } })}
    >
      <div data-theme={role} data-color-mode={mode}>
        {children}
      </div>
    </ConfigProvider>
  );
}
