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
import type { Lineup, LineupPlayer, LineupSummary, MatchedPlayer, Player } from "../types";
import { POS_NAME, cost } from "../types";

interface Draft {
  id: number | null;
  name: string;
  bank: number;
  chips: Record<string, number>;
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
    players: l.players,
  };
}

function emptyDraft(): Draft {
  return { id: null, name: "My team", bank: 1, chips: { ...EMPTY_CHIPS }, players: [] };
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
        role: "starter",
        bench_order: null,
        is_captain: false,
        is_vice_captain: false,
        bought_cost: p.now_cost,
      };
      return { ...d, players: assignRoles([...d.players, np], d.players) };
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

  const selectPlayer = (p: LineupPlayer) => setSelectedId(p.player_id);

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
      const next = d.players.filter((p) => p.player_id !== selectedId);
      return { ...d, players: assignRoles(next, d.players) };
    });

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
      const body = {
        name: draft.name,
        transfer_bank: draft.bank,
        chips: draft.chips,
        players: draft.players.map((p) => ({
          player_id: p.player_id,
          role: p.role,
          bench_order: p.bench_order,
          is_captain: p.is_captain,
          is_vice_captain: p.is_vice_captain,
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
            <h2 style={{ marginTop: 0 }}>Squad ({draft.players.length}/15)</h2>
            <div style={{ maxHeight: 300, overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th onClick={() => clickSort("element_type")}>Pos</th>
                    <th onClick={() => clickSort("web_name")}>Name</th>
                    <th onClick={() => clickSort("team_name")}>Club</th>
                    <th onClick={() => clickSort("now_cost")}>Cost</th>
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
          <PitchView players={draft.players} selectedId={selectedId} onSelect={selectPlayer} />
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