import { useEffect, useState } from "react";
import { api } from "../api";
import type { SettingsResponse } from "../types";

export default function Settings() {
  const [s, setS] = useState<SettingsResponse | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // editable LLM fields (mirror of s.llm, once loaded)
  const [llmModel, setLlmModel] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmBase, setLlmBase] = useState("");

  useEffect(() => {
    api
      .getSettings()
      .then((cfg) => {
        setS(cfg);
        setLlmModel(cfg.llm.model ?? "");
        setLlmKey(cfg.llm.api_key ?? "");
        setLlmBase(cfg.llm.base_url ?? "");
      })
      .catch(() => {});
  }, []);

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

  const saveLlm = async () => {
    if (!s) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      const body = { ...s, llm: { ...s.llm, model: llmModel, api_key: llmKey, base_url: llmBase } };
      delete (body as Record<string, unknown>).llm_status;
      const updated = await api.putSettings(body);
      setS(updated);
      setSaveMsg("Saved.");
    } catch (e) {
      setSaveMsg(`Save failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  if (!s) return <div className="muted">loading…</div>;

  return (
    <div>
      <h1>Settings</h1>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>LLM</h2>
        <table>
          <tbody>
            <tr>
              <td className="muted">Status</td>
              <td>{s.llm_status.ready ? <span className="ok">ready</span> : <span className="muted">not configured</span>}</td>
            </tr>
            <tr>
              <td className="muted">Active base URL</td>
              <td className="mono">{s.llm_status.base_url}</td>
            </tr>
            <tr>
              <td className="muted">Active model</td>
              <td className="mono">{s.llm_status.model ?? "—"}</td>
            </tr>
            <tr>
              <td className="muted">API key</td>
              <td>{s.llm_status.key_set ? "set" : "not set"}</td>
            </tr>
          </tbody>
        </table>

        <div className="grid3" style={{ marginTop: 12 }}>
          <div>
            <label>Model name</label>
            <input value={llmModel} onChange={(e) => setLlmModel(e.target.value)} placeholder="e.g. local-llm" />
          </div>
          <div>
            <label>API key</label>
            <input type="password" value={llmKey} onChange={(e) => setLlmKey(e.target.value)} placeholder="your key" />
          </div>
          <div>
            <label>Base URL</label>
            <input value={llmBase} onChange={(e) => setLlmBase(e.target.value)} placeholder="http://localhost:8888/v1" />
          </div>
        </div>

        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={saveLlm} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
          <button className="ghost" onClick={testLlm} disabled={testing}>
            {testing ? "Testing…" : "Test connection"}
          </button>
        </div>
        {testResult && <div className="small" style={{ marginTop: 8 }}>{testResult}</div>}
        {saveMsg && <div className="small" style={{ marginTop: 8 }}>{saveMsg}</div>}
        <div className="small muted" style={{ marginTop: 8 }}>
          {s.llm_status.note} Values stored in config.json (local only); .env wins when set.
        </div>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>config.json (rest read-only in M2)</h2>
        <div className="small muted" style={{ marginBottom: 8 }}>
          Full editor (sources, optimizer weights) arrives in M4.
        </div>
        <pre className="mono" style={{ background: "var(--bg)", padding: 12, borderRadius: 6, overflowX: "auto", fontSize: 12 }}>
          {JSON.stringify(
            { sources: s.sources, optimizer: s.optimizer, group: s.group, ui: s.ui },
            null,
            2
          )}
        </pre>
      </div>
    </div>
  );
}