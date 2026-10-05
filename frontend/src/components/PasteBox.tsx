import { Fragment, useState } from "react";
import { api } from "../api";
import type { MatchedPlayer, NameMatch } from "../types";

interface Props {
  onLoad: (players: MatchedPlayer[]) => void;
}

export default function PasteBox({ onLoad }: Props) {
  const [text, setText] = useState("");
  const [matches, setMatches] = useState<NameMatch[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expand, setExpand] = useState<Record<number, boolean>>({});

  const runMatch = async () => {
    const names = text
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!names.length) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.matchNames(names);
      setMatches(r.matches);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const loadMatched = () => {
    if (!matches) return;
    const players = matches.filter((m) => m.matched).map((m) => m.matched!);
    onLoad(players);
  };

  return (
    <div className="card">
      <h2>Paste a team list</h2>
      <textarea
        rows={6}
        placeholder={"One player per line, e.g.\nHaaland\nSaka\nKovacic"}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={runMatch} disabled={busy || !text.trim()}>
          {busy ? "Matching…" : "Match names"}
        </button>
        {matches && (
          <button className="ghost" onClick={loadMatched}>
            Load matched ({matches.filter((m) => m.matched).length}/{matches.length})
          </button>
        )}
      </div>
      {error && <div className="err">{error}</div>}
      {matches && (
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>Input</th>
              <th>Match</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {matches.map((m, i) => {
              const ambiguous = !m.matched && m.candidates.length > 1;
              const shown = expand[i] ? m.candidates : m.candidates.slice(0, 1);
              return (
                <Fragment key={i}>
                  <tr>
                    <td>{m.input}</td>
                    <td>
                      {m.matched ? (
                        <span className="badge ok">{m.matched.web_name}</span>
                      ) : m.candidates.length === 1 ? (
                        <span className="badge warn">Maybe {m.candidates[0].web_name}</span>
                      ) : ambiguous ? (
                        <span className="badge warn">Several matches: pick one</span>
                      ) : (
                        <span className="badge out">No match</span>
                      )}
                    </td>
                    <td className="muted">
                      {m.matched
                        ? m.matched.confidence.toFixed(2)
                        : m.candidates[0]
                          ? m.candidates[0].confidence.toFixed(2)
                          : "—"}
                    </td>
                  </tr>
                  {ambiguous &&
                    shown.map((c) => (
                      <tr key={`${i}-${c.player_id}`} className="muted small">
                        <td />
                        <td colSpan={2}>
                          · {c.web_name} ({c.confidence.toFixed(2)}) — use the picker to resolve
                        </td>
                      </tr>
                    ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
      <div className="help" style={{ marginTop: 8 }}>
        Names that don't match: find them with the player search.
      </div>
    </div>
  );
}