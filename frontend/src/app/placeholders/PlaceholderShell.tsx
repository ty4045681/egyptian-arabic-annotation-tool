/**
 * P1 preview placeholders (work packages A/B).
 *
 * These are intentionally minimal English shells that prove routing,
 * lazy-loading, and build-constant trimming before the real representative
 * pages (E/F) and component matrix (C/D) land. They perform no data fetch
 * and contain no fixture data, so `dist/` trivially satisfies "no dev
 * sample" checks. Feature agents replace the corresponding placeholder
 * import with the real page without changing Flask routes.
 */
export function PlaceholderShell({
  title,
  description,
  backHref,
  backLabel,
}: {
  title: string;
  description: string;
  backHref: string;
  backLabel: string;
}): React.JSX.Element {
  return (
    <main style={{ padding: 24, fontFamily: "system-ui, sans-serif" }}>
      <h1>{title}</h1>
      <p>{description}</p>
      <p>
        <a href={backHref}>{backLabel}</a>
      </p>
    </main>
  );
}
