import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import LineupTabs from "../components/LineupTabs";
import MetaPanel from "../components/MetaPanel";
import PasteBox from "../components/PasteBox";
import PitchView from "../components/PitchView";
import PlayerPicker from "../components/PlayerPicker";
import ValidationPanel from "../components/ValidationPanel";
import { useSeason } from "../hooks/useSeason";
import { validateClient } from "../rules";
import type { Lineup, LineupPlayer, LineupSummary, MatchedPlayer, Player, SquadPlayer } from "../types";
import { POS_NAME, cost } from "../types";

interface Draft {
  id: number | null;
  name: string;
  bank: number;
  chips: Record<string, number>;
  kind: "current" | "test"; // A16: carried through so saves never promote a test copy
  players: LineupPlayer[];
}

// chips in hand default to 0; the stepper caps at 2 (FPL max per season)
const EMPTY_CHIPS = { wildcard: 0, freehit: 0, bboost: 0, triple_captain: 0 };
const CHIP_LABEL: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  triple_captain: "3× Captain",
};

function toDraft(l: Lineup): Draft {
  return {
    id: l.id,
    name: l.name,
    bank: l.transfer_bank,
    chips: l.chips,
    kind: l.kind,
    players: l.players,
  };
}

function emptyDraft(): Draft {
  return { id: null, name: "My team", bank: 1, chips: { ...EMPTY_CHIPS }, kind: "current", players: [] };
}

/** Recompute starter/bench roles + bench orders for a squad of ≤15.
 *  XI: best GK + top-10 by EP, enforcing ≥3 DEF and ≥1 FWD; bench ordered by EP.
 *  Keeps captain/VC flags only if that player survives into the XI. */
function assignRoles(squad: LineupPlayer[], previous: LineupPlayer[]): LineupPlayer[] {
  if (squad.length === 0) return squad;
  const ep = (p: LineupPlayer) => p.ep_next ?? 0;
  const gks = squad.filter((p) => p.element_type === 1).sort((a, b) => ep(b) - ep(a));
  const rest = squad.filter((p) => p.element_type !== 1).sort((a, b) => ep(b) - ep(a));
  const xi: LineupPlayer[] = gks[0] ? [gks[0], ...rest.slice(0, 10)] : [...rest.slice(0, 11)];

  const ensure = (pos: number, count: number) => {
    let have = xi.filter((p) => p.element_type === pos).length;
    while (have < count) {
      const unselected = rest.filter((p) => p.element_type === pos && !xi.includes(p));
      const victims = xi.filter((p) => p.element_type !== pos);
      if (!unselected.length || !victims.length) break;
      const victim = victims.sort((a, b) => ep(a) - ep(b))[0];
      const best = unselected.sort((a, b) => ep(b) - ep(a))[0];
      xi[xi.indexOf(victim)] = best;
      have += 1;
    }
  };
  ensure(4, 1);
  ensure(2, 3);

  const xiIds = new Set(xi.map((p) => p.player_id));
  const bench = squad.filter((p) => !xiIds.has(p.player_id)).sort((a, b) => ep(b) - ep(a));
  const prevCap = previous.find((p) => p.is_captain);
  const prevVc = previous.find((p) => p.is_vice_captain);
  const capStillIn = prevCap && xiIds.has(prevCap.player_id) ? prevCap.player_id : null;
  const vcStillIn = prevVc && xiIds.has(prevVc.player_id) ? prevVc.player_id : null;

  return squad.map((p) => {
    const inXi = xiIds.has(p.player_id);
    return {
      ...p,
      role: inXi ? ("starter" as const) : ("bench" as const),
      bench_order: inXi ? null : bench.indexOf(p) + 1,
      is_captain: p.player_id === capStillIn,
      is_vice_captain: p.player_id === vcStillIn,
    };
  });
}

/** Bench orders currently in use (1–4). */
const benchOrders = (d: Draft) =>
  d.players
    .filter((p) => p.role === "bench")
    .map((p) => p.bench_order)
    .filter((o): o is number => o != null);

/** First free bench slot (1–4), or null when the bench is full. */
const freeBenchSlot = (d: Draft) => [1, 2, 3, 4].find((o) => !benchOrders(d).includes(o)) ?? null;

/** Renumber bench players to exactly 1–4 (order kept, ties by EP). */
function renumberBench(players: LineupPlayer[]): LineupPlayer[] {
  const bench = players
    .filter((p) => p.role === "bench")
    .sort((a, b) => (a.bench_order ?? 9) - (b.bench_order ?? 9) || (b.ep_next ?? 0) - (a.ep_next ?? 0));
  const orderById = new Map(bench.map((p, i) => [p.player_id, i + 1]));
  return players.map((p) =>
    p.role === "bench" ? { ...p, bench_order: orderById.get(p.player_id) ?? null } : p
  );
}

/** Pick the swap victim when the target side is full: same position first,
 *  then lowest EP, then id (deterministic). */
function pickSwapVictim(candidates: LineupPlayer[], position: number): LineupPlayer | null {
  const samePos = candidates.filter((p) => p.element_type === position);
  const pool = samePos.length ? samePos : candidates;
  if (!pool.length) return null;
  return [...pool].sort(
    (a, b) => (a.ep_next ?? 0) - (b.ep_next ?? 0) || a.player_id - b.player_id
  )[0];
}

type SortKey = "web_name" | "element_type" | "team_name" | "now_cost" | "ep_next" | "selected_by_percent";

export default function MyTeam() {
  const nav = useNavigate();
  const { season } = useSeason();
  const [lineups, setLineups] = useState<LineupSummary[]>([]);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("element_type");
  const [sortDir, setSortDir] = useState(1);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [chipPlays, setChipPlays] = useState<{ gw: number; chip: string }[]>([]);

  const refreshLineups = useCallback(() => {
    api.listLineups().then((r) => setLineups(r.lineups)).catch(() => {});
  }, []);
  useEffect(refreshLineups, [refreshLineups]);

  // T4.3: chips the user actually played this GW (drives the Free-Hit ban)
  const loadChipPlays = useCallback(() => {
    if (!season?.current_gw) return;
    api.getChipPlays(season.current_gw).then((r) => setChipPlays(r.chip_plays)).catch(() => {});
  }, [season]);
  useEffect(loadChipPlays, [loadChipPlays]);
  const toggleChip = (chip: string) => {
    const gw = season?.current_gw;
    if (!gw) return;
    const on = chipPlays.some((c) => c.chip === chip);
    const p = on ? api.unlogChipPlay(gw, chip) : api.logChipPlay(gw, chip);
    p.then(loadChipPlays).catch(() => {});
  };

  // load the current lineup on first mount
  useEffect(() => {
    api
      .listLineups()
      .then((r) => {
        const cur = r.lineups.find((l) => l.is_current) ?? r.lineups[0];
        if (cur) return api.getLineup(cur.id).then((l) => setDraft(toDraft(l)));
      })
      .catch(() => {});
  }, []);

  const patch = (fn: (d: Draft) => Draft) => setDraft((d) => fn(d));

  const addPlayer = (p: Player) =>
    patch((d) => {
      if (d.players.length >= 15) return d;
      // Starter while the XI has room — but FPL allows exactly 1 GK in the XI,
      // so a second GK always starts on the bench.
      const xiFull = d.players.filter((x) => x.role === "starter").length >= 11;
      const gkAlreadyStarted = p.element_type === 1 && d.players.some((x) => x.role === "starter" && x.element_type === 1);
      const slot = freeBenchSlot(d);
      const toBench = xiFull || gkAlreadyStarted;
      if (toBench && slot == null) return d;
      const np: LineupPlayer = {
        player_id: p.id,
        web_name: p.web_name,
        element_type: p.element_type,
        team: p.team,
        team_name: p.team_name,
        now_cost: p.now_cost,
        status: p.status,
        can_select: p.can_select,
        ep_next: p.ep_next,
        selected_by_percent: p.selected_by_percent,
        chance_of_playing_next_round: p.chance_of_playing_next_round,
        role: toBench ? "bench" : "starter",
        bench_order: toBench ? slot : null,
        is_captain: false,
        is_vice_captain: false,
        bought_cost: p.now_cost,
      };
      return { ...d, players: [...d.players, np] };
    });

  const swapPlayer = (p: Player) =>
    patch((d) => {
      const idx = d.players.findIndex((x) => x.player_id === selectedId);
      if (idx < 0) return d;
      const old = d.players[idx];
      const np: LineupPlayer = {
        ...old,
        player_id: p.id,
        web_name: p.web_name,
        team: p.team,
        team_name: p.team_name,
        now_cost: p.now_cost,
        status: p.status,
        can_select: p.can_select,
        ep_next: p.ep_next,
        selected_by_percent: p.selected_by_percent,
        bought_cost: p.now_cost,
      };
      const players = [...d.players];
      players[idx] = np;
      return { ...d, players };
    });

  const loadMatched = (matched: MatchedPlayer[]) =>
    patch((d) => {
      const ids = new Set(matched.map((m) => m.player_id));
      const keep = d.players.filter((p) => !ids.has(p.player_id));
      const added: LineupPlayer[] = matched.map((m) => ({
        player_id: m.player_id,
        web_name: m.web_name,
        element_type: m.element_type,
        team: m.team,
        team_name: m.team_name,
        now_cost: m.now_cost,
        status: m.status,
        can_select: m.can_select,
        ep_next: m.ep_next,
        selected_by_percent: m.selected_by_percent,
        chance_of_playing_next_round: m.chance_of_playing_next_round,
        role: "starter",
        bench_order: null,
        is_captain: false,
        is_vice_captain: false,
        bought_cost: m.now_cost,
      }));
      return { ...d, players: assignRoles([...keep, ...added], d.players) };
    });

  const selectPlayer = (p: SquadPlayer) => setSelectedId(p.player_id);

  const setCaptain = () =>
    patch((d) => ({
      ...d,
      players: d.players.map((p) => ({
        ...p,
        is_captain: p.player_id === selectedId,
        is_vice_captain: p.player_id === selectedId ? false : p.is_vice_captain,
      })),
    }));

  const setVice = () =>
    patch((d) => ({
      ...d,
      players: d.players.map((p) => ({
        ...p,
        is_vice_captain: p.player_id === selectedId,
        is_captain: p.player_id === selectedId ? false : p.is_captain,
      })),
    }));

  const setBenchOrder = (order: number) =>
    patch((d) => {
      const others = d.players
        .filter((p) => p.role === "bench" && p.player_id !== selectedId)
        .map((p) => p.bench_order)
        .filter((o): o is number => o != null);
      const taken = new Set(others);
      const target = [1, 2, 3, 4].find((o) => o === order && !taken.has(o)) ?? [1, 2, 3, 4].find((o) => !taken.has(o));
      if (!target) return d;
      return {
        ...d,
        players: d.players.map((p) =>
          p.player_id === selectedId ? { ...p, bench_order: target } : p
        ),
      };
    });

  const removePlayer = () =>
    patch((d) => {
      const removed = d.players.find((p) => p.player_id === selectedId);
      let players = d.players.filter((p) => p.player_id !== selectedId);
      // If a starter leaves, auto-promote the best eligible bench player
      // (prefer the same position, e.g. a bench GK when the GK is removed;
      // otherwise the highest EP).
      if (removed?.role === "starter") {
        const bench = players
          .filter((p) => p.role === "bench")
          .sort(
            (a, b) =>
              (a.element_type === removed.element_type ? -1 : 0) -
              (b.element_type === removed.element_type ? -1 : 0) ||
              (b.ep_next ?? 0) - (a.ep_next ?? 0)
          );
        if (bench[0]) {
          players = players.map((p) =>
            p.player_id === bench[0].player_id
              ? { ...p, role: "starter" as const, bench_order: null }
              : p
          );
        }
      }
      return { ...d, players: renumberBench(players) };
    });

  // Demote a starter to the bench. FPL rule: captain/VC must stay in the XI,
  // so the UI blocks the move (the server would 422 with CAPTAIN_NOT_IN_XI anyway).
  // When the bench is full, the move becomes a swap: the lowest-EP eligible
  // bench player (same position first) takes the starter's XI slot.
  const moveToBench = (id: number) =>
    patch((d) => {
      const p = d.players.find((x) => x.player_id === id);
      if (!p || p.role !== "starter" || p.is_captain || p.is_vice_captain) return d;
      const slot = freeBenchSlot(d);
      if (slot != null) {
        return {
          ...d,
          players: d.players.map((x) =>
            x.player_id === id ? { ...x, role: "bench" as const, bench_order: slot } : x
          ),
        };
      }
      // Bench full: swap. A bench GK can't take the slot (the XI already has
      // its one GK), so exclude GKs from the candidates.
      const victim = pickSwapVictim(
        d.players.filter((x) => x.role === "bench" && x.element_type !== 1),
        p.element_type
      );
      if (!victim) return d;
      return {
        ...d,
        players: d.players.map((x) =>
          x.player_id === id
            ? { ...x, role: "bench" as const, bench_order: victim.bench_order }
            : x.player_id === victim.player_id
              ? { ...x, role: "starter" as const, bench_order: null }
              : x
        ),
      };
    });

  // Promote a bench player to the XI.
  // - Promoting a GK auto-benches the current GK (FPL: exactly 1 GK in the XI);
  //   the demoted GK takes the promoted GK's now-free bench slot, so this works
  //   even when the bench is full.
  // - When the XI is full, the move becomes a swap: the lowest-EP non-C/VC
  //   starter (same position first) takes the promoted player's bench slot.
  const moveToXi = (id: number) =>
    patch((d) => {
      const p = d.players.find((x) => x.player_id === id);
      if (!p || p.role !== "bench") return d;
      let players = d.players;
      if (p.element_type === 1) {
        const gk = players.find((x) => x.role === "starter" && x.element_type === 1);
        if (gk) {
          players = players.map((x) =>
            x.player_id === gk.player_id
              ? { ...x, role: "bench" as const, bench_order: p.bench_order }
              : x
          );
        }
      } else if (players.filter((x) => x.role === "starter").length >= 11) {
        const victim = pickSwapVictim(
          players.filter((x) => x.role === "starter" && !x.is_captain && !x.is_vice_captain),
          p.element_type
        );
        if (!victim) return d;
        players = players.map((x) =>
          x.player_id === id
            ? { ...x, role: "starter" as const, bench_order: null }
            : x.player_id === victim.player_id
              ? { ...x, role: "bench" as const, bench_order: p.bench_order }
              : x
        );
        return { ...d, players };
      }
      players = players.map((x) =>
        x.player_id === id ? { ...x, role: "starter" as const, bench_order: null } : x
      );
      return { ...d, players: renumberBench(players) };
    });

  // Drop a bench card onto another bench card: swap their bench orders.
  const reorderBench = (draggedId: number, targetId: number) =>
    patch((d) => {
      const dragged = d.players.find((p) => p.player_id === draggedId);
      const target = d.players.find((p) => p.player_id === targetId);
      if (!dragged || !target || dragged.role !== "bench" || target.role !== "bench") return d;
      const a = dragged.bench_order ?? 0;
      const b = target.bench_order ?? 0;
      const swapped = d.players.map((p) =>
        p.player_id === draggedId
          ? { ...p, bench_order: b || null }
          : p.player_id === targetId
            ? { ...p, bench_order: a || null }
            : p
      );
      return { ...d, players: renumberBench(swapped) };
    });

  // A15: editable real purchase price per row (drives the sell-on fee).
  const setPaid = (id: number, v: number | null) =>
    patch((d) => ({
      ...d,
      players: d.players.map((p) => (p.player_id === id ? { ...p, bought_cost: v } : p)),
    }));

  const selected = draft.players.find((p) => p.player_id === selectedId) ?? null;
  const totalCost = draft.players.reduce((s, p) => s + p.now_cost, 0);
  const clientErrors = useMemo(
    () => validateClient(draft.players, draft.bank),
    [draft.players, draft.bank]
  );

  const sorted = useMemo(() => {
    const arr = [...draft.players];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? -1;
      const bv = b[sortKey] ?? -1;
      if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv) * sortDir;
      return ((av as number) - (bv as number)) * sortDir;
    });
    return arr;
  }, [draft.players, sortKey, sortDir]);

  const clickSort = (k: SortKey) => {
    if (k === sortKey) setSortDir(-sortDir);
    else {
      setSortKey(k);
      setSortDir(1);
    }
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      // T4.3/A16: send the stored kind so saving a test copy doesn't promote it
      const body = {
        name: draft.name,
        transfer_bank: draft.bank,
        chips: draft.chips,
        kind: draft.kind,
        players: draft.players.map((p) => ({
          player_id: p.player_id,
          role: p.role,
          bench_order: p.bench_order,
          is_captain: p.is_captain,
          is_vice_captain: p.is_vice_captain,
          bought_cost: p.bought_cost, // A15: preserve real purchase prices
        })),
      };
      const saved = draft.id
        ? await api.updateLineup(draft.id, body)
        : await api.createLineup(body);
      setDraft(toDraft(saved));
      refreshLineups();
    } catch (e) {
      if (e instanceof ApiError)
        setSaveError(e.errors.length ? e.errors.map((x) => x.message).join(" · ") : e.message);
      else setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const loadLineup = (id: number) => {
    api.getLineup(id).then((l) => {
      setDraft(toDraft(l));
      setSelectedId(null);
    });
  };

  const newLineup = () => {
    setDraft(emptyDraft());
    setSelectedId(null);
  };

  const deleteLineup = () => {
    if (!draft.id) return;
    if (!confirm("Delete this lineup?")) return;
    api.deleteLineup(draft.id).then(() => {
      refreshLineups();
      newLineup();
    });
  };

  // T4.3: tab menu actions
  const renameLineup = (id: number, name: string) => {
    api
      .getLineup(id)
      .then((l) =>
        api.updateLineup(id, {
          name,
          transfer_bank: l.transfer_bank,
          chips: l.chips,
          kind: l.kind,
          players: l.players.map((p) => ({
            player_id: p.player_id,
            role: p.role,
            bench_order: p.bench_order,
            is_captain: p.is_captain,
            is_vice_captain: p.is_vice_captain,
            bought_cost: p.bought_cost, // A15: preserve real purchase prices
          })),
        })
      )
      .then(refreshLineups)
      .catch(() => {});
  };

  const duplicateLineup = (id: number) => {
    api
      .duplicateLineup(id)
      .then((l) => {
        refreshLineups();
        setDraft(toDraft(l));
        setSelectedId(null);
      })
      .catch(() => {});
  };

  const deleteLineupById = (id: number) => {
    if (!confirm("Delete this lineup?")) return;
    api.deleteLineup(id).then(() => {
      refreshLineups();
      if (draft.id === id) newLineup();
    });
  };

  const benchCount = draft.players.filter((p) => p.role === "bench").length;
  const xiCount = draft.players.filter((p) => p.role === "starter").length;
  // Who a full-side move would swap with (shown in the button tooltips).
  const benchSwapVictim =
    selected && selected.role === "starter" && benchCount >= 4
      ? pickSwapVictim(
          draft.players.filter((x) => x.role === "bench" && x.element_type !== 1),
          selected.element_type
        )
      : null;
  const xiSwapVictim =
    selected && selected.role === "bench" && selected.element_type !== 1 && xiCount >= 11
      ? pickSwapVictim(
          draft.players.filter((x) => x.role === "starter" && !x.is_captain && !x.is_vice_captain),
          selected.element_type
        )
      : null;

  return (
    <div>
      <h1>My Team</h1>
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
      <div className="grid2">
        <div>
          <PasteBox onLoad={loadMatched} />
          <PlayerPicker squad={draft.players} selectedId={selectedId} onAdd={addPlayer} onSwap={swapPlayer} />
        </div>
        <div>
          <ValidationPanel errors={clientErrors} />
          <div className="panel">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <h2 style={{ marginTop: 0 }}>Squad ({draft.players.length}/15)</h2>
              <button
                className="sm ghost"
                onClick={() => patch((d) => ({ ...d, players: assignRoles(d.players, d.players) }))}
                title="Recompute the XI and bench from projected points (overrides your manual arrangement)"
              >
                Auto-pick XI
              </button>
            </div>
            <div style={{ maxHeight: 300, overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th onClick={() => clickSort("element_type")}>Pos</th>
                    <th onClick={() => clickSort("web_name")}>Name</th>
                    <th onClick={() => clickSort("team_name")}>Club</th>
                    <th onClick={() => clickSort("now_cost")}>Cost</th>
                    <th title="Price you paid (defaults to the current price). Drives the sell-on fee in diffs.">Paid</th>
                    <th onClick={() => clickSort("ep_next")}>EP</th>
                    <th onClick={() => clickSort("selected_by_percent")}>Own %</th>
                    <th>Role</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((p) => (
                    <tr
                      key={p.player_id}
                      className={`clickable ${selectedId === p.player_id ? "selected" : ""}`}
                      onClick={() => selectPlayer(p)}
                    >
                      <td>
                        <span className={`badge ${["", "gk", "def", "mid", "fwd"][p.element_type]}`}>
                          {POS_NAME[p.element_type]}
                        </span>
                      </td>
                      <td>{p.web_name}</td>
                      <td>{p.team_name}</td>
                      <td>{cost(p.now_cost)}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="number"
                          min={0}
                          step={1}
                          style={{ width: 56 }}
                          value={p.bought_cost ?? p.now_cost}
                          onChange={(e) =>
                            setPaid(
                              p.player_id,
                              e.target.value === "" ? null : Math.max(0, Math.round(Number(e.target.value)))
                            )
                          }
                          title="Price you paid, same units as Cost (45 = £4.5m). Drives the sell-on fee in diffs."
                        />
                      </td>
                      <td>{p.ep_next != null ? p.ep_next.toFixed(1) : "—"}</td>
                      <td>{p.selected_by_percent != null ? p.selected_by_percent.toFixed(1) : "—"}</td>
                      <td className="small muted">
                        {p.role === "bench" ? `bench ${p.bench_order ?? "?"}` : "XI"}
                        {p.is_captain ? " C" : ""}
                        {p.is_vice_captain ? " VC" : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {selected && (
              <div className="row" style={{ marginTop: 10 }}>
                <span className="small muted">
                  {selected.web_name}
                  {selected.role === "bench" ? " (bench)" : " (XI)"}
                </span>
                <button className="sm" onClick={setCaptain} disabled={selected.role !== "starter"} title={selected.role !== "starter" ? "Captain must be a starter" : ""}>
                  Set captain
                </button>
                <button className="sm" onClick={setVice} disabled={selected.role !== "starter"} title={selected.role !== "starter" ? "Vice-captain must be a starter" : ""}>
                  Set VC
                </button>
                {selected.role === "starter" && (
                  <button
                    className="sm"
                    onClick={() => moveToBench(selected.player_id)}
                    disabled={selected.is_captain || selected.is_vice_captain}
                    title={
                      selected.is_captain || selected.is_vice_captain
                        ? "Captain/VC must stay in the XI — change captaincy first"
                        : benchCount >= 4
                          ? `Bench is full — will swap with ${benchSwapVictim?.web_name ?? "the lowest-EP bench player"}`
                          : ""
                    }
                  >
                    Send to bench
                  </button>
                )}
                {selected.role === "bench" && (
                  <button
                    className="sm"
                    onClick={() => moveToXi(selected.player_id)}
                    title={
                      selected.element_type !== 1 && xiCount >= 11
                        ? `XI is full — will swap with ${xiSwapVictim?.web_name ?? "the lowest-EP starter"}`
                        : ""
                    }
                  >
                    Move to XI
                  </button>
                )}
                {selected.role === "bench" &&
                  [1, 2, 3, 4].map((o) => (
                    <button key={o} className="sm ghost" onClick={() => setBenchOrder(o)} disabled={benchCount < o}>
                      Bench {o}
                    </button>
                  ))}
                <button className="sm danger" onClick={removePlayer}>
                  Remove
                </button>
              </div>
            )}
          </div>
          <PitchView
            players={draft.players}
            selectedId={selectedId}
            onSelect={selectPlayer}
            onMoveToBench={moveToBench}
            onMoveToXi={moveToXi}
            onBenchReorder={reorderBench}
          />
        </div>
      </div>
      {season && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Chips used — GW {season.current_gw}</h2>
          <div className="row" style={{ flexWrap: "wrap", gap: 14 }}>
            {Object.keys(EMPTY_CHIPS).map((chip) => {
              const on = chipPlays.some((c) => c.chip === chip);
              return (
                <label
                  key={chip}
                  className="chip-toggle"
                  title={
                    chip === "freehit"
                      ? "Logging a Free Hit here stops the app from suggesting another one next GW (consecutive-GW ban)"
                      : "Record that you played this chip this GW"
                  }
                >
                  <input type="checkbox" checked={on} onChange={() => toggleChip(chip)} />
                  {CHIP_LABEL[chip]}
                </label>
              );
            })}
          </div>
          <p className="small muted" style={{ marginBottom: 0 }}>
            Keep this in sync with FPL so chip advice (Free-Hit ban, chip windows) stays accurate.
          </p>
        </div>
      )}
      <MetaPanel
        name={draft.name}
        setName={(s) => patch((d) => ({ ...d, name: s }))}
        bank={draft.bank}
        setBank={(n) => patch((d) => ({ ...d, bank: n }))}
        chips={draft.chips}
        setChips={(c) => patch((d) => ({ ...d, chips: c }))}
        chipWindows={season?.chip_windows ?? []}
        totalCost={totalCost}
        savedId={draft.id}
        saving={saving}
        onSave={save}
        onSetCurrent={() => draft.id && api.setCurrent(draft.id).then(refreshLineups)}
        onGenerate={() => draft.id && nav("/suggestions", { state: { lineup_id: draft.id } })}
        onDelete={deleteLineup}
      />
      {saveError && <div className="err">{saveError}</div>}
    </div>
  );
}