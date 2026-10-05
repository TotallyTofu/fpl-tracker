// Boot-time full news refresh notification (M3 revised).
// Polls /api/meta/startup-refresh: shows a slim bar while the background
// refresh runs, and a dismissible "complete" banner when it finishes
// (only for recent runs, so old boots don't nag on every page load).
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { StartupRefreshState } from "../types";

const POLL_MS = 4000;
const BANNER_MS = 15000;
const RECENT_MS = 15 * 60 * 1000;

function fmtResult(v: string | Record<string, unknown>): string {
  if (typeof v === "string") return v;
  const rows = (v as { rows?: number }).rows;
  return rows !== undefined ? `${rows} rows` : "ok";
}

export default function StartupToast() {
  const [state, setState] = useState<StartupRefreshState | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const sawRunning = useRef(false);
  const notifiedRun = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .getStartupRefresh()
        .then((s) => {
          if (!alive) return;
          setState(s);
          if (s.status === "running") sawRunning.current = true;
          const runId = s.started_at ?? null;
          const fresh =
            s.status === "done" &&
            !!s.finished_at &&
            Date.now() - new Date(s.finished_at).getTime() < RECENT_MS;
          // notify when we watched the run finish, or when a run finished very recently
          if (s.status === "done" && runId && runId !== notifiedRun.current && (sawRunning.current || fresh)) {
            notifiedRun.current = runId;
            setDismissed(false);
          }
        })
        .catch(() => {});
    load();
    const t = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    if (!state || state.status !== "done" || dismissed) return;
    const t = setTimeout(() => setDismissed(true), BANNER_MS);
    return () => clearTimeout(t);
  }, [state, dismissed]);

  if (!state || dismissed) return null;

  if (state.status === "running") {
    return (
      <div className="startup-toast running" role="status">
        <span className="spinner" /> Fetching the latest news (BBC · ESPN · Reddit · YouTube)…
      </div>
    );
  }

  if (state.status === "done") {
    const parts = Object.entries(state.results ?? {})
      .map(([src, v]) => `${src}: ${fmtResult(v)}`)
      .join(" · ");
    return (
      <div className="startup-toast done" role="status">
        <span>
          News updated: {parts}
          {state.signals_stored ? ` · ${state.signals_stored} signal(s) stored` : ""}
        </span>
        <button className="toast-close" onClick={() => setDismissed(true)} aria-label="Dismiss">
          Close
        </button>
      </div>
    );
  }

  return null;
}