import { useEffect, useState } from "react";
import { api } from "../api";
import WeightSlider from "../components/WeightSlider";
import type {
  AvailabilityConfig,
  BbcSource,
  DbStats,
  EspnSource,
  FplSource,
  GroupConfig,
  LLMConfig,
  OptimizerWeights,
  RedditSource,
  SettingsResponse,
  SignalConfig,
  SolverConfig,
  YouTubeSource,
} from "../types";

type Draft = Omit<SettingsResponse, "llm_status" | "pulp_available">;

function fmtBytes(n: number) {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

export default function Settings() {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [status, setStatus] = useState<SettingsResponse["llm_status"] | null>(null);
  const [pulpAvailable, setPulpAvailable] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [stats, setStats] = useState<DbStats | null>(null);
  const [dataMsg, setDataMsg] = useState<string | null>(null);
  const [dataBusy, setDataBusy] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then((r) => {
        const { llm_status, pulp_available, ...rest } = r;
        setDraft(rest);
        setStatus(llm_status);
        setPulpAvailable(!!pulp_available);
      })
      .catch(() => {});
    api.getDbStats().then(setStats).catch(() => {});
  }, []);

  const markDirty = () => {
    setDirty(true);
    setSaveMsg(null);
  };

  const set = (fn: (d: Draft) => Draft) => {
    setDraft((d) => (d ? fn(d) : d));
    markDirty();
  };

  const upLlm = (p: Partial<LLMConfig>) => set((d) => ({ ...d, llm: { ...d.llm, ...p } }));
  const upFpl = (p: Partial<FplSource>) =>
    set((d) => ({ ...d, sources: { ...d.sources, fpl: { ...d.sources.fpl, ...p } } }));
  const upEspn = (p: Partial<EspnSource>) =>
    set((d) => ({ ...d, sources: { ...d.sources, espn: { ...d.sources.espn, ...p } } }));
  const upBbc = (p: Partial<BbcSource>) =>
    set((d) => ({ ...d, sources: { ...d.sources, bbc: { ...d.sources.bbc, ...p } } }));
  const upReddit = (p: Partial<RedditSource>) =>
    set((d) => ({ ...d, sources: { ...d.sources, reddit: { ...d.sources.reddit, ...p } } }));
  const upYoutube = (p: Partial<YouTubeSource>) =>
    set((d) => ({ ...d, sources: { ...d.sources, youtube: { ...d.sources.youtube, ...p } } }));
  const upWeights = (w: OptimizerWeights) =>
    set((d) => ({ ...d, optimizer: { ...d.optimizer, weights: w } }));
  const upAvail = (p: Partial<AvailabilityConfig>) =>
    set((d) => ({ ...d, optimizer: { ...d.optimizer, availability: { ...d.optimizer.availability, ...p } } }));
  const upSignal = (p: Partial<SignalConfig>) =>
    set((d) => ({ ...d, optimizer: { ...d.optimizer, signal: { ...d.optimizer.signal, ...p } } }));
  const upSolver = (p: Partial<SolverConfig>) =>
    set((d) => ({ ...d, optimizer: { ...d.optimizer, solver: { ...d.optimizer.solver, ...p } } }));
  const upOptTop = (p: Partial<Pick<NonNullable<Draft>["optimizer"], "differential_lambda" | "differential_ep_floor">>) =>
    set((d) => ({ ...d, optimizer: { ...d.optimizer, ...p } }));
  const upGroup = (p: Partial<GroupConfig>) => set((d) => ({ ...d, group: { ...d.group, ...p } }));

  const save = async () => {
    if (!draft) return;
    const w = draft.optimizer.weights;
    const sum = Math.round((w.ep + w.form + w.fixture) * 100) / 100;
    if (Math.abs(sum - 1) > 0.01) {
      setSaveMsg(`Optimizer weights must sum to 1.00 (got ${sum.toFixed(2)}) — fix them or use Auto-normalize first.`);
      return;
    }
    setSaving(true);
    setSaveMsg(null);
    try {
      const updated = await api.putSettings(draft);
      const { llm_status, pulp_available, ...rest } = updated;
      setDraft(rest);
      setStatus(llm_status);
      setPulpAvailable(!!pulp_available);
      setDirty(false);
      setSaveMsg("Saved — config.json updated on disk.");
    } catch (e) {
      setSaveMsg(`Save failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const testLlm = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.testLlm();
      setTestResult(r.ok ? `OK — model ${r.model} replied: "${r.reply}"` : `Failed: ${r.error}`);
    } catch (e) {
      setTestResult(String(e));
    } finally {
      setTesting(false);
    }
  };

  const refreshAll = async () => {
    setDataBusy("refresh");
    setDataMsg(null);
    try {
      const r = await api.refresh("all");
      const parts = Object.entries(r.results).map(([k, v]) =>
        typeof v === "string" ? `${k}: ${v}` : `${k}: ${JSON.stringify(v)}`
      );
      setDataMsg(parts.join(" · "));
    } catch (e) {
      setDataMsg(`Refresh failed: ${e}`);
    } finally {
      setDataBusy(null);
    }
  };

  const clearSignals = async () => {
    if (!window.confirm("Delete ALL extracted signals? News items are kept.")) return;
    setDataBusy("clear");
    setDataMsg(null);
    try {
      const r = await api.clearSignals();
      setDataMsg(`Cleared ${r.cleared} signal(s).`);
    } catch (e) {
      setDataMsg(`Clear failed: ${e}`);
    } finally {
      setDataBusy(null);
    }
  };

  if (!draft) return <div className="muted">loading…</div>;

  const s = draft.sources;
  const o = draft.optimizer;

  return (
    <div>
      <div className="row" style={{ alignItems: "center", gap: 12 }}>
        <h1 style={{ margin: 0 }}>Settings</h1>
        <span className="spacer" />
        {dirty && <span className="small muted">unsaved changes</span>}
        <button onClick={save} disabled={saving || !dirty}>
          {saving ? "Saving…" : "Save all changes"}
        </button>
      </div>
      {saveMsg && <div className="small" style={{ marginTop: 8 }}>{saveMsg}</div>}

      {/* ------------------------------------------------------------- LLM */}
      <div className="panel" style={{ marginTop: 16 }}>
        <h2 style={{ marginTop: 0 }}>LLM (signal extraction)</h2>
        <table>
          <tbody>
            <tr>
              <td className="muted">Status</td>
              <td>
                {status?.ready ? <span className="ok">ready</span> : <span className="muted">not configured</span>}
                {" — "}
                <span className="mono small">{status?.base_url}</span>
                {status?.model ? <span className="mono small"> · {status.model}</span> : null}
                {" · key "}
                {status?.key_set ? "set" : "not set"}
              </td>
            </tr>
          </tbody>
        </table>
        <div className="grid3" style={{ marginTop: 12 }}>
          <div>
            <label>
              <input
                type="checkbox"
                checked={draft.llm.enabled}
                onChange={(e) => upLlm({ enabled: e.target.checked })}
              />{" "}
              enabled
            </label>
          </div>
          <div>
            <label>Model name</label>
            <input value={draft.llm.model} onChange={(e) => upLlm({ model: e.target.value })} placeholder="e.g. local-llm" />
          </div>
          <div>
            <label>API key (stored locally only)</label>
            <input
              type="password"
              value={draft.llm.api_key}
              onChange={(e) => upLlm({ api_key: e.target.value })}
              placeholder="your key"
            />
          </div>
          <div>
            <label>Base URL</label>
            <input
              value={draft.llm.base_url}
              onChange={(e) => upLlm({ base_url: e.target.value })}
              placeholder="http://localhost:8888/v1"
            />
          </div>
          <div>
            <label>Timeout (s)</label>
            <input
              type="number"
              min={5}
              value={draft.llm.timeout_sec}
              onChange={(e) => upLlm({ timeout_sec: Number(e.target.value) })}
            />
          </div>
          <div>
            <label>Batch chars</label>
            <input
              type="number"
              min={500}
              step={500}
              value={draft.llm.batch_chars}
              onChange={(e) => upLlm({ batch_chars: Number(e.target.value) })}
            />
          </div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="ghost" onClick={testLlm} disabled={testing}>
            {testing ? "Testing…" : "Test connection"}
          </button>
        </div>
        {testResult && <div className="small" style={{ marginTop: 8 }}>{testResult}</div>}
        <div className="small muted" style={{ marginTop: 8 }}>
          {status?.note}
        </div>
      </div>

      {/* --------------------------------------------------------- Sources */}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Sources</h2>

        <h3>FPL official</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            <input type="checkbox" checked={s.fpl.enabled} onChange={(e) => upFpl({ enabled: e.target.checked })} />{" "}
            enabled
          </label>
          <label>
            bootstrap every (min){" "}
            <input
              type="number"
              min={5}
              style={{ width: 70 }}
              value={s.fpl.bootstrap_interval_min}
              onChange={(e) => upFpl({ bootstrap_interval_min: Number(e.target.value) })}
            />
          </label>
          <span className="small muted">live-score polling is out of scope in v1 (revised scope)</span>
        </div>

        <h3>ESPN</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            <input type="checkbox" checked={s.espn.enabled} onChange={(e) => upEspn({ enabled: e.target.checked })} />{" "}
            enabled
          </label>
          <label>
            news every (min){" "}
            <input
              type="number"
              min={5}
              style={{ width: 70 }}
              value={s.espn.news_interval_min}
              onChange={(e) => upEspn({ news_interval_min: Number(e.target.value) })}
            />
          </label>
          <span className="small muted">off by default — ESPN returned 403 from this network (verified 2026-09-19)</span>
        </div>

        <h3>BBC Sport (RSS)</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            <input type="checkbox" checked={s.bbc.enabled} onChange={(e) => upBbc({ enabled: e.target.checked })} />{" "}
            enabled
          </label>
          <label>
            every (min){" "}
            <input
              type="number"
              min={5}
              style={{ width: 70 }}
              value={s.bbc.interval_min}
              onChange={(e) => upBbc({ interval_min: Number(e.target.value) })}
            />
          </label>
        </div>

        <h3>Reddit r/FantasyPL (RSS)</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            <input type="checkbox" checked={s.reddit.enabled} onChange={(e) => upReddit({ enabled: e.target.checked })} />{" "}
            enabled
          </label>
          <label>
            every (min){" "}
            <input
              type="number"
              min={5}
              style={{ width: 70 }}
              value={s.reddit.interval_min}
              onChange={(e) => upReddit({ interval_min: Number(e.target.value) })}
            />
          </label>
          <label>
            mode{" "}
            <select value={s.reddit.mode} onChange={(e) => upReddit({ mode: e.target.value })}>
              <option value="rss">rss</option>
              <option value="oauth">oauth</option>
            </select>
          </label>
        </div>
        <div className="grid3" style={{ marginTop: 8, opacity: s.reddit.mode === "oauth" ? 1 : 0.5 }}>
          <div>
            <label>OAuth client id</label>
            <input
              value={s.reddit.oauth_client_id}
              disabled={s.reddit.mode !== "oauth"}
              onChange={(e) => upReddit({ oauth_client_id: e.target.value })}
            />
          </div>
          <div>
            <label>OAuth client secret</label>
            <input
              type="password"
              value={s.reddit.oauth_client_secret}
              disabled={s.reddit.mode !== "oauth"}
              onChange={(e) => upReddit({ oauth_client_secret: e.target.value })}
            />
          </div>
        </div>
        <div className="small muted" style={{ marginTop: 6 }}>
          OAuth mode is a documented future extension — RSS is used until then (create a free “script” app at
          reddit.com/prefs/apps if you want to try it later).
        </div>

        <h3>YouTube (channel RSS + transcripts)</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            <input
              type="checkbox"
              checked={s.youtube.enabled}
              onChange={(e) => upYoutube({ enabled: e.target.checked })}
            />{" "}
            enabled
          </label>
          <label>
            every (min){" "}
            <input
              type="number"
              min={5}
              style={{ width: 70 }}
              value={s.youtube.interval_min}
              onChange={(e) => upYoutube({ interval_min: Number(e.target.value) })}
            />
          </label>
          <label>
            max transcripts/poll{" "}
            <input
              type="number"
              min={0}
              style={{ width: 60 }}
              value={s.youtube.max_transcripts_per_poll}
              onChange={(e) => upYoutube({ max_transcripts_per_poll: Number(e.target.value) })}
            />
          </label>
        </div>
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>Handle</th>
              <th>Channel ID (optional — resolved automatically if empty)</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {s.youtube.channels.map((ch, i) => (
              <tr key={i}>
                <td>
                  <input
                    value={ch.handle}
                    placeholder="@ChannelName"
                    onChange={(e) => {
                      const channels = s.youtube.channels.map((c, j) => (j === i ? { ...c, handle: e.target.value } : c));
                      upYoutube({ channels });
                    }}
                  />
                </td>
                <td>
                  <input
                    value={ch.channel_id}
                    placeholder="UC…"
                    onChange={(e) => {
                      const channels = s.youtube.channels.map((c, j) => (j === i ? { ...c, channel_id: e.target.value } : c));
                      upYoutube({ channels });
                    }}
                  />
                </td>
                <td>
                  <button
                    className="sm ghost danger"
                    onClick={() => upYoutube({ channels: s.youtube.channels.filter((_, j) => j !== i) })}
                  >
                    remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row" style={{ marginTop: 6 }}>
          <button
            className="sm ghost"
            onClick={() => upYoutube({ channels: [...s.youtube.channels, { handle: "", channel_id: "" }] })}
          >
            + Add channel
          </button>
        </div>
        <div style={{ marginTop: 8 }}>
          <label>Transcript keywords (comma-separated)</label>
          <input
            value={s.youtube.transcript_keywords.join(", ")}
            onChange={(e) =>
              upYoutube({
                transcript_keywords: e.target.value
                  .split(",")
                  .map((k) => k.trim())
                  .filter(Boolean),
              })
            }
          />
        </div>
      </div>

      {/* ------------------------------------------------------- Optimizer */}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Optimizer</h2>

        <h3>Score weights (must sum to 1.00)</h3>
        <WeightSlider value={o.weights} onChange={upWeights} />

        <h3>Availability map</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            <input
              type="checkbox"
              checked={o.availability.active}
              onChange={(e) => upAvail({ active: e.target.checked })}
            />{" "}
            active (off = hard gates only)
          </label>
          <label>
            doubt multiplier{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.availability.doubt}
              onChange={(e) => upAvail({ doubt: Number(e.target.value) })}
            />
          </label>
          <label>
            chance 100%{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.availability.chance_100}
              onChange={(e) => upAvail({ chance_100: Number(e.target.value) })}
            />
          </label>
          <label>
            chance 50%{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.availability.chance_50}
              onChange={(e) => upAvail({ chance_50: Number(e.target.value) })}
            />
          </label>
          <label>
            chance 0%{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.availability.chance_0}
              onChange={(e) => upAvail({ chance_0: Number(e.target.value) })}
            />
          </label>
        </div>

        <h3>Signal coefficients</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            negative per signal{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.signal.neg_per}
              onChange={(e) => upSignal({ neg_per: Number(e.target.value) })}
            />
          </label>
          <label>
            negative cap{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.signal.neg_cap}
              onChange={(e) => upSignal({ neg_cap: Number(e.target.value) })}
            />
          </label>
          <label>
            positive per signal{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.signal.pos_per}
              onChange={(e) => upSignal({ pos_per: Number(e.target.value) })}
            />
          </label>
          <label>
            positive cap{" "}
            <input
              type="number"
              step={0.05}
              style={{ width: 70 }}
              value={o.signal.pos_cap}
              onChange={(e) => upSignal({ pos_cap: Number(e.target.value) })}
            />
          </label>
        </div>

        <h3>Differential &amp; solver</h3>
        <div className="row" style={{ gap: 16 }}>
          <label>
            differential λ{" "}
            <input
              type="number"
              step={0.5}
              style={{ width: 70 }}
              value={o.differential_lambda}
              onChange={(e) => upOptTop({ differential_lambda: Number(e.target.value) })}
            />
          </label>
          <label>
            differential EP floor{" "}
            <input
              type="number"
              step={0.1}
              style={{ width: 70 }}
              value={o.differential_ep_floor}
              onChange={(e) => upOptTop({ differential_ep_floor: Number(e.target.value) })}
            />
          </label>
          <label>
            restarts{" "}
            <input
              type="number"
              min={1}
              style={{ width: 70 }}
              value={o.solver.restarts}
              onChange={(e) => upSolver({ restarts: Number(e.target.value) })}
            />
          </label>
          <label>
            timebox (s){" "}
            <input
              type="number"
              min={1}
              max={60}
              style={{ width: 70 }}
              value={o.solver.timebox_sec}
              onChange={(e) => upSolver({ timebox_sec: Number(e.target.value) })}
            />
          </label>
          <label>
            seed{" "}
            <input
              type="number"
              style={{ width: 70 }}
              value={o.solver.seed}
              onChange={(e) => upSolver({ seed: Number(e.target.value) })}
            />
          </label>
          <label style={{ opacity: pulpAvailable ? 1 : 0.5 }}>
            <input
              type="checkbox"
              checked={o.solver.exact_ilp}
              disabled={!pulpAvailable}
              onChange={(e) => upSolver({ exact_ilp: e.target.checked })}
            />{" "}
            exact ILP (PuLP)
          </label>
        </div>
        {!pulpAvailable && (
          <div className="small muted" style={{ marginTop: 6 }}>
            Exact ILP is greyed out until PuLP is installed: <span className="mono">pip install pulp</span>
          </div>
        )}
      </div>

      {/* ---------------------------------------------------------- Group */}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Group</h2>
        <div className="row" style={{ gap: 12 }}>
          <label>
            FPL entry ID{" "}
            <input
              type="number"
              style={{ width: 140 }}
              value={draft.group.fpl_entry_id}
              onChange={(e) => upGroup({ fpl_entry_id: e.target.value })}
              placeholder="e.g. 12345678"
            />
          </label>
        </div>
        <div className="small muted" style={{ marginTop: 6 }}>
          The group-rank card is optional and was skipped in v1 (no entry ID provided). If you set an ID here, a
          future update can surface your league rank on the Dashboard.
        </div>
      </div>

      {/* ----------------------------------------------------------- Data */}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Data</h2>
        <div className="row" style={{ gap: 10 }}>
          <button onClick={refreshAll} disabled={dataBusy !== null}>
            {dataBusy === "refresh" ? "Refreshing…" : "Re-fetch all now"}
          </button>
          <button className="ghost danger" onClick={clearSignals} disabled={dataBusy !== null}>
            {dataBusy === "clear" ? "Clearing…" : "Clear all signals"}
          </button>
        </div>
        {dataMsg && <div className="small" style={{ marginTop: 8 }}>{dataMsg}</div>}

        {stats && (
          <>
            <h3>Database</h3>
            <div className="row" style={{ gap: 24 }}>
              {Object.entries(stats.row_counts).map(([t, n]) => (
                <span key={t} className="small">
                  <b>{n}</b> <span className="muted">{t}</span>
                </span>
              ))}
            </div>
            <div className="small muted" style={{ marginTop: 6 }}>
              DB size: {fmtBytes(stats.db_size_bytes)}
            </div>
            {stats.recent_errors.length > 0 && (
              <>
                <h3>Recent poll errors</h3>
                <table>
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Source</th>
                      <th>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stats.recent_errors.map((e, i) => (
                      <tr key={i}>
                        <td className="muted small">{e.finished_at}</td>
                        <td>{e.source}</td>
                        <td className="small">{e.error ?? e.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        )}
      </div>

      {/* ---------------------------------------------------------- About */}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>About</h2>
        <div className="small">
          <b>FPL Tracker v1.0.0</b> — local-first FPL team optimizer. No FPL login by design: data comes from public
          endpoints only, and your API key (if any) is stored in this machine's config.json / .env and sent only to
          your LLM endpoint.
        </div>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>Source</th>
              <th>Method</th>
              <th>Default cadence</th>
              <th>Posture</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>FPL official</td>
              <td>public REST API</td>
              <td>15 min</td>
              <td>read-only, no account</td>
            </tr>
            <tr>
              <td>BBC Sport</td>
              <td>RSS</td>
              <td>30 min</td>
              <td>light polling, no scraping</td>
            </tr>
            <tr>
              <td>Reddit r/FantasyPL</td>
              <td>RSS</td>
              <td>30 min</td>
              <td>rate-limit aware (circuit breaker)</td>
            </tr>
            <tr>
              <td>YouTube</td>
              <td>channel RSS + yt-dlp transcripts</td>
              <td>60 min</td>
              <td>metadata + transcripts only</td>
            </tr>
            <tr>
              <td>ESPN</td>
              <td>REST (opt-in)</td>
              <td>30 min</td>
              <td>disabled by default (403 on this network)</td>
            </tr>
          </tbody>
        </table>
        <div className="small muted" style={{ marginTop: 10 }}>
          Suggestions are projections, not guarantees — verify news on the official FPL site before the deadline.
          Design docs live in the repo: <span className="mono">OUTLINE.MD</span>, <span className="mono">PLAN.MD</span>–
          <span className="mono">PLAN-5-M4-POLISH.MD</span>.
        </div>
      </div>
    </div>
  );
}