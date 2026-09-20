// Shared TS types mirroring PLAN.MD §8 (backend API shapes).

export interface ChipWindow {
  chip: string;
  event: number;
  playable_next_gw: boolean;
}

export interface Season {
  season: string | null;
  current_gw: number | null;
  next_gw: number | null;
  deadline: string | null;
  deadline_is_past: boolean;
  live_mode: boolean;
  live_window: [string, string] | null;
  chip_windows: ChipWindow[];
  events_total: number;
  fixtures_next_gw: Fixture[];
}

export interface Fixture {
  event: number;
  home_team: number;
  away_team: number;
  home_name: string;
  away_name: string;
  kickoff_time: string;
  status: "not_started" | "in_play" | "finished";
  minutes: number | null;
  score_home: number | null;
  score_away: number | null;
}

export interface Player {
  id: number;
  web_name: string;
  first_name: string;
  second_name: string;
  known_name: string | null;
  team: number;
  team_name: string | null;
  element_type: 1 | 2 | 3 | 4;
  news: string | null;
  now_cost: number;
  value_form: number | null;
  ep_next: number | null;
  selected_by_percent: number | null;
  status: string | null;
  chance_of_playing_next_round: number | null;
  can_select: number;
  removed: number;
}

export interface RuleError {
  code: string;
  message: string;
  severity: "error" | "warning";
}

export interface LineupPlayer {
  player_id: number;
  web_name: string;
  element_type: 1 | 2 | 3 | 4;
  team: number;
  team_name: string | null;
  now_cost: number;
  status: string | null;
  can_select: number;
  ep_next: number | null;
  selected_by_percent: number | null;
  chance_of_playing_next_round: number | null;
  role: "starter" | "bench";
  bench_order: number | null;
  is_captain: boolean;
  is_vice_captain: boolean;
  bought_cost: number | null;
}

export interface Lineup {
  id: number;
  name: string;
  transfer_bank: number;
  chips: Record<string, number>;
  is_current: number;
  kind: "current" | "test";
  created_at: string;
  updated_at: string;
  players: LineupPlayer[];
  validation: { valid: boolean; errors: RuleError[] };
  budget_remaining: number;
}

export interface LineupSummary {
  id: number;
  name: string;
  transfer_bank: number;
  chips: Record<string, number>;
  is_current: number;
  kind: "current" | "test";
  created_at: string;
  updated_at: string;
}

export interface TransferIn {
  player_id: number;
  web_name: string;
  cost: number;
}

export interface TransferOut {
  player_id: number;
  web_name: string;
  sell_value: number;
}

export interface Diff {
  transfers_in: TransferIn[];
  transfers_out: TransferOut[];
  cost_delta: number;
  total_cost_after: number;
  free_transfers_used: number;
  bank_after: number;
  penalty_points: number;
}

export interface ChipAdvice {
  chip: string;
  recommendation: "use" | "consider" | "skip";
  reason: string;
}

export interface SuggestedLineup {
  squad: LineupPlayer[];
  xi: number[];
  captain: number;
  vice_captain: number;
  bench: number[];
}

export interface Suggestion {
  id: number;
  profile: "max_ep" | "differential" | "safe";
  variant_of: string | null;
  generated_at: string;
  target_gw: number;
  projected_points: { baseline: number; adjusted: number; with_captain: number };
  objective: number;
  diff: Diff;
  chip_advice: ChipAdvice[];
  rationale: { per_player: Record<string, string>; notes: string[] };
  lineup: SuggestedLineup;
  applied_at: string | null;
}

export interface MatchedPlayer {
  player_id: number;
  web_name: string;
  confidence: number;
  element_type: 1 | 2 | 3 | 4;
  team: number;
  team_name: string | null;
  now_cost: number;
  status: string | null;
  can_select: number;
  ep_next: number | null;
  selected_by_percent: number | null;
  chance_of_playing_next_round: number | null;
}

export interface NameMatch {
  input: string;
  matched: MatchedPlayer | null;
  candidates: MatchedPlayer[];
}

export interface LLMConfig {
  enabled: boolean;
  base_url: string;
  api_key: string;
  model: string;
  timeout_sec: number;
  batch_chars: number;
}

export interface SettingsResponse {
  sources: Record<string, unknown>;
  llm: LLMConfig;
  optimizer: Record<string, unknown>;
  group: Record<string, unknown>;
  ui: Record<string, unknown>;
  maps?: Record<string, unknown>;
  llm_status: {
    ready: boolean;
    base_url: string;
    model: string | null;
    key_set: boolean;
    note: string;
  };
}

export interface Signal {
  id: number;
  player_id: number;
  web_name: string | null;
  team_code: string | null;
  category: "injury" | "suspension" | "selection" | "return" | "transfer" | "other";
  sentiment: "positive" | "negative";
  confidence: number;
  summary: string;
  source: string;
  url: string | null;
  published_at: string | null;
  retrieved_at: string;
  expires_at: string;
  model: string;
}

export interface RawItem {
  id: number;
  source: string;
  external_id: string | null;
  kind: "article" | "thread" | "video" | "official-news";
  title: string | null;
  url: string | null;
  published_at: string | null;
  retrieved_at: string;
  body: string | null;
  takeaways: string[];
  processed: boolean;
}

export interface PollInfo {
  status: string;
  rows: number;
  error: string | null;
  finished_at: string;
}

export interface Health {
  db_ok: boolean;
  last_polls: Record<string, PollInfo>;
  schema_drift: string[];
}

export interface StartupRefreshState {
  status: "idle" | "running" | "done";
  started_at?: string;
  finished_at?: string;
  results?: Record<string, string | Record<string, unknown>>;
  signals_stored?: number;
}

export const POS_NAME: Record<number, string> = { 1: "GK", 2: "DEF", 3: "MID", 4: "FWD" };
export const POS_SHORT: Record<number, string> = { 1: "GK", 2: "D", 3: "M", 4: "F" };

export function cost(m: number): string {
  return `£${(m / 10).toFixed(1)}m`;
}

export function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}