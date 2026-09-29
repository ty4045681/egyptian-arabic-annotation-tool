/**
 * AnnotatorShell: stable top bar for the workspace (work packages C/D).
 * The editing area receives the primary width; no persistent side column
 * (U06). Task/save/audio slots keep stable positions across filename
 * lengths and save-state changes (U04).
 */
import type { ReactNode } from "react";
import styles from "./AnnotatorShell.module.css";

export function AnnotatorShell({
  taskSlot,
  userEntry,
  children,
}: {
  taskSlot?: ReactNode;
  userEntry?: ReactNode;
  children: ReactNode;
}): React.JSX.Element {
  return (
    <div className={styles.shell}>
      <header className={styles.appbar}>
        <span className={styles.brand}>Annotation workspace</span>
        {taskSlot === undefined ? null : <span className={styles.taskSlot}>{taskSlot}</span>}
        {userEntry === undefined ? null : <span className={styles.userSlot}>{userEntry}</span>}
      </header>
      <main className={styles.body}>{children}</main>
    </div>
  );
}
