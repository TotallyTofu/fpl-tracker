import { useState } from "react";
import { api } from "../api";
import ApplyChecklist from "./ApplyChecklist";
import DiffTable from "./DiffTable";
import PitchView from "./PitchView";
import type { SquadPlayer, Suggestion } from "../types";
import { fmtTime } from "../types";

function DiffEdge({ squad }: { squad: SquadPlayer[] }) {
  const starters = squad.filter((p) => p.role === "starter");
  const low = starters.filter((p) => (p.selected_by_percent ?? 100) < 10);
  if (starters.length === 0) return null;
  return (
    <div style={{ marginTop: 4 }}>
      🎯 Mini-league edge: {low.length} of {starters.length} starters owned by &lt;10% of the FPL public
    </div>
  );
}

const DESC: Record<string, string> = {
  max_ep: "Highest projected points for the target GW — stays within your free-transfer bank",
  differential: "Same idea, but favours low-ownership picks (λ bonus) — stays within your free-transfer bank",
  safe: "Weighted by reliability — avoids doubt/50% risk; strictly within your free-transfer bank",
};

const CHIP_LABELS: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  triple_captain: "Triple Captain",
};

export default function SuggestionCard({
  s,
  canApply = true,
  onApplied,
}: {
  s: Suggestion;
  canApply?: boolean;
  onApplied?: () => void;
}) {
  const proj = s.projected_points;
  const [applying, setApplying] = useState(false);
  const [applyErr, setApplyErr] = useState<string | null>(null);
  const [appliedAt, setAppliedAt] = useState<string | null>(s.applied_at ?? null);

  const apply = async () => {
    setApplying(true);
    setApplyErr(null);
    try {
      await api.applySuggestion(s.id);
      setAppliedAt(new Date().toISOString());
      onApplied?.();
    } catch (e) {
      setApplyErr(String(e));
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="panel">
      <div className="card-header">
        <span className="card-title">{s.profile.replace("_", " ")}</span>
        {s.variant_of && <span className="badge skip">variant of {s.variant_of}</span>}
        <span className="spacer" />
        <span className="big-num">{proj.adjusted.toFixed(1)}</span>
        <span className="sub">
          FPL baseline {proj.baseline.toFixed(1)} (raw EP, no signals/chips) → adjusted{" "}
          {proj.adjusted.toFixed(1)} (incl. availability, signals, captain ×2)
          {proj.penalty_points ? (
            <>
              {" "}→ net {proj.net_after_transfers} after −{proj.penalty_points} transfer penalty
            </>
          ) : null}
        </span>
      </div>
      <div className="small muted" style={{ marginBottom: 10 }}>
        {DESC[s.profile] ?? ""}
        {s.profile === "differential" && <DiffEdge squad={s.lineup.squad} />}
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
      <div className="row" style={{ marginTop: 12, alignItems: "center" }}>
        {appliedAt ? (
          <span className="badge use">✓ applied {fmtTime(appliedAt)}</span>
        ) : canApply ? (
          <button onClick={apply} disabled={applying}>
            {applying ? "Applying…" : "Apply this suggestion"}
          </button>
        ) : (
          <span className="small muted">
            ⚗ Test lineup — apply is disabled (it would only affect the sandbox).
          </span>
        )}
        {applyErr && <span className="err small">{applyErr}</span>}
      </div>
    </div>
  );
}