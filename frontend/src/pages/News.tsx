import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fmtTime } from "../types";
import type { RawItem, Signal } from "../types";

const SOURCES = ["fpl", "bbc", "espn", "reddit", "youtube", "all"] as const;
const CATEGORIES = ["injury", "suspension", "selection", "return", "transfer", "other"] as const;
const KINDS = ["article", "thread", "video", "official-news"] as const;

function hoursLeft(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "expired";
  const h = Math.floor(ms / 3_600_000);
  return h >= 48 ? `${Math.floor(h / 24)}d` : `${h}h`;
}

function SentimentDot({ s }: { s: Signal }) {
  return (
    <span
      title={s.sentiment}
      style={{
        display: "inline-block",
        width: 9,
        height: 9,
        borderRadius: "50%",
        background: s.sentiment === "negative" ? "var(--red)" : "var(--accent)",
        marginRight: 8,
        flexShrink: 0,
      }}
    />
  );
}

export default function News() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [sigCat, setSigCat] = useState("");
  const [sigActive, setSigActive] = useState(true);
  const [items, setItems] = useState<RawItem[]>([]);
  const [itemSource, setItemSource] = useState("");
  const [itemKind, setItemKind] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [refreshResults, setRefreshResults] = useState<Record<string, string>>({});
  const [llmReady, setLlmReady] = useState<boolean | null>(null);
  const [fetchingBody, setFetchingBody] = useState<number | null>(null);
  const [bodyNote, setBodyNote] = useState<string | null>(null);

  const loadSignals = useCallback(() => {
    api
      .getSignals({ category: sigCat || undefined, active: sigActive })
      .then((d) => setSignals(d.signals))
      .catch(() => {});
  }, [sigCat, sigActive]);

  const loadItems = useCallback(() => {
    api
      .getItems({ source: itemSource || undefined, kind: itemKind || undefined, limit: 100 })
      .then((d) => setItems(d.items))
      .catch(() => {});
  }, [itemSource, itemKind]);

  useEffect(() => {
    loadSignals();
  }, [loadSignals]);
  useEffect(() => {
    loadItems();
  }, [loadItems]);
  useEffect(() => {
    api.getSettings().then((s) => setLlmReady(s.llm_status.ready)).catch(() => {});
  }, []);

  const doRefresh = async (source: string) => {
    setBusy(source);
    setBodyNote(null);
    try {
      const d = await api.refresh(source);
      const out: Record<string, string> = {};
      for (const [k, v] of Object.entries(d.results)) {
        out[k] = typeof v === "string" ? v : JSON.stringify(v);
      }
      setRefreshResults(out);
    } catch (e) {
      setRefreshResults({ [source]: String(e) });
    } finally {
      setBusy(null);
      loadSignals();
      loadItems();
    }
  };

  const doFetchBody = async (item: RawItem) => {
    setFetchingBody(item.id);
    setBodyNote(null);
    try {
      const d = await api.fetchItemBody(item.id);
      setBodyNote(`Fetched ${item.title ?? `item ${item.id}`} — full article stored (${d.truncated ? "truncated preview" : "complete"}). Re-queued for extraction.`);
      loadItems();
    } catch (e) {
      setBodyNote(`Fetch failed: ${e}`);
    } finally {
      setFetchingBody(null);
    }
  };

  return (
    <div>
      <h1>News &amp; signals</h1>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Refresh sources</h2>
        <div className="row">
          {SOURCES.map((s) => (
            <button
              key={s}
              className="ghost sm"
              disabled={busy !== null}
              onClick={() => doRefresh(s)}
            >
              {busy === s ? "…" : s === "all" ? "All + extract" : s}
            </button>
          ))}
        </div>
        {Object.keys(refreshResults).length > 0 && (
          <div className="small" style={{ marginTop: 10 }}>
            {Object.entries(refreshResults).map(([k, v]) => (
              <div key={k} className={v.startsWith("error") ? "err" : "muted"}>
                <b>{k}</b>: {v}
              </div>
            ))}
          </div>
        )}
        {bodyNote && <div className="small" style={{ marginTop: 8 }}>{bodyNote}</div>}
        <div className="small muted" style={{ marginTop: 10 }}>
          {llmReady === null
            ? "Checking LLM status…"
            : llmReady
              ? "LLM extraction active — signals carry LLM confidence bands."
              : "LLM not configured — rule-based extraction only (confidence ≤ 0.6). Set model + API key in Settings."}
        </div>
      </div>

      <div className="panel">
        <div className="card-header">
          <div className="card-title">Player signals</div>
          <span className="muted small">{signals.length} shown</span>
          <div className="spacer" />
          <select value={sigCat} onChange={(e) => setSigCat(e.target.value)}>
            <option value="">all categories</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          <label className="row" style={{ gap: 6, marginBottom: 0 }}>
            <input
              type="checkbox"
              checked={sigActive}
              onChange={(e) => setSigActive(e.target.checked)}
            />
            active only
          </label>
        </div>
        {signals.length === 0 ? (
          <div className="muted small">
            No signals yet. Refresh a source above (e.g. <b>bbc</b> or <b>fpl</b>) — official FPL
            status changes and news articles become per-player signals that the optimizer applies.
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th style={{ width: 18 }}></th>
                <th>Player</th>
                <th>Category</th>
                <th>Conf</th>
                <th>Summary</th>
                <th>Source</th>
                <th>Expires</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.id}>
                  <td><SentimentDot s={s} /></td>
                  <td>
                    <b>{s.web_name ?? `#${s.player_id}`}</b>
                    {s.team_code && <span className="muted"> · {s.team_code}</span>}
                  </td>
                  <td><span className="badge skip">{s.category}</span></td>
                  <td className={s.sentiment === "negative" ? "red" : "green"}>
                    {Math.round(s.confidence * 100)}%
                  </td>
                  <td>
                    {s.summary}
                    {s.url && (
                      <>
                        {" "}
                        <a href={s.url} target="_blank" rel="noreferrer" className="muted small">
                          [link]
                        </a>
                      </>
                    )}
                  </td>
                  <td className="muted small">
                    {s.source}
                    <div className="small">{fmtTime(s.published_at)}</div>
                  </td>
                  <td className="muted small">{hoursLeft(s.expires_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <div className="card-header">
          <div className="card-title">Fetched items</div>
          <span className="muted small">{items.length} shown</span>
          <div className="spacer" />
          <select value={itemSource} onChange={(e) => setItemSource(e.target.value)}>
            <option value="">all sources</option>
            {["bbc", "espn", "reddit", "youtube"].map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <select value={itemKind} onChange={(e) => setItemKind(e.target.value)}>
            <option value="">all kinds</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </div>
        {items.length === 0 ? (
          <div className="muted small">
            Nothing fetched yet. Use the refresh buttons above to pull BBC Football, r/FantasyPL or
            Planet FPL into the pipeline.
          </div>
        ) : (
          <div>
            {items.map((it) => (
              <div key={it.id} style={{ borderBottom: "1px solid #21262d", padding: "8px 0" }}>
                <div className="row" style={{ alignItems: "center" }}>
                  <span className="badge skip">{it.source}</span>
                  <span className="badge skip">{it.kind}</span>
                  {it.processed ? (
                    <span className="badge use">extracted</span>
                  ) : (
                    <span className="badge warn">pending</span>
                  )}
                  <span className="muted small" style={{ marginLeft: "auto" }}>
                    {fmtTime(it.published_at ?? it.retrieved_at)}
                  </span>
                </div>
                <div style={{ marginTop: 4 }}>
                  {it.url ? (
                    <a href={it.url} target="_blank" rel="noreferrer">
                      {it.title ?? "(untitled)"}
                    </a>
                  ) : (
                    <b>{it.title ?? "(untitled)"}</b>
                  )}
                </div>
                {it.takeaways.length > 0 && (
                  <div className="small muted" style={{ marginTop: 4 }}>
                    {it.takeaways.map((t, i) => (
                      <div key={i}>• {t}</div>
                    ))}
                  </div>
                )}
                {it.body && (
                  <details style={{ marginTop: 4 }}>
                    <summary className="small">body preview</summary>
                    <pre className="mono" style={{ background: "var(--bg)", padding: 8, borderRadius: 6, whiteSpace: "pre-wrap", maxHeight: 220, overflowY: "auto" }}>
                      {it.body}
                    </pre>
                  </details>
                )}
                {it.source === "bbc" && it.url && (
                  <div style={{ marginTop: 6 }}>
                    <button
                      className="ghost sm"
                      disabled={fetchingBody !== null}
                      onClick={() => doFetchBody(it)}
                    >
                      {fetchingBody === it.id ? "Fetching…" : "Fetch full article"}
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}