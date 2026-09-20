import ApplyChecklist from "./ApplyChecklist";
import DiffTable from "./DiffTable";
import PitchView from "./PitchView";
import type { Suggestion } from "../types";

const DESC: Record<string, string> = {
  max_ep: "Highest projected points for the target GW",
  differential: "Same idea, but favours low-ownership picks (λ bonus)",
  safe: "Weighted by reliability — avoids doubt/50% risk",
};

const CHIP_LABELS: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  triple_captain: "Triple Captain",
};

export default function SuggestionCard({ s }: { s: Suggestion }) {
  const proj = s.projected_points;
  return (
    <div className="panel">
      <div className="card-header">
        <span className="card-title">{s.profile.replace("_", " ")}</span>
        {s.variant_of && <span className="badge skip">variant of {s.variant_of}</span>}
        <span className="spacer" />
        <span className="big-num">{proj.adjusted.toFixed(1)}</span>
        <span className="sub">
          baseline {proj.baseline.toFixed(1)} → adjusted {proj.adjusted.toFixed(1)} (captain ×2 incl.)
        </span>
      </div>
      <div className="small muted" style={{ marginBottom: 10 }}>
        {DESC[s.profile] ?? ""}
      </div>
      <PitchView players={s.lineup.squad} compact />
      <div style={{ marginTop: 12 }}>
        <DiffTable diff={s.diff} />
      </div>
      <h2>Chip advice</h2>
      {s.chip_advice.map((c) => (
        <div key={c.chip} className="row" style={{ alignItems: "center", marginBottom: 4 }}>
          <span className={`badge ${c.recommendation}`}>{c.recommendation}</span>
          <b>{CHIP_LABELS[c.chip] ?? c.chip}</b>
          <span className="muted small">{c.reason}</span>
        </div>
      ))}
      {s.rationale.notes.length > 0 && (
        <>
          <h2>Rationale</h2>
          <ol className="checklist">
            {s.rationale.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ol>
        </>
      )}
      <div style={{ marginTop: 10 }}>
        <ApplyChecklist s={s} />
      </div>
    </div>
  );
}