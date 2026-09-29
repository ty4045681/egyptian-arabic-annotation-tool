import { PlaceholderShell } from "./PlaceholderShell";

/** Reserved for the F-agent workspace representative page. */
export default function WorkspacePlaceholder(): React.JSX.Element {
  return (
    <PlaceholderShell
      title="Workspace preview"
      description="The annotator workspace representative page will be mounted here by work package F. Use the legacy workspace until then."
      backHref="/"
      backLabel="Back to workspace"
    />
  );
}
