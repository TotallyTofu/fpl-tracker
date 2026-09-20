import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import SuggestionCard from "../components/SuggestionCard";
import { useSeason } from "../hooks/useSeason";
import type { LineupSummary, Suggestion } from "../types";
import { fmtTime } from "../types";

const ORDER: Record<string, number> = { max_ep: 0, differential: 1, safe: 2 };

export default function Suggestions() {
  const { season } = useSeason();
  const loc = useLocation();
  const preselect = (loc.state as { lineup_id?: number } | null)?.lineup_id;

  const [lineups, setLineups] = useState<LineupSummary[]>([]);
  const [lineupId, setLineupId] = useState<number | "">("");
  const [targetGw, setTargetGw] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cards, setCards] = useState<Suggestion[]>([]);
  const [history, setHistory] = useState<Suggestion[]>([]);

  useEffect(() => {
    api
      .listLineups()
      .then((r) => {
        setLineups(r.lineups);
        const def = preselect ?? r.lineups.find((l) => l.is_current)?.id ?? r.lineups[0]?.id ?? "";
        setLineupId(def);
      })
      .catch((e) => setError(String(e)));
  }, [preselect]);

  useEffect(() => {
    if (season?.next_gw && targetGw === "") setTargetGw(season.next_gw);
  }, [season, targetGw]);

  const loadHistory = (lid: number) => {
    api.listSuggestions(lid).then((r) => setHistory(r.suggestions)).catch(() => {});
  };
  useEffect(() => {
    if (lineupId !== "") loadHistory(lineupId);
  }, [lineupId]);

  const generate = async () => {
    if (lineupId === "") {
      setError("Save a lineup first (My Team page)");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.generateSuggestions({
        lineup_id: lineupId,
        target_gw: targetGw === "" ? undefined : targetGw,
      });
      setCards(
        [...r.suggestions].sort((a, b) => (ORDER[a.profile] ?? 9) - (ORDER[b.profile] ?? 9))
      );
      loadHistory(lineupId);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const gwOptions: number[] = [];
  if (season?.current_gw) gwOptions.push(season.current_gw);
  if (season?.next_gw) gwOptions.push(season.next_gw);
  if (season?.next_gw && season.events_total)
    gwOptions.push(Math.min(season.next_gw + 1, season.events_total));
  const uniqGws = [...new Set(gwOptions)].sort((a, b) => a - b);

  return (
    <div>
      <h1>Suggestions</h1>
      <div className="panel">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div>
            <label>Lineup</label>
            <select value={lineupId} onChange={(e) => setLineupId(e.target.value ? Number(e.target.value) : "")}>
              {lineups.length === 0 && <option value="">no saved lineups</option>}
              {lineups.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                  {l.is_current ? " ★" : ""}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label>Target GW</label>
            <select value={targetGw} onChange={(e) => setTargetGw(e.target.value ? Number(e.target.value) : "")}>
              {uniqGws.map((g) => (
                <option key={g} value={g}>
                  GW {g}
                  {g === season?.next_gw ? " (next)" : ""}
                </option>
              ))}
            </select>
          </div>
          <button onClick={generate} disabled={busy || lineupId === ""}>
            {busy ? (
              <>
                <span className="spinner" /> optimizing… (~8 s × 3 profiles)
              </>
            ) : (
              "Generate"
            )}
          </button>
        </div>
        {error && <div className="err" style={{ marginTop: 8 }}>{error}</div>}
      </div>

      {cards.map((s) => (
        <SuggestionCard key={s.id} s={s} />
      ))}

      {history.length > 0 && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>History</h2>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Profile</th>
                <th>Adjusted</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id}>
                  <td className="muted small">{fmtTime(h.generated_at)}</td>
                  <td>{h.profile}</td>
                  <td>{h.projected_points.adjusted.toFixed(1)}</td>
                  <td>
                    <button
                      className="sm ghost"
                      onClick={() =>
                        setCards(
                          cards.some((c) => c.id === h.id)
                            ? cards
                            : [...cards, h].sort((a, b) => (ORDER[a.profile] ?? 9) - (ORDER[b.profile] ?? 9))
                        )
                      }
                    >
                      view
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}