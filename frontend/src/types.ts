// Shared TS types mirroring PLAN.MD §8 (backend API shapes).

// A23: mirrors season.active_chip_windows() exactly — there is no `event`.
export interface ChipWindow {
  chip: string;
  set: number;
  start_event: number;
  stop_event: number;
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

export type LastMatch = "started" | "came_on" | "no_minutes";

/** Below this chance of starting, a player gets a rotation-risk chip. */
export const ROTATION_RISK = 0.6;

const LAST_MATCH_TEXT: Record<LastMatch, string> = {
  started: "started",
  came_on: "came on",
  no_minutes: "did not play",
};

/** "Starts ~45%: started 2 of last 5; last match: came on" (tooltip text). */
export function startTitle(p: { p_start?: number | null; last_match?: LastMatch | null; start_rate5?: number | null }): string {
  if (p.p_start == null) return "";
  const parts = [`Starts ~${Math.round(p.p_start * 100)}%`];
  const detail: string[] = [];
  if (p.start_rate5 != null) detail.push(`started ${Math.round(p.start_rate5 * 5)} of last 5`);
  if (p.last_match) detail.push(`last match: ${LAST_MATCH_TEXT[p.last_match]}`);
  return detail.length ? `${parts[0]} — ${detail.join("; ")}` : parts[0];
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
  /** v1.0 display extras from GET /lineups/{id} */
  form?: number | null;
  points_per_game?: number | null;
  news?: string | null;
  team_short?: string | null;
  /** solver projection (suggested squads only) */
  ep?: number;
  /** v1.1 rotation risk (display only): P(starts next GW), what happened in the
   *  last match, and the share of his recent team matches he started. */
  p_start?: number | null;
  last_match?: LastMatch | null;
  start_rate5?: number | null;
  /** "Keep players in lineup": plans never sell this player (stored lineups only) */
  keep?: boolean;
}

// A23: a suggested squad (SuggestedLineup.squad) is a LineupPlayer minus
// bought_cost — purchase prices only exist for stored lineups, and the solver
// has no way to know them for players you don't own yet.
export type SquadPlayer = Omit<LineupPlayer, "bought_cost"> & { bought_cost?: number | null };

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
  /** money in the bank (£0.1m): the user's figure, else the £100m − squad estimate */
  budget_remaining: number;
  bank_money: number | null;
  bank_gw: number | null;
  money_known: boolean;
}

export interface LineupSummary {
  id: number;
  name: string;
  transfer_bank: number;
  bank_money?: number | null;
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
  ep?: number | null;
}

export interface TransferOut {
  player_id: number;
  web_name: string;
  sell_value: number;
  ep?: number | null;
}

export interface Diff {
  transfers_in: TransferIn[];
  transfers_out: TransferOut[];
  cost_delta: number;
  total_cost_after: number;
  free_transfers_used: number;
  bank_after: number;
  penalty_points: number;
  transfer_cap_exceeded?: boolean;
  chip_covers?: boolean;
  chip_played?: string | null;
  money_known?: boolean;
  bank_before?: number;
  budget_before?: number;
  budget_after?: number;
  /** players the user kept when this plan was made (absent on older plans) */
  kept_ids?: number[];
}

export interface ChipAdvice {
  chip: string;
  recommendation: "use" | "consider" | "skip";
  reason: string;
}

export interface SuggestedLineup {
  squad: SquadPlayer[];
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
  projected_points: {
    baseline: number;
    adjusted: number;
    with_captain: number;
    penalty_points?: number;
    net_after_transfers?: number;
    /** projected points of the saved team unchanged (same model) */
    current_team?: number | null;
  };
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
  max_tokens: number;
  player_list_mode: string; // "filtered" | "full"
  disable_thinking?: boolean;
  llm_wait_hours?: number;
}

export interface YouTubeChannel {
  handle: string;
  channel_id: string;
}

export interface FplSource {
  enabled: boolean;
  bootstrap_interval_min: number;
  live_players_interval_sec: number;
  live_matches_interval_sec: number;
}

export interface EspnSource {
  enabled: boolean;
  news_interval_min: number;
  live_interval_sec: number;
  fetch_bodies?: boolean;
  max_bodies_per_poll?: number;
}

export interface BbcSource {
  enabled: boolean;
  interval_min: number;
  fetch_bodies: boolean;
  max_bodies_per_poll: number;
}

export interface RedditSource {
  enabled: boolean;
  interval_min: number;
  mode: string; // "rss" | "oauth"
  oauth_client_id: string;
  oauth_client_secret: string;
}

export interface YouTubeSource {
  enabled: boolean;
  interval_min: number;
  channels: YouTubeChannel[];
  transcript_keywords: string[];
  max_transcripts_per_poll: number;
  llm_truncate_chars: number;
}

export interface SourcesConfig {
  fpl: FplSource;
  espn: EspnSource;
  bbc: BbcSource;
  reddit: RedditSource;
  youtube: YouTubeSource;
}

export interface OptimizerWeights {
  ep: number;
  form: number;
  fixture: number;
}

export interface AvailabilityConfig {
  active: boolean;
  doubt: number;
  chance_null: number;
  chance_100: number;
  chance_75: number;
  chance_50: number;
  chance_25: number;
  chance_0: number;
}

/** v1.1: share of a fit player's points that a flagged player delivers. */
export interface AvailabilityCurve {
  play_75: number;
  play_50: number;
  play_25: number;
}

/** v1.1: blend of a minutes-based estimate into the projection. */
export interface MinutesModelConfig {
  enabled: boolean;
  weight: number;
}

export interface SignalConfig {
  neg_per: number;
  neg_cap: number;
  pos_per: number;
  pos_cap: number;
}

export interface SolverConfig {
  restarts: number;
  timebox_sec: number;
  seed: number;
}

export interface OptimizerConfig {
  weights: OptimizerWeights;
  availability: AvailabilityConfig;
  availability_curve: AvailabilityCurve;
  minutes_model: MinutesModelConfig;
  signal: SignalConfig;
  differential_lambda: number;
  differential_ep_floor: number;
  solver: SolverConfig;
}

export interface GroupConfig {
  fpl_entry_id: string;
}

export interface EntryLeaguePhase {
  phase: number;
  rank: number | null;
  rank_count: number | null;
  total: number | null;
  entry_percentile_rank: number | null;
}

export interface EntryLeague {
  league_id: number | null;
  name: string | null;
  class: string | null;
  league_type: string | null;
  rank: number | null;
  size: number | null;
  points: number | null;
  percentile: number | null;
  active_phases: EntryLeaguePhase[];
}

export interface EntryData {
  entry_id: number | null;
  name: string | null;
  overall_points: number | null;
  overall_rank: number | null;
  overall_rank_out_of: number | null;
  overall_percentile: number | null;
  leagues: EntryLeague[];
}

export interface UIConfig {
  theme: string;
}

export interface SettingsResponse {
  sources: SourcesConfig;
  llm: LLMConfig;
  optimizer: OptimizerConfig;
  group: GroupConfig;
  ui: UIConfig;
  maps?: Record<string, unknown>;
  llm_status: {
    ready: boolean;
    base_url: string;
    model: string | null;
    key_set: boolean;
    note: string;
  };
  reddit_status?: { secret_set: boolean };
  pulp_available?: boolean;
}

export interface DbStats {
  row_counts: Record<string, number>;
  db_size_bytes: number;
  recent_errors: {
    source: string;
    status: string;
    rows: number | null;
    error: string | null;
    finished_at: string;
  }[];
}

export interface Signal {
  id: number;
  player_id: number;
  web_name: string | null;
  team_code: string | null;
  category: "injury" | "suspension" | "selection" | "rotation" | "return" | "transfer" | "other";
  sentiment: "positive" | "negative" | "neutral";
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

export interface SourceHealth {
  source: "fpl-official" | "bbc" | "espn" | "reddit" | "youtube";
  enabled: boolean;
  last_poll: { status: string; rows: number | null; error: string | null; finished_at: string } | null;
  items: number;
  last_item: string | null;
  waiting: number;
  undated: number;
  skipped_by_llm_breaker: number;
  live_signals: number;
}

export interface SourcesResponse {
  sources: SourceHealth[];
  llm: {
    ready: boolean;
    model: string | null;
    base_url: string;
    disable_thinking: boolean;
    ok_7d: number;
    errors_7d: number;
    last: { status: string; error: string | null; finished_at: string } | null;
  };
}

export interface TeamFixture {
  gw: number;
  opp: string;
  home: boolean;
  d: number;
}

export interface TeamFixturesResponse {
  from_gw: number | null;
  count: number;
  teams: Record<string, { short: string; name: string }>;
  fixtures: Record<string, TeamFixture[]>;
}

export const CHIP_LABEL: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  triple_captain: "Triple Captain",
};

export const PROFILE_LABEL: Record<string, string> = {
  max_ep: "Best projected",
  differential: "Differential",
  safe: "Safe",
};

/** "£4.5m" from £0.1m units, or "—". */
export function money(m: number | null | undefined): string {
  return m == null ? "—" : `£${(m / 10).toFixed(1)}m`;
}

/** Signed one-decimal number: +2.8 / −1.2 / 0.0 */
export function signed(n: number): string {
  if (Math.abs(n) < 0.05) return "0.0";
  return `${n > 0 ? "+" : "−"}${Math.abs(n).toFixed(1)}`;
}

/** Player's fixture label for a GW: "ARS (H)" + difficulty class. */
export function fixtureFor(fx: TeamFixturesResponse | null, team: number, gw?: number | null):
  { label: string; cls: string } {
  const list = fx?.fixtures[String(team)] ?? [];
  const mine = gw ? list.filter((f) => f.gw === gw) : list.slice(0, 1);
  if (!fx) return { label: "", cls: "" };
  if (!mine.length) return { label: "blank", cls: "blank" };
  if (mine.length > 1) return { label: mine.map((f) => f.opp).join("+"), cls: `d${Math.min(...mine.map((f) => f.d))}` };
  const f = mine[0];
  return { label: `${f.opp} (${f.home ? "H" : "A"})`, cls: `d${f.d}` };
}

