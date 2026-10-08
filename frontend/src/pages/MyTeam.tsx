import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import LineupTabs from "../components/LineupTabs";
import PasteBox from "../components/PasteBox";
import PitchView, { LockIcon } from "../components/PitchView";
import PlayerPicker from "../components/PlayerPicker";
import ValidationPanel from "../components/ValidationPanel";
import { useSeason } from "../hooks/useSeason";
import { validateClient } from "../rules";
import type { Lineup, LineupPlayer, LineupSummary, MatchedPlayer, Player, SquadPlayer, TeamFixturesResponse } from "../types";
import { CHIP_LABEL, POS_NAME, ROTATION_RISK, money, startTitle } from "../types";

const CHIPS = ["wildcard", "freehit", "bboost", "triple_captain"] as const;
type ChipUse = Record<string, number | null>; // null = available; 0 = used (GW unknown); n = used in GW n

interface Draft {
  id: number | null;
  name: string;
  bank: number;
  bankMoney: number | null; // £0.1m; null = not entered
  chips: Record<string, number>;
  chipUse: ChipUse;
  kind: "current" | "test"; // A16: carried through so saves never promote a test copy
  players: LineupPlayer[];
}

const ep = (p: LineupPlayer) => p.ep_next ?? 0;

/** FPL bench: the GK sub takes slot 1; outfield subs keep their order in 2–4. */
function normalizeBench(players: LineupPlayer[]): LineupPlayer[] {
  const bench = players
    .filter((p) => p.role === "bench")
    .sort((a, b) => Number(b.element_type === 1) - Number(a.element_type === 1)
      || (a.bench_order ?? 9) - (b.bench_order ?? 9) || ep(b) - ep(a));
  const order = new Map(bench.map((p, i) => [p.player_id, i + 1]));
  return players.map((p) => (p.role === "bench"
    ? { ...p, bench_order: order.get(p.player_id) ?? null }
    : { ...p, bench_order: null }));
}

function toDraft(l: Lineup, chipUse: ChipUse = {}): Draft {
  return {
    id: l.id, name: l.name, bank: l.transfer_bank, bankMoney: l.bank_money ?? null,
    chips: l.chips, chipUse, kind: l.kind,
    // the API returns stored flags as 0/1; booleans keep JSX like `{p.is_captain && …}` from printing "0"
    players: normalizeBench(l.players.map((p) => ({ ...p, is_captain: Boolean(p.is_captain), is_vice_captain: Boolean(p.is_vice_captain) }))),
  };
}

function emptyDraft(): Draft {
  return {
    id: null, name: "My team", bank: 1, bankMoney: null,
    chips: { wildcard: 1, freehit: 1, bboost: 1, triple_captain: 1 },
    chipUse: {}, kind: "current", players: [],
  };
}

/** Recompute starter/bench roles: best GK + top-10 outfield by EP with ≥3 DEF
 *  and ≥1 FWD. Keeps captain/VC flags only for players still in the XI. */
function assignRoles(squad: LineupPlayer[], previous: LineupPlayer[]): LineupPlayer[] {
  if (squad.length === 0) return squad;
  const gks = squad.filter((p) => p.element_type === 1).sort((a, b) => ep(b) - ep(a));
  const rest = squad.filter((p) => p.element_type !== 1).sort((a, b) => ep(b) - ep(a));
  const xi: LineupPlayer[] = gks[0] ? [gks[0], ...rest.slice(0, 10)] : [...rest.slice(0, 11)];
  const ensure = (pos: number, count: number) => {
    let have = xi.filter((p) => p.element_type === pos).length;
    while (have < count) {
      const unselected = rest.filter((p) => p.element_type === pos && !xi.includes(p));
      const victims = xi.filter((p) => p.element_type !== pos && p.element_type !== 1);
      if (!unselected.length || !victims.length) break;
      const victim = victims.sort((a, b) => ep(a) - ep(b))[0];
      xi[xi.indexOf(victim)] = unselected.sort((a, b) => ep(b) - ep(a))[0];
      have += 1;
    }
  };
  ensure(4, 1);
  ensure(2, 3);
  const xiIds = new Set(xi.map((p) => p.player_id));
  const prevCap = previous.find((p) => p.is_captain);
  const prevVc = previous.find((p) => p.is_vice_captain);
  const benchByEp = squad.filter((p) => !xiIds.has(p.player_id)).sort((a, b) => ep(b) - ep(a));
  return normalizeBench(squad.map((p) => {
    const inXi = xiIds.has(p.player_id);
    return {
      ...p,
      role: inXi ? ("starter" as const) : ("bench" as const),
      bench_order: inXi ? null : benchByEp.indexOf(p) + 1,
      is_captain: !!prevCap && p.player_id === prevCap.player_id && inXi,
      is_vice_captain: !!prevVc && p.player_id === prevVc.player_id && inXi,
    };
  }));
}

/** Pick the swap victim when the target side is full: same position first,
 *  then lowest EP, then id (deterministic). */
function pickSwapVictim(candidates: LineupPlayer[], position: number): LineupPlayer | null {
  const samePos = candidates.filter((p) => p.element_type === position);
  const pool = samePos.length ? samePos : candidates;
  if (!pool.length) return null;
  return [...pool].sort((a, b) => ep(a) - ep(b) || a.player_id - b.player_id)[0];
}

/** Sorted ids of the kept players, to tell whether the Keep flags changed since the last save. */
const keepKey = (players: LineupPlayer[]) =>
  players.filter((p) => p.keep).map((p) => p.player_id).sort((a, b) => a - b).join(",");

const sellValue = (now: number, paid: number | null) => {
  const b = paid ?? now;
  return now > b ? b + Math.floor((now - b) / 2) : now;
};

type SortKey = "element_type" | "web_name" | "now_cost" | "ep_next" | "selected_by_percent";

export default function MyTeam() {
  const nav = useNavigate();
  const { season } = useSeason();
  const [lineups, setLineups] = useState<LineupSummary[]>([]);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [savedChipUse, setSavedChipUse] = useState<ChipUse>({});
  const [savedKeep, setSavedKeep] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("element_type");
  const [sortDir, setSortDir] = useState(1);
  const [fixtures, setFixtures] = useState<TeamFixturesResponse | null>(null);

  const refreshLineups = useCallback(() => {
    api.listLineups().then((r) => setLineups(r.lineups)).catch(() => {});
  }, []);

  // chip windows that cover the upcoming GW (2026/27: one set per half-season)
  const windowOf = useCallback((chip: string) =>
    season?.chip_windows.find((w) => w.chip === chip && w.playable_next_gw)
    ?? season?.chip_windows.find((w) => w.chip === chip) ?? null, [season]);

  const chipUseFor = useCallback(async (l: Lineup): Promise<ChipUse> => {
    const plays = (await api.getChipPlays().catch(() => ({ chip_plays: [] }))).chip_plays;
    const use: ChipUse = {};
    for (const c of CHIPS) {
      const w = windowOf(c);
      const inWindow = plays.filter((p) => p.chip === c && (!w || (p.gw >= w.start_event && p.gw <= w.stop_event)));
      use[c] = (l.chips[c] ?? 0) > 0 ? null : inWindow.length ? inWindow[inWindow.length - 1].gw : 0;
    }
    return use;
  }, [windowOf]);

  const loadLineup = useCallback((id: number) => {
    api.getLineup(id).then(async (l) => {
      const use = await chipUseFor(l);
      setDraft(toDraft(l, use));
      setSavedChipUse(use);
      setSavedKeep(keepKey(l.players));
      setSelectedId(null);
      setSaveMsg(null);
    }).catch(() => {});
  }, [chipUseFor]);

  useEffect(() => {
    refreshLineups();
    api.getTeamFixtures(3).then(setFixtures).catch(() => {});
  }, [refreshLineups]);

  // load the current lineup once the season (chip windows) is known
  useEffect(() => {
    if (!season || draft.id !== null) return;
    api.listLineups().then((r) => {
      const cur = r.lineups.find((l) => l.is_current) ?? r.lineups[0];
      if (cur) loadLineup(cur.id);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [season]);

  const patch = (fn: (d: Draft) => Draft) =>
    setDraft((d) => {
      const n = fn(d);
      return n === d ? d : { ...n, players: normalizeBench(n.players) };
    });

  const newPlayer = (p: Player | MatchedPlayer, id: number, role: "starter" | "bench", order: number | null): LineupPlayer => ({
    player_id: id, web_name: p.web_name, element_type: p.element_type, team: p.team, team_name: p.team_name,
    now_cost: p.now_cost, status: p.status, can_select: p.can_select, ep_next: p.ep_next,
    selected_by_percent: p.selected_by_percent, chance_of_playing_next_round: p.chance_of_playing_next_round,
    role, bench_order: order, is_captain: false, is_vice_captain: false, bought_cost: p.now_cost,
    keep: false, // a new signing, or a replacement, is never kept
  });

  const addPlayer = (p: Player) =>
    patch((d) => {
      if (d.players.length >= 15) return d;
      // FPL allows exactly 1 GK in the XI, so a second GK always starts on the bench.
      const xiFull = d.players.filter((x) => x.role === "starter").length >= 11;
      const gkStarted = p.element_type === 1 && d.players.some((x) => x.role === "starter" && x.element_type === 1);
      const benchCount = d.players.filter((x) => x.role === "bench").length;
      const toBench = xiFull || gkStarted;
      if (toBench && benchCount >= 4) return d;
      return { ...d, players: [...d.players, newPlayer(p, p.id, toBench ? "bench" : "starter", toBench ? benchCount + 1 : null)] };
    });

  const swapPlayer = (p: Player) =>
    patch((d) => {
      const idx = d.players.findIndex((x) => x.player_id === selectedId);
      if (idx < 0) return d;
      const old = d.players[idx];
      const players = [...d.players];
      players[idx] = { ...newPlayer(p, p.id, old.role, old.bench_order), is_captain: old.is_captain, is_vice_captain: old.is_vice_captain };
      setSelectedId(p.id);
      return { ...d, players };
    });

  const loadMatched = (matched: MatchedPlayer[]) =>
    patch((d) => {
      const ids = new Set(matched.map((m) => m.player_id));
      const others = d.players.filter((p) => !ids.has(p.player_id));
      // a player pasted again keeps his Keep flag
      const added = matched.map((m) => ({
        ...newPlayer(m, m.player_id, "starter", null),
        keep: d.players.find((x) => x.player_id === m.player_id)?.keep ?? false,
      }));
      return { ...d, players: assignRoles([...others, ...added], d.players) };
    });

  const toggleKeep = (id: number) => patch((d) => ({
    ...d, players: d.players.map((p) => (p.player_id === id ? { ...p, keep: !p.keep } : p)),
  }));
  const clearKeep = () => patch((d) => ({ ...d, players: d.players.map((p) => ({ ...p, keep: false })) }));

  const setCaptain = () => patch((d) => ({
    ...d,
    players: d.players.map((p) => ({
      ...p, is_captain: p.player_id === selectedId,
      is_vice_captain: p.player_id === selectedId ? false : p.is_vice_captain,
    })),
  }));
  const setVice = () => patch((d) => ({
    ...d,
    players: d.players.map((p) => ({
      ...p, is_vice_captain: p.player_id === selectedId,
      is_captain: p.player_id === selectedId ? false : p.is_captain,
    })),
  }));

  /** Put an outfield sub at priority k (1–3). */
  const setSubPriority = (k: number) => patch((d) => {
    const subs = d.players.filter((p) => p.role === "bench" && p.element_type !== 1)
      .sort((a, b) => (a.bench_order ?? 9) - (b.bench_order ?? 9));
    const me = subs.find((p) => p.player_id === selectedId);
    if (!me) return d;
    const rest = subs.filter((p) => p !== me);
    rest.splice(Math.max(0, Math.min(k - 1, rest.length)), 0, me);
    const hasGk = d.players.some((p) => p.role === "bench" && p.element_type === 1);
    const order = new Map(rest.map((p, i) => [p.player_id, i + (hasGk ? 2 : 1)]));
    return { ...d, players: d.players.map((p) => (order.has(p.player_id) ? { ...p, bench_order: order.get(p.player_id)! } : p)) };
  });

  const removePlayer = () => patch((d) => {
    const removed = d.players.find((p) => p.player_id === selectedId);
    let players = d.players.filter((p) => p.player_id !== selectedId);
    // A starter leaving auto-promotes the best eligible bench player (same position first).
    if (removed?.role === "starter") {
      const bench = players.filter((p) => p.role === "bench").sort((a, b) =>
        Number(b.element_type === removed.element_type) - Number(a.element_type === removed.element_type) || ep(b) - ep(a));
      if (bench[0]) players = players.map((p) => (p.player_id === bench[0].player_id ? { ...p, role: "starter" as const } : p));
    }
    setSelectedId(null);
    return { ...d, players };
  });

  // Demote a starter. Captain/VC must stay in the XI. With a full bench the
  // move becomes a swap with the lowest-EP outfield sub (same position first).
  const moveToBench = (id: number) => patch((d) => {
    const p = d.players.find((x) => x.player_id === id);
    if (!p || p.role !== "starter" || p.is_captain || p.is_vice_captain) return d;
    const bench = d.players.filter((x) => x.role === "bench");
    if (bench.length < 4) {
      return { ...d, players: d.players.map((x) => (x.player_id === id ? { ...x, role: "bench" as const, bench_order: 9 } : x)) };
    }
    const victim = pickSwapVictim(bench.filter((x) => x.element_type !== 1), p.element_type);
    if (!victim) return d;
    return {
      ...d,
      players: d.players.map((x) => (x.player_id === id ? { ...x, role: "bench" as const, bench_order: victim.bench_order }
        : x.player_id === victim.player_id ? { ...x, role: "starter" as const, bench_order: null } : x)),
    };
  });

  // Promote a bench player. A GK swaps with the starting GK; otherwise, with a
  // full XI, the lowest-EP non-C/VC starter (same position first) drops.
  const moveToXi = (id: number) => patch((d) => {
    const p = d.players.find((x) => x.player_id === id);
    if (!p || p.role !== "bench") return d;
    let players = d.players;
    if (p.element_type === 1) {
      const gk = players.find((x) => x.role === "starter" && x.element_type === 1);
      if (gk) players = players.map((x) => (x.player_id === gk.player_id ? { ...x, role: "bench" as const, bench_order: p.bench_order } : x));
    } else if (players.filter((x) => x.role === "starter").length >= 11) {
      const victim = pickSwapVictim(players.filter((x) => x.role === "starter" && !x.is_captain && !x.is_vice_captain && x.element_type !== 1), p.element_type);
      if (!victim) return d;
      players = players.map((x) => (x.player_id === victim.player_id ? { ...x, role: "bench" as const, bench_order: p.bench_order } : x));
    }
    return { ...d, players: players.map((x) => (x.player_id === id ? { ...x, role: "starter" as const, bench_order: null } : x)) };
  });

  const reorderBench = (draggedId: number, targetId: number) => patch((d) => {
    const a = d.players.find((p) => p.player_id === draggedId);
    const b = d.players.find((p) => p.player_id === targetId);
    if (!a || !b || a.role !== "bench" || b.role !== "bench" || a.element_type === 1 || b.element_type === 1) return d;
    return {
      ...d,
      players: d.players.map((p) => (p.player_id === draggedId ? { ...p, bench_order: b.bench_order }
        : p.player_id === targetId ? { ...p, bench_order: a.bench_order } : p)),
    };
  });

  const setPaid = (id: number, v: number | null) =>
    patch((d) => ({ ...d, players: d.players.map((p) => (p.player_id === id ? { ...p, bought_cost: v } : p)) }));

  const setChipUse = (chip: string, v: number | null) =>
    patch((d) => ({ ...d, chipUse: { ...d.chipUse, [chip]: v }, chips: { ...d.chips, [chip]: v === null ? 1 : 0 } }));

  const selected = draft.players.find((p) => p.player_id === selectedId) ?? null;
  const keptIds = useMemo(
    () => new Set(draft.players.filter((p) => p.keep).map((p) => p.player_id)), [draft.players]);
  const keepUnsaved = draft.id !== null && keepKey(draft.players) !== savedKeep;
  const clientErrors = useMemo(() => validateClient(draft.players, draft.bank), [draft.players, draft.bank]);
  const totalCost = draft.players.reduce((s, p) => s + p.now_cost, 0);
  const sellTotal = draft.players.reduce((s, p) => s + sellValue(p.now_cost, p.bought_cost), 0);
  const formation = [2, 3, 4].map((pos) => draft.players.filter((p) => p.role === "starter" && p.element_type === pos).length).join("-");

  const sorted = useMemo(() => [...draft.players].sort((a, b) => {
    const av = a[sortKey] ?? -1;
    const bv = b[sortKey] ?? -1;
    if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv) * sortDir;
    return (((av as number) - (bv as number)) || (a.role === "starter" ? -1 : 1) - (b.role === "starter" ? -1 : 1)) * sortDir;
  }), [draft.players, sortKey, sortDir]);
  const clickSort = (k: SortKey) => {
    if (k === sortKey) setSortDir(-sortDir);
    else { setSortKey(k); setSortDir(1); }
  };

  const syncChipLog = async (): Promise<string | null> => {
    if (draft.kind !== "current") return null;
    for (const c of CHIPS) {
      const before = savedChipUse[c] ?? null;
      const after = draft.chipUse[c] ?? null;
      if (before === after) continue;
      if (before) await api.unlogChipPlay(before, c).catch(() => {});
      if (after) {
        try {
          await api.logChipPlay(after, c);
        } catch (e) {
          return e instanceof ApiError ? e.message : String(e);
        }
      }
    }
    return null;
  };

  const save = async () => {
    setSaving(true);
    setSaveMsg(null);
    try {
      const body = {
        name: draft.name,
        transfer_bank: draft.bank,
        bank_money: draft.bankMoney,
        chips: draft.chips,
        kind: draft.kind, // A16: saving a test copy never promotes it
        players: draft.players.map((p) => ({
          player_id: p.player_id, role: p.role, bench_order: p.bench_order,
          is_captain: p.is_captain, is_vice_captain: p.is_vice_captain,
          bought_cost: p.bought_cost, // A15: real purchase prices
          keep: p.keep ?? false,      // plans never sell a kept player
        })),
      };
      const saved = draft.id ? await api.updateLineup(draft.id, body) : await api.createLineup(body);
      const chipErr = await syncChipLog();
      setDraft(toDraft(saved, draft.chipUse));
      setSavedChipUse(draft.chipUse);
      setSavedKeep(keepKey(saved.players));
      refreshLineups();
      setSaveMsg(chipErr ? { ok: false, text: `Team saved, but the chip log was not: ${chipErr}` } : { ok: true, text: "Team saved." });
    } catch (e) {
      setSaveMsg({
        ok: false,
        text: e instanceof ApiError ? (e.errors.length ? e.errors.map((x) => x.message).join(" · ") : e.message) : String(e),
      });
    } finally {
      setSaving(false);
    }
  };

  const newLineup = () => {
    setDraft(emptyDraft());
    setSavedChipUse({});
    setSavedKeep("");
    setSelectedId(null);
  };
  const renameLineup = (id: number, name: string) => {
    api.getLineup(id).then((l) => api.updateLineup(id, {
      name, transfer_bank: l.transfer_bank, bank_money: l.bank_money, chips: l.chips, kind: l.kind,
      players: l.players.map((p) => ({
        player_id: p.player_id, role: p.role, bench_order: p.bench_order,
        is_captain: p.is_captain, is_vice_captain: p.is_vice_captain, bought_cost: p.bought_cost,
        keep: p.keep,
      })),
    })).then(refreshLineups).catch(() => {});
  };
  const duplicateLineup = (id: number) => {
    api.duplicateLineup(id).then((l) => { refreshLineups(); loadLineup(l.id); }).catch(() => {});
  };
  const deleteLineupById = (id: number) => {
    if (!window.confirm("Delete this team?")) return;
    api.deleteLineup(id).then(() => { refreshLineups(); if (draft.id === id) newLineup(); });
  };

  const benchSubs = draft.players.filter((p) => p.role === "bench" && p.element_type !== 1)
    .sort((a, b) => (a.bench_order ?? 9) - (b.bench_order ?? 9));
  const nextGw = season?.next_gw ?? null;
  const hard = clientErrors.filter((e) => e.severity === "error").length;

  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>My team</h1>
          <p className="lede">Enter your team exactly as it is on the FPL site. Plans start from it.</p>
        </div>
        <LineupTabs
          lineups={lineups}
          activeId={draft.id}
          onSelect={loadLineup}
          onNew={newLineup}
          onRename={renameLineup}
          onDuplicate={duplicateLineup}
          onDelete={deleteLineupById}
          onSetCurrent={(id) => api.setCurrent(id).then(refreshLineups)}
          onSuggest={(id) => nav("/suggestions", { state: { lineup_id: id } })}
        />
      </section>

      <div className="split">
        <section className="main" aria-labelledby="xi-h">
          <div className="page-head" style={{ alignItems: "center" }}>
            <h2 id="xi-h" style={{ margin: 0 }}>
              {draft.players.length === 15 ? `Starting XI · ${formation}` : `Squad ${draft.players.length}/15`}
            </h2>
            <button type="button" className="ghost" onClick={() => patch((d) => ({ ...d, players: assignRoles(d.players, d.players) }))}
              title="Pick the XI and bench from projected points (replaces your arrangement)">
              Auto-pick XI
            </button>
          </div>

          {selected ? (
            <div className="card" style={{ padding: "12px 16px" }}>
              <div className="row">
                <b>{selected.web_name}</b>
                <span className="small muted">{POS_NAME[selected.element_type]} · {selected.role === "starter" ? "starting" : "bench"}</span>
                <span className="spacer" />
                <button type="button" className="sm" onClick={setCaptain} disabled={selected.role !== "starter"}>Captain</button>
                <button type="button" className="sm ghost" onClick={setVice} disabled={selected.role !== "starter"}>Vice-captain</button>
                <button type="button" className={`sm ${selected.keep ? "" : "ghost"}`}
                  onClick={() => toggleKeep(selected.player_id)} aria-pressed={Boolean(selected.keep)}
                  title="Plans never sell a kept player, even with a Wildcard or Free Hit">
                  <LockIcon /> {selected.keep ? "Kept" : "Keep"}
                </button>
                {selected.role === "starter" ? (
                  <button type="button" className="sm ghost" onClick={() => moveToBench(selected.player_id)}
                    disabled={selected.is_captain || selected.is_vice_captain}
                    title={selected.is_captain || selected.is_vice_captain ? "Change the captaincy first" : ""}>
                    Send to bench
                  </button>
                ) : (
                  <button type="button" className="sm ghost" onClick={() => moveToXi(selected.player_id)}>Move to XI</button>
                )}
                {selected.role === "bench" && selected.element_type !== 1 && benchSubs.length > 1 &&
                  benchSubs.map((_, i) => (
                    <button key={i} type="button" className="sm ghost" onClick={() => setSubPriority(i + 1)}
                      aria-pressed={benchSubs[i].player_id === selected.player_id}>
                      Sub {i + 1}
                    </button>
                  ))}
                <button type="button" className="sm danger" onClick={removePlayer}>Remove</button>
              </div>
              <p className="help" style={{ marginTop: 6 }}>Pick a player in the list below to swap him in, or drag cards between the pitch and the bench. Keep stops the plans selling him.</p>
            </div>
          ) : (
            <p className="help">Click a player to make him captain, move him or swap him. Drag cards to move them.</p>
          )}

          <PitchView
            players={draft.players as SquadPlayer[]}
            selectedId={selectedId}
            onSelect={(p) => setSelectedId(selectedId === p.player_id ? null : p.player_id)}
            onMoveToBench={moveToBench}
            onMoveToXi={moveToXi}
            onBenchReorder={reorderBench}
            fixtures={fixtures}
            gw={nextGw}
            keptIds={keptIds}
          />
          <p className="help">Your goalkeeper sub has a fixed slot, as on the FPL site. Only the three outfield subs have an order.</p>
          {keptIds.size > 0 && <p className="help">Grey shirt with a lock: kept. Plans never sell that player.</p>}
        </section>

        <div className="side">
          <section className="card" aria-labelledby="facts-h" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <h2 id="facts-h" style={{ margin: 0 }}>Squad facts</h2>
            <div className="field">
              <label className="label" htmlFor="team-name">Team name</label>
              <input id="team-name" value={draft.name} onChange={(e) => patch((d) => ({ ...d, name: e.target.value }))} />
            </div>
            <div className="field">
              <span className="label" id="ft-label">Free transfers</span>
              <div className="stepper" role="group" aria-labelledby="ft-label">
                <button type="button" className="ghost" aria-label="One fewer" onClick={() => patch((d) => ({ ...d, bank: Math.max(0, d.bank - 1) }))}>−</button>
                <output aria-live="polite">{draft.bank}</output>
                <button type="button" className="ghost" aria-label="One more" onClick={() => patch((d) => ({ ...d, bank: Math.min(5, d.bank + 1) }))}>+</button>
              </div>
              <p className="help">For the GW{nextGw ?? ""} deadline, 0 to 5. Use 0 if you have already used this week's. It goes up by one by itself when a deadline passes.</p>
            </div>
            <div className="field">
              <label className="label" htmlFor="itb">Money in the bank</label>
              <span className="money">
                <span>£</span>
                <input id="itb" type="number" inputMode="decimal" min={0} step={0.1} placeholder="e.g. 0.5"
                  value={draft.bankMoney == null ? "" : (draft.bankMoney / 10).toString()}
                  onChange={(e) => patch((d) => ({ ...d, bankMoney: e.target.value === "" ? null : Math.max(0, Math.round(Number(e.target.value) * 10)) }))} />
                <span>m</span>
              </span>
              <p className="help">Copy “Bank” from the Transfers page on the FPL site. Plans then only spend money you have.</p>
            </div>
            <div className="field">
              <span className="label">Chips this half of the season</span>
              <ul className="list" style={{ border: "1px solid var(--line-soft)", borderRadius: 10 }}>
                {CHIPS.map((c) => {
                  const w = windowOf(c);
                  const v = draft.chipUse[c] ?? null;
                  const lastGw = nextGw ?? season?.current_gw ?? 38;
                  const gws: number[] = [];
                  for (let g = w?.start_event ?? 1; g <= Math.min(lastGw, w?.stop_event ?? 38); g++) gws.push(g);
                  return (
                    <li key={c} className="list-row" style={{ padding: "8px 12px", justifyContent: "space-between" }}>
                      <label htmlFor={`chip-${c}`} style={{ margin: 0, color: "var(--ink)", fontWeight: 600 }}>{CHIP_LABEL[c]}</label>
                      <select id={`chip-${c}`} value={v === null ? "a" : String(v)}
                        onChange={(e) => setChipUse(c, e.target.value === "a" ? null : Number(e.target.value))}>
                        <option value="a">Available{w ? ` until GW${w.stop_event}` : ""}</option>
                        <option value="0">Used</option>
                        {gws.map((g) => <option key={g} value={g}>{g === nextGw ? `Playing in GW${g}` : `Used in GW${g}`}</option>)}
                      </select>
                    </li>
                  );
                })}
              </ul>
              <p className="help">One chip per gameweek. The GW you pick also stops the app suggesting a Free Hit straight after one.</p>
            </div>
            {keptIds.size > 0 && (
              <div className="field">
                <span className="label">Keep players in lineup</span>
                <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
                  <span className="small"><b>{keptIds.size}</b> kept. Plans never sell them, even with a Wildcard or Free Hit.</span>
                  <button type="button" className="sm ghost" onClick={clearKeep}>Clear</button>
                </div>
              </div>
            )}
            <div className="row small muted" style={{ justifyContent: "space-between" }}>
              <span>Squad value {money(totalCost)}</span>
              <span>Sells for {money(sellTotal)}</span>
            </div>
          </section>

          <ValidationPanel errors={clientErrors} />

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <button type="button" className="lg" onClick={save} disabled={saving || !draft.name.trim() || hard > 0}>
              {saving ? "Saving…" : draft.id ? "Save team" : "Save new team"}
            </button>
            {draft.id && (
              <button type="button" className="ghost" onClick={() => nav("/suggestions", { state: { lineup_id: draft.id } })}>
                Make transfer plans
              </button>
            )}
            {keepUnsaved && <div className="alert warn">Keep changes aren't saved yet. Press Save team so plans use them.</div>}
            {saveMsg && <div className={`alert ${saveMsg.ok ? "ok" : "bad"}`}>{saveMsg.text}</div>}
            {draft.kind === "test" && <div className="alert info">Sandbox copy: changes here never touch your real team.</div>}
          </div>
        </div>
      </div>

      <section className="card flush" aria-labelledby="sq-h">
        <div className="card-head">
          <h2 id="sq-h">Squad</h2>
          <span className="small muted">Edit what you paid if it differs: you keep half of any price rise when you sell. Tick Keep and plans will never sell that player.</span>
        </div>
        <div className="table-wrap">
          <table style={{ minWidth: 1000 }}>
            <thead>
              <tr>
                <th style={{ paddingLeft: 20 }} title="Plans never sell a kept player, even with a Wildcard or Free Hit">Keep</th>
                <th className="sortable" onClick={() => clickSort("element_type")}>Pos</th>
                <th className="sortable" onClick={() => clickSort("web_name")}>Player</th>
                <th>Next 3</th>
                <th className="sortable num" onClick={() => clickSort("now_cost")}>Price</th>
                <th>Paid</th>
                <th className="num">Sells for</th>
                <th className="num">Form</th>
                <th className="sortable num" onClick={() => clickSort("ep_next")}>FPL xP</th>
                <th className="num" title="Chance of starting the next gameweek, from recent starts and the last match">Starts</th>
                <th className="sortable num" onClick={() => clickSort("selected_by_percent")}>Owned</th>
                <th style={{ paddingRight: 20 }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((p) => {
                const next = fixtures?.fixtures[String(p.team)] ?? [];
                const doubt = p.status === "d" || p.status === "i" || (p.chance_of_playing_next_round != null && p.chance_of_playing_next_round < 100);
                const out = p.status === "u" || p.status === "s" || p.can_select === 0;
                return (
                  <tr key={p.player_id} className={`clickable ${selectedId === p.player_id ? "selected" : ""} ${p.keep ? "kept" : ""}`}
                    onClick={() => setSelectedId(p.player_id)}>
                    <td style={{ paddingLeft: 20 }} onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" checked={Boolean(p.keep)} onChange={() => toggleKeep(p.player_id)}
                        aria-label={`Keep ${p.web_name}`} />
                    </td>
                    <td><span className="badge">{POS_NAME[p.element_type]}</span></td>
                    <td>
                      <b>{p.web_name}</b>{p.keep ? <> <span className="muted" title="Kept: plans never sell this player"><LockIcon /></span></> : null}{" "}
                      <span className="small muted">
                        {p.team_short ?? fixtures?.teams[String(p.team)]?.short ?? ""} · {p.role === "starter" ? "XI" : p.element_type === 1 ? "GK sub" : `sub ${(p.bench_order ?? 2) - (draft.players.some((x) => x.role === "bench" && x.element_type === 1) ? 1 : 0)}`}
                        {p.is_captain ? " · C" : ""}{p.is_vice_captain ? " · VC" : ""}
                      </span>
                    </td>
                    <td>
                      <span className="row" style={{ gap: 4, flexWrap: "nowrap" }}>
                        {next.slice(0, 3).map((f, i) => <span key={i} className={`fdr d${f.d}`}>{f.opp} ({f.home ? "H" : "A"})</span>)}
                      </span>
                    </td>
                    <td className="num">{money(p.now_cost)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <span className="money" style={{ minHeight: 32 }}>
                        <span>£</span>
                        <input type="number" min={0} step={0.1} style={{ width: 64, minHeight: 32 }}
                          aria-label={`Paid for ${p.web_name}`}
                          value={((p.bought_cost ?? p.now_cost) / 10).toString()}
                          onChange={(e) => setPaid(p.player_id, e.target.value === "" ? null : Math.max(0, Math.round(Number(e.target.value) * 10)))} />
                      </span>
                    </td>
                    <td className="num">{money(sellValue(p.now_cost, p.bought_cost))}</td>
                    <td className="num">{p.form != null ? Number(p.form).toFixed(1) : "—"}</td>
                    <td className="num"><b>{p.ep_next != null ? p.ep_next.toFixed(1) : "—"}</b></td>
                    <td className="num">
                      {p.p_start == null ? "—" : (
                        <span className={`badge ${p.p_start < 0.3 ? "out" : p.p_start < ROTATION_RISK ? "warn" : ""}`}
                          title={startTitle(p)}>
                          {Math.round(p.p_start * 100)}%
                        </span>
                      )}
                    </td>
                    <td className="num">{p.selected_by_percent != null ? `${p.selected_by_percent.toFixed(1)}%` : "—"}</td>
                    <td style={{ paddingRight: 20 }}>
                      <span className={`badge ${out ? "out" : doubt ? "warn" : "ok"}`}>
                        {out ? "Out" : doubt ? `${p.chance_of_playing_next_round ?? "?"}%${p.news ? ` · ${p.news.split(" - ")[0].toLowerCase()}` : ""}` : "Fit"}
                      </span>
                    </td>
                  </tr>
                );
              })}
              {sorted.length === 0 && (
                <tr><td colSpan={12} className="muted" style={{ padding: 20 }}>No players yet. Paste a list or search below.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-labelledby="add-h" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <h2 id="add-h" style={{ margin: 0 }}>{selected ? `Swap ${selected.web_name}` : "Add players"}</h2>
        <div className="grid2" style={{ alignItems: "start" }}>
          <PlayerPicker squad={draft.players} selectedId={selectedId} onAdd={addPlayer} onSwap={swapPlayer} />
          <PasteBox onLoad={loadMatched} />
        </div>
      </section>
    </div>
  );
}
