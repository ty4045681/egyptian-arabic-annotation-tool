/**
 * Corpus preview entry: real admin API behind the shared admin shell.
 * Served at `/admin/preview?view=corpus` (Flask gates auth + build).
 */
import { apiGet } from "../../../api/client";
import { AdminShell } from "../../../components/AdminShell";
import { RoleTheme } from "../../../theme/RoleTheme";
import type { CorpusApiClient } from "./api";
import { CorpusPage } from "./CorpusPage";

const client: CorpusApiClient = {
  get: (path, options) => apiGet<unknown>(path, options),
};

export default function CorpusPreview(): React.JSX.Element {
  return (
    <RoleTheme role="admin">
      <AdminShell
        nav={[
          {
            key: "corpus",
            label: "Tasks & corpus (preview)",
            href: "/admin/preview?view=corpus",
            current: true,
          },
          {
            key: "legacy",
            label: "Back to admin console",
            href: "/admin?view=corpus",
          },
        ]}
      >
        <CorpusPage apiClient={client} />
      </AdminShell>
    </RoleTheme>
  );
}
