import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { signalEffect, signalMark, signalOrigin, signalSourceLabel, shortDate, sortSignals } from "../signals";
import type { RawItem, Signal, SourceHealth, SourcesResponse } from "../types";
import { fmtTime } from "../types";

const SOURCE_NAME: Record<string, { name: string; sub: string; refresh: string }> = {
  "fpl-official": { name: "FPL official", sub: "Prices, fixtures, injury flags", refresh: "fpl" },
  bbc: { name: "BBC Sport", sub: "Football RSS plus full articles", refresh: "bbc" },
  espn: { name: "ESPN", sub: "Premier League news, full stories", refresh: "espn" },
  reddit: { name: "Reddit r/FantasyPL", sub: "Posts and top comments", refresh: "reddit" },
  youtube: { name: "YouTube", sub: "Planet FPL videos and transcripts", refresh: "youtube" },
};
const CATEGORIES = ["injury", "suspension", "selection", "rotation", "return", "transfer", "other"] as const;

function status(s: SourceHealth): { text: string; cls: string; note: string } {
  if (!s.enabled) return { text: "Off", cls: "skip", note: "Turned off in Settings." };
  if (s.last_poll?.status === "error") return { text: "Error", cls: "out", note: s.last_poll.error ?? "Last fetch failed." };
  const notes: string[] = [];
  if (s.skipped_by_llm_breaker) notes.push(`${s.skipped_by_llm_breaker} item(s) were never read by the LLM.`);
  if (s.undated && s.source === "reddit") notes.push(`${s.undated} post(s) have no date.`);
  if (s.waiting) notes.push(`${s.waiting} waiting to be read.`);
  if (s.items > 0 && s.live_signals === 0 && s.source !== "fpl-official") notes.push("No live signals right now.");
  return { text: notes.length ? "Check" : "OK", cls: notes.length ? "warn" : "ok", note: notes.join(" ") || "Working." };
}

export default function News() {
  const [sources, setSources] = useState<SourcesResponse | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [items, setItems] = useState<RawItem[]>([]);
  const [squad, setSquad] = useState<Set<number>>(new Set());
  const [onlySquad, setOnlySquad] = useState(false);
  const [category, setCategory] = useState("");
  const [hideKeyword, setHideKeyword] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const loadSources = useCallback(() => api.getSources().then(setSources).catch(() => {}), []);
  const loadSignals = useCallback(() => api.getSignals({ active: true }).then((r) => setSignals(r.signals)).catch(() => {}), []);
  const loadItems = useCallback(() => api.getItems({ limit: 40 }).then((r) => setItems(r.items)).catch(() => {}), []);

  useEffect(() => {
    loadSources();
    loadSignals();
    loadItems();
    api.listLineups().then((r) => {
      const cur = r.lineups.find((l) => l.is_current) ?? r.lineups[0];
      if (cur) api.getLineup(cur.id).then((l) => setSquad(new Set(l.players.map((p) => p.player_id)))).catch(() => {});
    }).catch(() => {});
  }, [loadSources, loadSignals, loadItems]);

  const run = async (key: string, fn: () => Promise<string>) => {
    setBusy(key);
    setMsg(null);
    try {
      setMsg(await fn());
    } catch (e) {
      setMsg(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
      loadSources();
      loadSignals();
      loadItems();
    }
  };

  const refresh = (src: string) => run(src, async () => {
    const r = await api.refresh(src);
    return Object.entries(r.results).map(([k, v]) =>
      `${k}: ${typeof v === "string" ? v : v.status === "ok" ? `${v.rows ?? 0} new` : v.status}`).join(" · ");
  });
  const requeue = () => run("requeue", async () => {
    const r = await api.requeueSkipped(10);
    return r.requeued
      ? `${r.requeued} item(s) queued for the LLM (keyword signals from them removed: ${r.signals_removed}). They are read in the next pass, or press Refresh all.`
      : "Nothing to re-read.";
  });
  const dismiss = (id: number) => api.dismissSignal(id).then(loadSignals).catch(() => {});

  const shown = useMemo(() => sortSignals(signals).filter((s) =>
    (!onlySquad || squad.has(s.player_id))
    && (!category || s.category === category)
    && (!hideKeyword || signalOrigin(s) !== "keyword")), [signals, onlySquad, squad, category, hideKeyword]);
  const squadCount = signals.filter((s) => squad.has(s.player_id)).length;
  const skippedTotal = (sources?.sources ?? []).reduce((n, s) => n + s.skipped_by_llm_breaker, 0);
  const llm = sources?.llm;

  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>News &amp; signals</h1>
          <p className="lede">A signal is one piece of player news turned into a nudge up or down in projected points.</p>
        </div>
        <button type="button" className="lg" onClick={() => refresh("all")} disabled={busy !== null}>
          {busy === "all" ? <><span className="spinner" /> Refreshing…</> : "Refresh all sources"}
        </button>
      </section>

      {msg && <div className="alert info">{msg}</div>}

      <section className="card flush" aria-labelledby="src-h">
        <div className="card-head"><h2 id="src-h">Sources</h2></div>
        <div className="table-wrap">
          <table style={{ minWidth: 900 }}>
            <thead>
              <tr>
                <th style={{ paddingLeft: 20 }}>Source</th><th>Status</th><th>Last fetched</th>
                <th className="num">Items</th><th className="num">Live signals</th><th>Notes</th>
                <th style={{ paddingRight: 20 }}><span className="sr-only">Action</span></th>
              </tr>
            </thead>
            <tbody>
              {(sources?.sources ?? []).map((s) => {
                const st = status(s);
                const meta = SOURCE_NAME[s.source];
                return (
                  <tr key={s.source}>
                    <td style={{ paddingLeft: 20 }}><b>{meta?.name ?? s.source}</b><br /><span className="small muted">{meta?.sub}</span></td>
                    <td><span className={`badge ${st.cls}`}>{st.text}</span></td>
                    <td className="small muted">{fmtTime(s.last_poll?.finished_at ?? s.last_item)}</td>
                    <td className="num">{s.items}</td>
                    <td className="num">{s.live_signals}</td>
                    <td className="small" style={{ maxWidth: 360 }}>{st.note}</td>
                    <td style={{ paddingRight: 20, textAlign: "right" }}>
                      {s.enabled ? (
                        <button type="button" className="sm ghost" disabled={busy !== null} onClick={() => refresh(meta?.refresh ?? s.source)}>
                          {busy === (meta?.refresh ?? s.source) ? "…" : "Refresh"}
                        </button>
                      ) : (
                        <Link className="btn ghost" to="/settings" style={{ minHeight: 32, fontSize: 13 }}>Turn on</Link>
                      )}
                    </td>
                  </tr>
                );
              })}
              {llm && (
                <tr>
                  <td style={{ paddingLeft: 20 }}><b>Local LLM</b><br /><span className="small muted">{llm.model ?? "not configured"}</span></td>
                  <td>
                    <span className={`badge ${!llm.ready ? "skip" : llm.errors_7d > llm.ok_7d ? "warn" : "ok"}`}>
                      {!llm.ready ? "Off" : llm.errors_7d > llm.ok_7d ? "Check" : "OK"}
                    </span>
                  </td>
                  <td className="small muted">{fmtTime(llm.last?.finished_at ?? null)}</td>
                  <td className="num">{llm.ok_7d + llm.errors_7d}</td>
                  <td className="num">—</td>
                  <td className="small" style={{ maxWidth: 360 }}>
                    {!llm.ready ? "Keyword matching only. Add a model and key in Settings for better signals."
                      : `${llm.ok_7d} ok, ${llm.errors_7d} failed in 7 days. Thinking ${llm.disable_thinking ? "off" : "on (long articles can time out)"}.`}
                    {skippedTotal > 0 && ` ${skippedTotal} item(s) were skipped while it failed.`}
                  </td>
                  <td style={{ paddingRight: 20, textAlign: "right" }}>
                    {skippedTotal > 0 ? (
                      <button type="button" className="sm" disabled={busy !== null} onClick={requeue}>Re-read skipped</button>
                    ) : (
                      <Link className="btn ghost" to="/settings" style={{ minHeight: 32, fontSize: 13 }}>Settings</Link>
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div className="split">
        <section className="card flush main" aria-labelledby="sig-h">
          <div className="card-head" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h2 id="sig-h">Player signals</h2>
              <span className="small muted">{signals.length} live · showing {shown.length}</span>
            </div>
            <div className="row">
              <div className="seg" role="group" aria-label="Whose news">
                <button type="button" aria-pressed={onlySquad} onClick={() => setOnlySquad(true)}>Your squad ({squadCount})</button>
                <button type="button" aria-pressed={!onlySquad} onClick={() => setOnlySquad(false)}>All players</button>
              </div>
              <label className="sr-only" htmlFor="cat">Category</label>
              <select id="cat" value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">All categories</option>
                {CATEGORIES.map((c) => <option key={c} value={c}>{c[0].toUpperCase() + c.slice(1)}</option>)}
              </select>
              <label className="inline">
                <input type="checkbox" checked={hideKeyword} onChange={(e) => setHideKeyword(e.target.checked)} />
                Hide keyword-only matches
              </label>
            </div>
          </div>
          {shown.length === 0 ? (
            <p className="help" style={{ padding: 20 }}>No signals match. Refresh the sources to pull the latest news.</p>
          ) : (
            <ul className="list">
              {shown.map((s) => {
                const mk = signalMark(s);
                const keyword = signalOrigin(s) === "keyword";
                return (
                  <li key={s.id} style={{ display: "flex", gap: 14 }}>
                    <span className={`mark ${mk.cls}`} aria-label={s.sentiment}>{mk.text}</span>
                    <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
                      <div className="list-row">
                        <b style={{ fontSize: 16 }}>{s.web_name}</b>
                        <span className="small muted">{s.team_code}</span>
                        <span className="badge skip">{s.category}</span>
                        {squad.has(s.player_id) && <span className="badge dark">In your squad</span>}
                        <span className="small muted" style={{ marginLeft: "auto" }}>{shortDate(s.published_at ?? s.retrieved_at)}</span>
                      </div>
                      <p style={{ margin: 0 }}>
                        {s.summary}{" "}
                        {s.url && <a href={s.url} target="_blank" rel="noreferrer" className="small">source</a>}
                      </p>
                      <div className="list-row small muted" style={{ gap: "6px 14px" }}>
                        <span>{signalSourceLabel(s)}</span>
                        <span>confidence <span className="confbar"><span style={{ width: `${Math.round(s.confidence * 100)}%` }} /></span> {Math.round(s.confidence * 100)}%</span>
                        <span style={{ fontWeight: 600, color: "var(--ink)" }}>{signalEffect(s)}</span>
                      </div>
                      {keyword && s.confidence < 0.6 && (
                        <div className="note-box">
                          <span style={{ flex: "1 1 240px" }}>Keyword match: check it is about this player and is real news.</span>
                          <button type="button" className="sm ghost" onClick={() => dismiss(s.id)}>Dismiss</button>
                        </div>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="card flush side" aria-labelledby="items-h">
          <div className="card-head"><h2 id="items-h">Latest fetched</h2></div>
          <ul className="list">
            {items.filter((it) => it.source !== "fpl-official").slice(0, 25).map((it) => {
              const skipped = (it.takeaways ?? []).some((t) => t.includes("circuit breaker") || t.startsWith("Keyword match only"));
              const state = !it.processed ? ["Waiting", "skip"] : skipped ? ["Keywords only", "warn"] : ["Read", "ok"];
              return (
                <li key={it.id}>
                  <div className="list-row small muted">
                    <b style={{ color: "var(--ink)" }}>{SOURCE_NAME[it.source]?.name.split(" ")[0] ?? it.source}</b>
                    <span>{shortDate(it.published_at ?? it.retrieved_at)}</span>
                    <span className={`badge ${state[1]}`} style={{ marginLeft: "auto" }}>{state[0]}</span>
                  </div>
                  <div style={{ marginTop: 4, fontWeight: 600 }}>
                    {it.url ? <a href={it.url} target="_blank" rel="noreferrer" style={{ color: "var(--ink)" }}>{it.title ?? "(untitled)"}</a> : it.title}
                  </div>
                  {it.takeaways?.length > 0 && !skipped && (
                    <div className="small muted" style={{ marginTop: 4 }}>{it.takeaways.slice(0, 2).join(" · ")}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      </div>
    </div>
  );
}
