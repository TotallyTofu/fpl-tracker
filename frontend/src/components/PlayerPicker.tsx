import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { LineupPlayer, Player } from "../types";
import { POS_NAME, cost } from "../types";

interface Props {
  squad: LineupPlayer[];
  selectedId: number | null;
  onAdd: (p: Player) => void;
  onSwap: (p: Player) => void;
}

const POS_TABS = [0, 1, 2, 3, 4];

export default function PlayerPicker({ squad, selectedId, onAdd, onSwap }: Props) {
  const [search, setSearch] = useState("");
  const [pos, setPos] = useState(0);
  const [rows, setRows] = useState<Player[]>([]);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      setBusy(true);
      api
        .getPlayers({ search: search || undefined, pos: pos || undefined, limit: 60 })
        .then((r) => setRows(r.players))
        .catch(() => setRows([]))
        .finally(() => setBusy(false));
    }, 300);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [search, pos]);

  const squadIds = new Set(squad.map((p) => p.player_id));
  const sel = squad.find((p) => p.player_id === selectedId);

  const roomFor = (pos: number) => {
    const need = { 1: 2, 2: 5, 3: 5, 4: 3 }[pos] ?? 0;
    const have = squad.filter((p) => p.element_type === pos).length;
    return have < need && squad.length < 15;
  };

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <label htmlFor="picker-search" className="sr-only">Search players</label>
        <input
          id="picker-search"
          type="search"
          style={{ flex: 1, minWidth: 180 }}
          placeholder="Search players…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="seg" role="group" aria-label="Position">
          {POS_TABS.map((p) => (
            <button key={p} type="button" className="sm" aria-pressed={pos === p} onClick={() => setPos(p)}>
              {p === 0 ? "All" : POS_NAME[p]}
            </button>
          ))}
        </div>
      </div>
      {sel && (
        <div className="small muted" style={{ marginBottom: 6 }}>
          Swapping <b>{sel.web_name}</b>: click a {POS_NAME[sel.element_type]} below
        </div>
      )}
      <div style={{ maxHeight: 320, overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Pos</th>
              <th>Club</th>
              <th className="num">Price</th>
              <th className="num">FPL xP</th>
              <th className="num">Owned</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {busy && (
              <tr>
                <td colSpan={7} className="muted">
                  <span className="spinner" /> Loading…
                </td>
              </tr>
            )}
            {!busy &&
              rows.map((p) => {
                const inSquad = squadIds.has(p.id);
                const canSwap = !!sel && sel.element_type === p.element_type && !inSquad;
                const canAdd = !inSquad && roomFor(p.element_type);
                return (
                  <tr
                    key={p.id}
                    className="clickable"
                    style={{ opacity: inSquad ? 0.45 : 1, cursor: canAdd || canSwap ? "pointer" : "not-allowed" }}
                    onClick={() => (canSwap ? onSwap(p) : canAdd ? onAdd(p) : undefined)}
                    title={inSquad ? "Already in squad" : canSwap ? "Swap with selected" : canAdd ? "Add to squad" : "No room for this position"}
                  >
                    <td>{p.web_name}</td>
                    <td>
                      <span className="badge">{POS_NAME[p.element_type]}</span>
                    </td>
                    <td>{p.team_name}</td>
                    <td className="num">{cost(p.now_cost)}</td>
                    <td className="num">{p.ep_next != null ? p.ep_next.toFixed(1) : "—"}</td>
                    <td className="num">{p.selected_by_percent != null ? `${p.selected_by_percent.toFixed(1)}%` : "—"}</td>
                    <td>
                      {p.status === "u" || p.status === "s" || p.can_select === 0 ? (
                        <span className="badge out">Out</span>
                      ) : p.status === "d" || p.chance_of_playing_next_round === 50 ? (
                        <span className="badge warn">Doubt</span>
                      ) : (
                        <span className="badge ok">Fit</span>
                      )}
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>
    </div>
  );
}