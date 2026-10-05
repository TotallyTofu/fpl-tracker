import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import Countdown from "../components/Countdown";
import TeamStrip from "../components/TeamStrip";
import { useSeason } from "../hooks/useSeason";
import { fmtTime, type EntryData, type Signal } from "../types";

export default function Dashboard() {
  const { season, error } = useSeason();
  const nav = useNavigate();
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const [signals, setSignals] = useState<Signal[] | null>(null);
  const [entryCfg, setEntryCfg] = useState(false);
  const [entry, setEntry] = useState<EntryData | null>(null);
  const [entryErr, setEntryErr] = useState<string | null>(null);

  const loadSignals = () =>
    api
      .getSignals({ active: true })
      .then((r) => setSignals(r.signals))
      .catch(() => setSignals([]));

  useEffect(() => {
    loadSignals();
    // Group-rank card (T4.4): only fetch when the user set an entry ID.
    api
      .getSettings()
      .then((s) => {
        if ((s.group?.fpl_entry_id ?? "").trim()) {
          setEntryCfg(true);
          api.getEntry().then(setEntry).catch((e) => setEntryErr(e.message));
        }
      })
      .catch(() => {});
  }, []);

  const refreshAll = async () => {
    setRefreshing(true);
    setRefreshMsg(null);
    try {
      const r = await api.refresh("all");
      setRefreshMsg(
        Object.entries(r.results)
          .map(([k, v]) => (typeof v === "string" ? v : "ok"))
          .join(" · ")
      );
      loadSignals();
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
          {signals === null ? (
            <div className="muted small">loading…</div>
          ) : signals.length === 0 ? (
            <div className="muted small">No active signals right now.</div>
          ) : (
            <>
              <div className="small muted" style={{ marginBottom: 6 }}>
                {signals.length} active
              </div>
              <ul className="sig-list">
                {signals.slice(0, 5).map((s) => (
                  <li key={s.id}>
                    <span
                      className="dot"
                      style={{ background: s.sentiment === "negative" ? "var(--red)" : "var(--accent)" }}
                    />
                    <span className="sig-player">{s.web_name ?? `#${s.player_id}`}</span>
                    <span className="badge skip">{s.category}</span>
                    <span className="muted small">{Math.round(s.confidence * 100)}%</span>
                  </li>
                ))}
              </ul>
              {signals.length > 5 && (
                <button className="ghost small" onClick={() => nav("/news")} style={{ marginTop: 6 }}>
                  See all {signals.length} →
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {entryCfg && (
        <div className="panel" style={{ marginBottom: 14 }}>
          <h2 style={{ marginTop: 0 }}>My group position</h2>
          {entryErr ? (
            <div className="err small">{entryErr}</div>
          ) : entry === null ? (
            <div className="muted small">loading…</div>
          ) : (
            <>
              <div className="row" style={{ alignItems: "flex-start", gap: 28 }}>
                <div>
                  <div className="big-num">{entry.overall_points ?? "—"}</div>
                  <div className="muted small">overall points</div>
                </div>
                <div>
                  <div className="big-num">
                    {entry.overall_rank ? `#${entry.overall_rank.toLocaleString()}` : "—"}
                  </div>
                  <div className="muted small">
                    {entry.overall_percentile != null && entry.overall_rank_out_of
                      ? `top ${entry.overall_percentile}% of ${entry.overall_rank_out_of.toLocaleString()} entries`
                      : "rank not published yet"}
                  </div>
                </div>
                {entry.name && (
                  <div className="muted small" style={{ marginTop: 8 }}>
                    {entry.name}
                  </div>
                )}
              </div>
              {entry.leagues.length > 0 && (
                <div style={{ maxHeight: 220, overflowY: "auto", marginTop: 10 }}>
                  <table>
                    <thead>
                      <tr>
                        <th>League</th>
                        <th>Rank</th>
                        <th>Size</th>
                        <th>Points</th>
                      </tr>
                    </thead>
                    <tbody>
                      {entry.leagues.map((lg) => (
                        <tr key={lg.league_id ?? lg.name ?? "league"}>
                          <td>
                            {lg.name ?? `#${lg.league_id}`}
                            {lg.league_type === "x" && (
                              <span className="badge skip" style={{ marginLeft: 8 }}>
                                mini-league
                              </span>
                            )}
                          </td>
                          <td>{lg.rank ? `#${lg.rank.toLocaleString()}` : "—"}</td>
                          <td className="muted">{lg.size ? lg.size.toLocaleString() : "—"}</td>
                          <td>{lg.points ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="small muted" style={{ marginTop: 8 }}>
                updates as gameweeks finalize (official ranks publish after each GW)
              </div>
            </>
          )}
        </div>
      )}

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