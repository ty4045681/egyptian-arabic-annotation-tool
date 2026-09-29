/**
 * AdminShell: single top navigation for management entries (work packages C/D).
 * Content has `min-width: 0` so long filters/tables cannot stretch the page
 * (U01). Cross-entry navigation uses full page loads, never the preview
 * client router.
 */
import type { ReactNode } from "react";
import styles from "./AdminShell.module.css";

export interface AdminNavItem {
  key: string;
  label: string;
  href: string;
  current?: boolean;
}

export function AdminShell({
  nav,
  userEntry,
  children,
}: {
  nav: AdminNavItem[];
  userEntry?: ReactNode;
  children: ReactNode;
}): React.JSX.Element {
  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <span className={styles.brand}>Annotation console</span>
        <nav className={styles.nav} aria-label="Management">
          {nav.map((item) =>
            item.current ? (
              <span key={item.key} className={styles.navLink} aria-current="page">
                {item.label}
              </span>
            ) : (
              <a key={item.key} className={styles.navLink} href={item.href}>
                {item.label}
              </a>
            ),
          )}
        </nav>
        {userEntry === undefined ? null : <span className={styles.userSlot}>{userEntry}</span>}
      </header>
      <main className={styles.content}>{children}</main>
    </div>
  );
}
