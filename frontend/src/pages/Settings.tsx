import { useEffect, useState } from "react";
import { api } from "../api";
import type { SettingsResponse } from "../types";

export default function Settings() {
  const [s, setS] = useState<SettingsResponse | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    api.getSettings().then(setS).catch(() => {});
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
              <td>{s.llm_status.ready ? <span className="ok">ready</span> : <span className="muted">not configured (editor in M4)</span>}</td>
            </tr>
            <tr>
              <td className="muted">Base URL</td>
              <td className="mono">{s.llm_status.base_url}</td>
            </tr>
            <tr>
              <td className="muted">Model</td>
              <td className="mono">{s.llm_status.model ?? "—"}</td>
            </tr>
            <tr>
              <td className="muted">API key</td>
              <td>{s.llm_status.key_set ? "set" : "not set"}</td>
            </tr>
          </tbody>
        </table>
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={testLlm} disabled={testing}>
            {testing ? "Testing…" : "Test connection"}
          </button>
        </div>
        {testResult && <div className="small" style={{ marginTop: 8 }}>{testResult}</div>}
        <div className="small muted" style={{ marginTop: 8 }}>
          {s.llm_status.note}
        </div>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>config.json (read-only in M1)</h2>
        <div className="small muted" style={{ marginBottom: 8 }}>
          Full editor (sources, optimizer weights, LLM fields) arrives in M4.
        </div>
        <pre className="mono" style={{ background: "var(--bg)", padding: 12, borderRadius: 6, overflowX: "auto", fontSize: 12 }}>
          {JSON.stringify(
            { sources: s.sources, llm: s.llm, optimizer: s.optimizer, group: s.group, ui: s.ui },
            null,
            2
          )}
        </pre>
      </div>
    </div>
  );
}