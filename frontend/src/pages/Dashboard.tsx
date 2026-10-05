import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { latestRun, planNet } from "../components/PlanCard";
import { pairTransfers } from "../components/PlanDetail";
import { useSeason } from "../hooks/useSeason";
import { signalMark, signalOrigin, signalSourceLabel, shortDate, sortSignals } from "../signals";
import type {
  EntryData, Lineup, LineupPlayer, Signal, SourceHealth, SourcesResponse, Suggestion, TeamFixturesResponse,
} from "../types";
import { CHIP_LABEL, POS_NAME, fixtureFor, money, signed } from "../types";

const SOURCE_NAME: Record<string, string> = {
  "fpl-official": "FPL official", bbc: "BBC Sport", espn: "ESPN", reddit: "Reddit", youtube: "YouTube",
};

function flagOf(p: LineupPlayer): string | null {
  if (p.status === "u" || p.status === "s" || p.can_select === 0) return "out";
  const c = p.chance_of_playing_next_round;
  if (p.status === "d" || p.status === "i" || (c != null && c < 100)) return c != null ? `${c}%` : "doubt";
  return null;
}

function sourceTile(s: SourceHealth): { state: string; cls: string; line1: string; line2: string } {
  const name = s.source;
  if (!s.enabled) return { state: "Off", cls: "skip", line1: "Turned off", line2: "Settings → Sources" };
  const err = s.last_poll?.status === "error";
  const line1 = name === "fpl-official"
    ? `Refreshed ${shortDate(s.last_poll?.finished_at ?? null) || "—"}`
    : `${s.items} item${s.items === 1 ? "" : "s"}`;
  if (err) return { state: "Error", cls: "out", line1, line2: (s.last_poll?.error ?? "").slice(0, 60) };
  if (s.items > 0 && s.live_signals === 0 && name !== "fpl-official")
    return { state: "Check", cls: "warn", line1, line2: "0 live signals" };
  if (s.skipped_by_llm_breaker > 0)
    return { state: "Check", cls: "warn", line1, line2: `${s.skipped_by_llm_breaker} never read by the LLM` };
  return { state: "OK", cls: "ok", line1, line2: `${s.live_signals} live signal${s.live_signals === 1 ? "" : "s"}` };
}

export default function Dashboard() {
  const { season, error } = useSeason();
  const [lineup, setLineup] = useState<Lineup | null | undefined>(undefined);
  const [plans, setPlans] = useState<Suggestion[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [sources, setSources] = useState<SourcesResponse | null>(null);
  const [fixtures, setFixtures] = useState<TeamFixturesResponse | null>(null);
  const [entry, setEntry] = useState<EntryData | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const loadPlans = useCallback((lid: number) => {
    api.listSuggestions(lid).then((r) => {
      setPlans(latestRun(r.suggestions));
    }).catch(() => setPlans([]));
  }, []);

  const loadSignals = () => api.getSignals({ active: true }).then((r) => setSignals(r.signals)).catch(() => {});

  useEffect(() => {
    api.listLineups().then((r) => {
      const cur = r.lineups.find((l) => l.is_current) ?? r.lineups.find((l) => l.kind !== "test") ?? r.lineups[0];
      if (!cur) return setLineup(null);
      api.getLineup(cur.id).then(setLineup).catch(() => setLineup(null));
      loadPlans(cur.id);
    }).catch(() => setLineup(null));
    loadSignals();
    api.getSources().then(setSources).catch(() => {});
    api.getTeamFixtures(1).then(setFixtures).catch(() => {});
    api.getSettings().then((s) => {
      if ((s.group?.fpl_entry_id ?? "").trim()) api.getEntry().then(setEntry).catch(() => {});
    }).catch(() => {});
  }, [loadPlans]);

  const makePlans = async () => {
    if (!lineup) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.generateSuggestions({ lineup_id: lineup.id });
      setPlans(r.suggestions);
    } catch (e) {
      setMsg(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const refreshAll = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await api.refresh("all");
      loadSignals();
      api.getSources().then(setSources).catch(() => {});
      setMsg("All sources refreshed.");
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  };

  const squadIds = useMemo(() => new Set((lineup?.players ?? []).map((p) => p.player_id)), [lineup]);
  const flagged = (lineup?.players ?? []).filter((p) => flagOf(p));
  const squadNews = sortSignals(signals.filter((s) => squadIds.has(s.player_id))).slice(0, 6);
  const gw = season?.next_gw;
  const freshPlans = plans.filter((p) => p.target_gw === gw);
  const best = [...freshPlans].filter((s) => !s.variant_of).sort((a, b) => planNet(b) - planNet(a))[0];
  const pairs = best && lineup ? pairTransfers(best, lineup.players) : [];
  const capP = best?.lineup.squad.find((p) => p.player_id === best.lineup.captain);
  const vcP = best?.lineup.squad.find((p) => p.player_id === best.lineup.vice_captain);
  const capFx = capP ? fixtureFor(fixtures, capP.team, gw) : null;
  const chipUse = best?.chip_advice.filter((c) => c.recommendation !== "skip") ?? [];
  const mineByTeam = useMemo(() => {
    const m = new Map<number, number>();
    (lineup?.players ?? []).forEach((p) => m.set(p.team, (m.get(p.team) ?? 0) + 1));
    return m;
  }, [lineup]);
  const deadline = season?.deadline ? new Date(season.deadline) : null;
  const chipsLeft = lineup ? Object.entries(lineup.chips).filter(([, n]) => n > 0).map(([c]) => CHIP_LABEL[c] ?? c) : [];

  return (
    <div className="page">
      {error && <div className="alert bad">The app's server is not answering: {error}</div>}
      <section className="page-head">
        <div>
          <p className="eyebrow">Season {season?.season ?? "—"} · up next</p>
          <h1 className="hero">Gameweek {gw ?? "—"}</h1>
          <p className="lede">
            {deadline
              ? <>Deadline {deadline.toLocaleString(undefined, { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })}</>
              : "No deadline yet"}
            {season?.fixtures_next_gw?.length ? ` · ${season.fixtures_next_gw.length} matches` : ""}
          </p>
        </div>
        {lineup && (
          <div className="facts">
            <div><div className="fact-label">Team</div><div className="fact-value"><Link to="/team">{lineup.name}</Link></div></div>
            <div><div className="fact-label">Free transfers</div><div className="fact-value big">{lineup.transfer_bank}</div></div>
            <div>
              <div className="fact-label">In the bank</div>
              <div className="fact-value">{lineup.money_known ? money(lineup.bank_money) : <Link to="/team">Add amount</Link>}</div>
            </div>
            <div><div className="fact-label">Chips left</div><div className="fact-value">{chipsLeft.length ? chipsLeft.join(" · ") : "None"}</div></div>
          </div>
        )}
      </section>

      {lineup === null && (
        <div className="card">
          <h2>Add your team to get started</h2>
          <p className="help">Enter your squad on the <Link to="/team">My team</Link> page: paste a list of names or pick players. Then come back here for this week's checklist.</p>
        </div>
      )}

      {lineup && (
        <div className="split">
          <section className="card flush main" aria-labelledby="todo-h">
            <div className="card-head">
              <h2 id="todo-h" style={{ fontSize: 32 }}>Before the deadline</h2>
              <span className="small muted">4 checks{best ? ` · plans made ${shortDate(best.generated_at)}` : ""}</span>
            </div>
            <ol className="steps">
              <li>
                <span className={`step-num ${flagged.length ? "warn" : ""}`}>1</span>
                <div className="step-body">
                  <h3>{flagged.length ? `${flagged.length} of your players ${flagged.length === 1 ? "has" : "have"} an injury flag` : "No injury flags in your squad"}</h3>
                  {flagged.length > 0 && (
                    <div className="row">
                      {flagged.map((p) => (
                        <div key={p.player_id} className="flag-card">
                          <span className="flag-pct">{flagOf(p)}</span>
                          <span style={{ display: "flex", flexDirection: "column" }}>
                            <b>{p.web_name}</b>
                            <span className="small muted">
                              {p.team_short ?? ""} · {POS_NAME[p.element_type]} · {p.role === "starter" ? "starter" : "bench"}
                              {p.news ? ` · ${p.news}` : ""}
                            </span>
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </li>

              <li className="hl">
                <span className="step-num go">2</span>
                <div className="step-body">
                  {!best ? (
                    <>
                      <h3>Make this week's transfer plans</h3>
                      <p>Three plans (best projected, differential, safe), each within your free transfers.</p>
                      <button type="button" onClick={makePlans} disabled={busy}>
                        {busy ? <><span className="spinner" /> Working…</> : "Make plans"}
                      </button>
                    </>
                  ) : (
                    <>
                      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
                        <h3>
                          {pairs.length === 0 ? "Keep your squad: no transfer beats it"
                            : best.diff.penalty_points > 0 ? `${pairs.length} transfers, −${best.diff.penalty_points} point hit`
                              : `Use ${pairs.length} free transfer${pairs.length === 1 ? "" : "s"}. No points hit.`}
                        </h3>
                        <span className="badge dark">Best plan</span>
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 14 }}>
                        {pairs.map((p, i) => {
                          const gain = p.out?.ep != null && p.inn?.ep != null ? p.inn.ep - p.out.ep : null;
                          return (
                            <div className="move" key={i}>
                              <span className="who"><span className="tag out">OUT</span><b>{p.out?.name ?? "—"}</b>
                                {p.out && <span className="meta">{money(p.out.sell)}{p.out.ep != null ? ` · ${p.out.ep.toFixed(1)} pts` : ""}</span>}</span>
                              <svg width="20" height="20" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M4 11h14M13 6l5 5-5 5" /></svg>
                              <span className="who"><span className="tag in">IN</span><b>{p.inn?.name ?? "—"}</b>
                                {p.inn && <span className="meta">{p.inn.team ? `${p.inn.team} · ` : ""}{money(p.inn.cost)}{p.inn.ep != null ? ` · ${p.inn.ep.toFixed(1)} pts` : ""}</span>}</span>
                              <span className={`gain ${gain != null && gain < 0 ? "neg" : ""}`}>{gain != null ? signed(gain) : ""}</span>
                            </div>
                          );
                        })}
                      </div>
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <p style={{ margin: 0 }}>
                          <b style={{ color: "var(--ink)" }}>{planNet(best).toFixed(1)} projected points</b>
                          {best.projected_points.current_team != null &&
                            <>, {signed(planNet(best) - best.projected_points.current_team)} vs keeping your team ({best.projected_points.current_team.toFixed(1)})</>}
                        </p>
                        <span className="row">
                          <button type="button" className="ghost" onClick={makePlans} disabled={busy}>
                            {busy ? <><span className="spinner" /> Working…</> : "Recalculate"}
                          </button>
                          <Link className="btn" to="/suggestions">Compare all 3 plans</Link>
                        </span>
                      </div>
                    </>
                  )}
                </div>
              </li>

              <li>
                <span className="step-num">3</span>
                <div className="step-body">
                  {capP ? (
                    <>
                      <h3>Captain {capP.web_name}</h3>
                      <div className="row small muted">
                        {capFx?.label && <span className={`fdr ${capFx.cls}`}>{capFx.label}</span>}
                        <span>{(capP.ep ?? capP.ep_next ?? 0).toFixed(1)} projected, counted twice</span>
                        {vcP && <span>Vice-captain: {vcP.web_name}</span>}
                      </div>
                    </>
                  ) : (
                    <><h3>Captain</h3><p>Make plans to get a captain pick.</p></>
                  )}
                </div>
              </li>

              <li>
                <span className="step-num">4</span>
                <div className="step-body">
                  <h3>
                    {!best ? "Chips"
                      : chipUse.find((c) => c.recommendation === "use")
                        ? `Chips: play ${CHIP_LABEL[chipUse.find((c) => c.recommendation === "use")!.chip]}`
                        : chipUse.length ? `Chips: ${chipUse.map((c) => CHIP_LABEL[c.chip]).join(" or ")} worth a look` : "Chips: hold them"}
                  </h3>
                  <p style={{ marginBottom: 0 }}>
                    {best ? (chipUse.length ? chipUse.map((c) => `${CHIP_LABEL[c.chip]}: ${c.reason}`).join(" · ") : "No chip clears its bar this week.")
                      : "Chip advice comes with the plans."} You can play one chip per gameweek.
                  </p>
                </div>
              </li>
            </ol>
            {msg && <div className="alert info" style={{ margin: "0 20px 20px" }}>{msg}</div>}
          </section>

          <div className="side">
            <section className="card flush" aria-labelledby="news-h">
              <div className="card-head">
                <h2 id="news-h">News on your squad</h2>
                <Link to="/news" className="small">All news</Link>
              </div>
              {squadNews.length === 0 ? (
                <p className="help" style={{ padding: "14px 20px" }}>No live news about your players.</p>
              ) : (
                <ul className="list">
                  {squadNews.map((s) => {
                    const mk = signalMark(s);
                    const keyword = signalOrigin(s) === "keyword";
                    return (
                      <li key={s.id}>
                        <div className="list-row">
                          <span className={`mark ${mk.cls}`} aria-hidden="true">{mk.text}</span>
                          <b>{s.web_name}</b>
                          <span className="small muted">{s.team_code}</span>
                          <span className="small muted" style={{ marginLeft: "auto" }}>{shortDate(s.published_at ?? s.retrieved_at)}</span>
                        </div>
                        <p style={{ margin: "6px 0 4px 32px" }}>{s.summary}</p>
                        <div className="list-row small muted" style={{ marginLeft: 32 }}>
                          <span className="badge outline">{signalSourceLabel(s)}</span>
                          {keyword && (
                            <button type="button" className="sm ghost" onClick={() =>
                              api.dismissSignal(s.id).then(loadSignals).catch(() => {})}>
                              Not about this player
                            </button>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            <section className="card flush" aria-labelledby="fx-h">
              <div className="card-head">
                <h2 id="fx-h">GW{gw ?? ""} fixtures</h2>
                <span className="small muted">your local time</span>
              </div>
              <ul className="list" style={{ padding: "6px 0" }}>
                {(season?.fixtures_next_gw ?? []).map((f) => {
                  const mine = (mineByTeam.get(f.home_team) ?? 0) + (mineByTeam.get(f.away_team) ?? 0);
                  const h = fixtureFor(fixtures, f.home_team, gw);
                  const a = fixtureFor(fixtures, f.away_team, gw);
                  return (
                    <li key={`${f.home_team}-${f.away_team}`} className="fx-row" style={{ border: 0 }}>
                      <span className="small muted">{new Date(f.kickoff_time).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" })}</span>
                      <span className="row" style={{ gap: 6 }}>
                        <span className={`fdr ${h.cls}`}>{fixtures?.teams[String(f.home_team)]?.short ?? f.home_name}</span>
                        <span className="small muted">v</span>
                        <span className={`fdr ${a.cls}`}>{fixtures?.teams[String(f.away_team)]?.short ?? f.away_name}</span>
                      </span>
                      {mine > 0 ? <span className="yours">{mine} yours</span> : <span />}
                    </li>
                  );
                })}
              </ul>
              <div className="row small muted" style={{ padding: "10px 20px", borderTop: "1px solid var(--line-soft)", gap: 6 }}>
                Difficulty
                {[1, 2, 3, 4, 5].map((d) => <span key={d} className={`fdr d${d}`} style={{ minWidth: 24 }}>{d}</span>)}
                easy to hard
              </div>
            </section>
          </div>
        </div>
      )}

      <section className="card" aria-labelledby="src-h">
        <div className="page-head" style={{ marginBottom: 14, alignItems: "baseline" }}>
          <h2 id="src-h" style={{ margin: 0 }}>Where the news comes from</h2>
          <div className="row">
            <button type="button" className="ghost sm" onClick={refreshAll} disabled={busy}>Refresh all</button>
            <Link to="/news" className="small">Source details</Link>
          </div>
        </div>
        <div className="tiles">
          {(sources?.sources ?? []).map((s) => {
            const t = sourceTile(s);
            return (
              <div className="tile" key={s.source}>
                <div className="tile-head"><b>{SOURCE_NAME[s.source] ?? s.source}</b><span className={`badge ${t.cls}`}>{t.state}</span></div>
                <div className="small muted" style={{ marginTop: 8 }}>{t.line1}</div>
                <div className="small muted">{t.line2}</div>
              </div>
            );
          })}
          {sources && (
            <div className="tile">
              <div className="tile-head">
                <b>Local LLM</b>
                <span className={`badge ${!sources.llm.ready ? "skip" : sources.llm.errors_7d > sources.llm.ok_7d ? "warn" : "ok"}`}>
                  {!sources.llm.ready ? "Off" : sources.llm.errors_7d > sources.llm.ok_7d ? "Check" : "OK"}
                </span>
              </div>
              <div className="small muted" style={{ marginTop: 8 }}>{sources.llm.model ?? "not configured"}</div>
              <div className="small muted">{sources.llm.ok_7d} ok · {sources.llm.errors_7d} failed (7 days)</div>
            </div>
          )}
        </div>
      </section>

      {entry && (
        <section className="card" aria-labelledby="rank-h">
          <h2 id="rank-h">Your rank</h2>
          <div className="row" style={{ gap: 32, alignItems: "flex-start" }}>
            <div><div className="fact-label">Overall points</div><div className="fact-value big">{entry.overall_points ?? "—"}</div></div>
            <div>
              <div className="fact-label">Overall rank</div>
              <div className="fact-value big">{entry.overall_rank ? `#${entry.overall_rank.toLocaleString()}` : "—"}</div>
              {entry.overall_percentile != null && <div className="small muted">top {entry.overall_percentile}%</div>}
            </div>
          </div>
          {entry.leagues.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead><tr><th>League</th><th className="num">Rank</th><th className="num">Size</th><th className="num">Points</th></tr></thead>
                <tbody>
                  {entry.leagues.map((lg) => (
                    <tr key={lg.league_id ?? lg.name ?? "lg"}>
                      <td>{lg.name ?? `#${lg.league_id}`} {lg.league_type === "x" && <span className="badge skip">mini-league</span>}</td>
                      <td className="num">{lg.rank ? `#${lg.rank.toLocaleString()}` : "—"}</td>
                      <td className="num">{lg.size?.toLocaleString() ?? "—"}</td>
                      <td className="num">{lg.points ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
