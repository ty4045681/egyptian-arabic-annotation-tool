import { PlaceholderShell } from "./PlaceholderShell";

/**
 * Reserved for the C/D-agent component state matrix (two themes).
 * Only bundled into the P1 verification build (`dist-p1/`); the standard
 * `dist/` build sets `__FRONTEND_BUILD_P1__` to false so this chunk is
 * tree-shaken out. Flask additionally gates the route server-side.
 */
export default function ComponentsPlaceholder(): React.JSX.Element {
  return (
    <PlaceholderShell
      title="Components preview"
      description="The two-theme component state matrix will be mounted here by work packages C/D (P1 verification build only)."
      backHref="/admin?view=corpus"
      backLabel="Back to admin console"
    />
  );
}
