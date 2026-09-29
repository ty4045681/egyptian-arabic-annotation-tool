import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { shouldRetryGet } from "../api/client";

/**
 * Shared providers for the admin console and preview pages.
 *
 * - TanStack Query manages list/session reads only: no persistence of
 *   identity cache, no save-queue state, refetchOnWindowFocus disabled by
 *   default so focusing never reorders a list the user is reading.
 * - No bare Ant Design ConfigProvider here: every entry renders
 *   inside a themed `RoleTheme` (admin/annotator) which also carries the
 *   per-response CSP nonce. A bare provider would register the default
 *   theme's global CSS variables without a nonce and violate the Flask
 *   `style-src` policy.
 * - The router lives in router.tsx. Legacy annotator pages are reached by
 *   full navigation.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: shouldRetryGet,
      refetchOnWindowFocus: false,
      staleTime: 15_000,
    },
  },
});

export function AppProviders({ children }: { children: ReactNode }): ReactNode {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}
