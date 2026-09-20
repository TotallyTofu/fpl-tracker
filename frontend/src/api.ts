// Typed API client. All /api/*; 422 → ApiError with parsed errors[].
import type {
  DbStats,
  Diff,
  Health,
  Lineup,
  LineupSummary,
  NameMatch,
  Player,
  RawItem,
  Season,
  SettingsResponse,
  Signal,
  StartupRefreshState,
  Suggestion,
} from "./types";

export class ApiError extends Error {
  status: number;
  errors: { code: string; message: string; severity: string }[];
  constructor(status: number, message: string, errors: ApiError["errors"] = []) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    let errors: ApiError["errors"] = [];
    try {
      const body = await r.json();
      if (typeof body.detail === "string") msg = body.detail;
      else if (Array.isArray(body.detail)) {
        errors = body.detail;
        msg = errors.map((e) => e.message || e.code).join("; ");
      } else if (body.error) msg = body.error;
    } catch {
      /* non-JSON */
    }
    throw new ApiError(r.status, msg, errors);
  }
  return r.json() as Promise<T>;
}

export const api = {
  getSeason: () => req<Season>("/meta/season"),
  getHealth: () => req<Health>("/meta/health"),
  getStartupRefresh: () => req<StartupRefreshState>("/meta/startup-refresh"),
  refresh: (source: string) =>
    req<{ results: Record<string, string | { status: string; rows?: number; [k: string]: unknown }> }>(
      `/refresh/${source}`,
      { method: "POST" }
    ),

  getSignals: (params: { player_id?: number; team?: string; category?: string; active?: boolean } = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") q.set(k, String(v));
    const qs = q.toString();
    return req<{ signals: Signal[]; count: number }>(`/signals${qs ? `?${qs}` : ""}`);
  },
  getItems: (params: { source?: string; kind?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") q.set(k, String(v));
    const qs = q.toString();
    return req<{ items: RawItem[]; count: number }>(`/items${qs ? `?${qs}` : ""}`);
  },
  fetchItemBody: (id: number) =>
    req<{ body: string; truncated: boolean }>(`/items/${id}/fetch-body`, { method: "POST" }),

  getPlayers: (params: {
    search?: string;
    pos?: number;
    team?: number;
    min_cost?: number;
    max_cost?: number;
    has_signal?: boolean;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") q.set(k, String(v));
    return req<{ players: Player[]; count: number }>(`/players?${q.toString()}`);
  },

  listLineups: () => req<{ lineups: LineupSummary[] }>("/lineups"),
  getLineup: (id: number) => req<Lineup>(`/lineups/${id}`),
  createLineup: (body: unknown) => req<Lineup>("/lineups", { method: "POST", body: JSON.stringify(body) }),
  updateLineup: (id: number, body: unknown) =>
    req<Lineup>(`/lineups/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteLineup: (id: number) => req<{ deleted: number }>(`/lineups/${id}`, { method: "DELETE" }),
  setCurrent: (id: number) => req<{ current: number }>(`/lineups/${id}/set-current`, { method: "POST" }),
  duplicateLineup: (id: number, name?: string) =>
    req<Lineup>(`/lineups/${id}/duplicate`, {
      method: "POST",
      body: JSON.stringify(name ? { name } : {}),
    }),
  getChipPlays: (gw?: number) =>
    req<{ chip_plays: { gw: number; chip: string; played_at: string }[] }>(
      `/lineups/chip-plays${gw ? `?gw=${gw}` : ""}`
    ),
  logChipPlay: (gw: number, chip: string) =>
    req<{ logged: { gw: number; chip: string } }>("/lineups/chip-play", {
      method: "POST",
      body: JSON.stringify({ gw, chip }),
    }),
  unlogChipPlay: (gw: number, chip: string) =>
    req<{ deleted: boolean }>(`/lineups/chip-play?gw=${gw}&chip=${chip}`, { method: "DELETE" }),
  matchNames: (names: string[]) =>
    req<{ matches: NameMatch[] }>("/lineups/match-names", {
      method: "POST",
      body: JSON.stringify({ names }),
    }),

  generateSuggestions: (body: { lineup_id: number; target_gw?: number }) =>
    req<{ suggestions: Suggestion[]; target_gw: number }>("/suggestions/generate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listSuggestions: (lineupId?: number) =>
    req<{ suggestions: Suggestion[] }>(
      `/suggestions${lineupId ? `?lineup_id=${lineupId}` : ""}`
    ),
  deleteSuggestion: (id: number) => req<{ deleted: number }>(`/suggestions/${id}`, { method: "DELETE" }),
  applySuggestion: (id: number) =>
    req<{ applied: number; already?: boolean; bank_after: number; chips_logged: string[] }>(
      `/suggestions/${id}/apply`,
      { method: "POST" }
    ),

  getSettings: () => req<SettingsResponse>("/settings"),
  putSettings: (body: unknown) => req<SettingsResponse>("/settings", { method: "PUT", body: JSON.stringify(body) }),
  testLlm: () => req<{ ok: boolean; error?: string; model?: string; reply?: string }>("/settings/test-llm", { method: "POST" }),
  clearSignals: () => req<{ cleared: number }>("/signals/clear", { method: "POST" }),
  getDbStats: () => req<DbStats>("/meta/db-stats"),
};

export type { Diff };