import { PlaceholderShell } from "./PlaceholderShell";

/** Reserved for the E-agent Corpus representative page (view=corpus). */
export default function CorpusPlaceholder(): React.JSX.Element {
  return (
    <PlaceholderShell
      title="Corpus preview"
      description="The admin corpus representative page will be mounted here by work package E. Use the legacy console until then."
      backHref="/admin?view=corpus"
      backLabel="Back to admin console"
    />
  );
}
