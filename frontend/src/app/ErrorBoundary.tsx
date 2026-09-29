import { Component, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * P1 error boundary (work packages A/B).
 * Catches render failures in preview pages and shows a safe English
 * fallback without leaking response bodies, cookies, or transcripts.
 */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error): void {
    // Intentionally console-only; server-side reporting goes through the
    // explicit src/api error-log helper (never recursive).
    console.error("Preview render failed", error.message);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <main style={{ padding: 24, fontFamily: "system-ui, sans-serif" }}>
          <h1>Something went wrong</h1>
          <p>
            The preview page could not be rendered. Return to the existing
            workspace or admin console.
          </p>
          <p>
            <a href="/">Back to workspace</a> ·{" "}
            <a href="/admin">Back to admin</a>
          </p>
        </main>
      );
    }
    return this.props.children;
  }
}
