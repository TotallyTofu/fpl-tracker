// Client-side mirror of backend/app/optimizer/rules.py — same error codes,
// for instant draft feedback. The server re-validates on every save (source of truth).
import type { LineupPlayer, RuleError } from "./types";

const SQUAD_COMP: Record<number, number> = { 1: 2, 2: 5, 3: 5, 4: 3 };
const BUDGET = 1000;
const CLUB_LIMIT = 3;

export function validateClient(players: LineupPlayer[], bank: number): RuleError[] {
  const errors: RuleError[] = [];
  const ids = new Set<number>();
  const dup = players.some((p) => (ids.has(p.player_id) ? true : (ids.add(p.player_id), false)));
  if (players.length !== 15 || dup)
    errors.push({ code: "SQUAD_SIZE", message: `Squad must contain 15 distinct players (got ${players.length}${dup ? ", duplicates present" : ""})`, severity: "error" });

  const comp: Record<number, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };
  for (const p of players) comp[p.element_type] = (comp[p.element_type] || 0) + 1;
  for (const pos of [1, 2, 3, 4])
    if (comp[pos] !== SQUAD_COMP[pos])
      errors.push({ code: "SQUAD_COMPOSITION", message: `Squad must be 2 GK / 5 DEF / 5 MID / 3 FWD (got ${comp[1]}/${comp[2]}/${comp[3]}/${comp[4]})`, severity: "error" });

  // v1.0: a real squad can be worth more than £100m after price rises — a warning, not an error.
  const total = players.reduce((s, p) => s + p.now_cost, 0);
  if (total > BUDGET)
    errors.push({ code: "BUDGET_EXCEEDED", message: `Squad is worth £${(total / 10).toFixed(1)}m, over the £100.0m starting budget. Fine if your players have risen in price.`, severity: "warning" });

  const clubs: Record<number, number> = {};
  for (const p of players) clubs[p.team] = (clubs[p.team] || 0) + 1;
  for (const [team, n] of Object.entries(clubs))
    if (n > CLUB_LIMIT)
      errors.push({ code: "CLUB_LIMIT", message: `Max 3 players per club (got ${n} from team ${team})`, severity: "error" });

  const xi = players.filter((p) => p.role === "starter");
  const bench = players.filter((p) => p.role === "bench");
  if (xi.length !== 11) errors.push({ code: "XI_SIZE", message: `XI must have exactly 11 starters (got ${xi.length})`, severity: "error" });
  if (bench.length !== 4) errors.push({ code: "BENCH_SIZE", message: `Bench must be exactly 4 (got ${bench.length})`, severity: "error" });

  const orders = bench.map((p) => p.bench_order).sort((a, b) => (a || 0) - (b || 0));
  if (bench.length === 4 && JSON.stringify(orders) !== JSON.stringify([1, 2, 3, 4]))
    errors.push({ code: "BENCH_ORDER", message: "Bench orders must be 1–4, unique", severity: "error" });
  const benchGks = bench.filter((p) => p.element_type === 1);
  if (bench.length === 4 && benchGks.length === 1 && benchGks[0].bench_order !== 1)
    errors.push({ code: "BENCH_GK_SLOT", message: "The substitute goalkeeper must be in the first bench slot", severity: "error" });

  const xiGk = xi.filter((p) => p.element_type === 1).length;
  const xiDef = xi.filter((p) => p.element_type === 2).length;
  const xiFwd = xi.filter((p) => p.element_type === 4).length;
  if (xiGk !== 1) errors.push({ code: "XI_POSITION_MIN", message: `XI must have exactly 1 GK (got ${xiGk})`, severity: "error" });
  if (xiDef < 3) errors.push({ code: "XI_POSITION_MIN", message: `XI must have at least 3 DEF (got ${xiDef})`, severity: "error" });
  if (xiFwd < 1) errors.push({ code: "XI_POSITION_MIN", message: `XI must have at least 1 FWD (got ${xiFwd})`, severity: "error" });

  const xiIds = new Set(xi.map((p) => p.player_id));
  const caps = players.filter((p) => p.is_captain);
  const vcs = players.filter((p) => p.is_vice_captain);
  if (caps.length !== 1) errors.push({ code: "CAPTAIN_NOT_IN_XI", message: `Exactly one captain required (got ${caps.length})`, severity: "error" });
  else if (!xiIds.has(caps[0].player_id)) errors.push({ code: "CAPTAIN_NOT_IN_XI", message: "Captain must be in the XI", severity: "error" });
  if (vcs.length !== 1) errors.push({ code: "VICE_CAPTAIN_NOT_IN_XI", message: `Exactly one vice-captain required (got ${vcs.length})`, severity: "error" });
  else if (!xiIds.has(vcs[0].player_id)) errors.push({ code: "VICE_CAPTAIN_NOT_IN_XI", message: "Vice-captain must be in the XI", severity: "error" });
  if (caps.length === 1 && vcs.length === 1 && caps[0].player_id === vcs[0].player_id)
    errors.push({ code: "CAPTAIN_VC_SAME", message: "Captain and vice-captain must be different players", severity: "error" });

  if (bank < 0 || bank > 5) errors.push({ code: "BANK_RANGE", message: `Free transfers must be 0–5 (got ${bank})`, severity: "error" });

  for (const p of players)
    if (p.can_select === 0 || p.status === "u" || p.status === "s")
      errors.push({ code: "PLAYER_UNAVAILABLE", message: `${p.web_name} is unavailable (cannot select this GW)`, severity: "warning" });

  return errors;
}