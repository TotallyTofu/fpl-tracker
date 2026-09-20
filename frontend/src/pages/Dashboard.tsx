import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import Countdown from "../components/Countdown";
import TeamStrip from "../components/TeamStrip";
import { useSeason } from "../hooks/useSeason";
import { fmtTime } from "../types";

export default function Dashboard() {
  const { season, error } = useSeason();
  const nav = useNavigate();
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);

  const refreshAll = async () => {
    setRefreshing(true);
    setRefreshMsg(null);
    try {
      const r = await api.refresh("all");
      setRefreshMsg(
        Object.entries(r.results)
          .map(([k, v]) => `${k}: ${v}`)
          .join(" · ")
      );
    } catch (e) {
      setRefreshMsg(String(e));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div>
      <h1>Dashboard</h1>
      {error && <div className="err">Backend not reachable: {error}</div>}
      <div className="grid3">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Season</h2>
          <div style={{ marginBottom: 8 }}>
            <div className="big-num">{season?.season ?? "—"}</div>
            <div className="muted">
              GW {season?.current_gw ?? "—"} / {season?.events_total ?? "?"}
              {season?.next_gw ? ` · next: GW ${season.next_gw}` : ""}
            </div>
          </div>
          <div className="row" style={{ alignItems: "center" }}>
            <span className="muted small">Deadline in</span>
            <Countdown deadline={season?.deadline ?? null} />
            <span className="live-badge">{season?.live_mode ? "LIVE" : "not live"}</span>
          </div>
          {season?.deadline && (
            <div className="small muted" style={{ marginTop: 6 }}>
              {fmtTime(season.deadline)} (local)
            </div>
          )}
        </div>
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Quick actions</h2>
          <div className="row">
            <button onClick={() => nav("/suggestions")}>Get suggestions →</button>
            <button className="ghost" onClick={refreshAll} disabled={refreshing}>
              {refreshing ? "Refreshing…" : "Refresh all sources"}
            </button>
          </div>
          {refreshMsg && <div className="small muted" style={{ marginTop: 8 }}>{refreshMsg}</div>}
        </div>
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Signals</h2>
          <div className="muted small">
            No signals yet — the news pipeline (BBC / Reddit / YouTube / LLM) lands in M2.
          </div>
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>My team</h2>
          <TeamStrip />
        </div>
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>
            GW {season?.next_gw ?? "—"} fixtures
          </h2>
          <div style={{ maxHeight: 320, overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Kickoff</th>
                  <th>Home</th>
                  <th>Away</th>
                </tr>
              </thead>
              <tbody>
                {(season?.fixtures_next_gw ?? []).map((f) => (
                  <tr key={`${f.home_team}-${f.away_team}`}>
                    <td className="muted small">{fmtTime(f.kickoff_time)}</td>
                    <td>{f.home_name}</td>
                    <td>{f.away_name}</td>
                  </tr>
                ))}
                {(season?.fixtures_next_gw ?? []).length === 0 && (
                  <tr>
                    <td colSpan={3} className="muted small">
                      no fixtures yet — refresh FPL data
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}