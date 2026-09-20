import type { ChipWindow } from "../types";
import { cost } from "../types";

interface Props {
  name: string;
  setName: (s: string) => void;
  bank: number;
  setBank: (n: number) => void;
  chips: Record<string, number>;
  setChips: (c: Record<string, number>) => void;
  chipWindows: ChipWindow[];
  totalCost: number;
  savedId: number | null;
  saving: boolean;
  onSave: () => void;
  onSetCurrent: () => void;
  onGenerate: () => void;
  onDelete: () => void;
}

const CHIP_LABELS: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  triple_captain: "Triple Captain",
};

function Stepper({ label, value, set, min, max, hint }: {
  label: string; value: number; set: (n: number) => void; min: number; max: number; hint?: string;
}) {
  return (
    <div className="panel" style={{ marginBottom: 0, flex: 1, minWidth: 130 }}>
      <label>{label}</label>
      <div className="row" style={{ alignItems: "center" }}>
        <button className="sm ghost" onClick={() => set(Math.max(min, value - 1))}>−</button>
        <b style={{ minWidth: 20, textAlign: "center" }}>{value}</b>
        <button className="sm ghost" onClick={() => set(Math.min(max, value + 1))}>+</button>
      </div>
      {hint && <div className="small muted" style={{ marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

export default function MetaPanel(p: Props) {
  const remaining = 1000 - p.totalCost;
  const pct = Math.min(100, (p.totalCost / 1000) * 100);
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Lineup meta</h2>
      <label>Name</label>
      <input value={p.name} onChange={(e) => p.setName(e.target.value)} style={{ width: "100%", marginBottom: 10 }} />
      <div className="row">
        <Stepper label="Transfer bank" value={p.bank} set={p.setBank} min={1} max={5} />
        {Object.keys(CHIP_LABELS).map((chip) => {
          const w = p.chipWindows.find((c) => c.chip === chip);
          return (
            <Stepper
              key={chip}
              label={CHIP_LABELS[chip]}
              value={p.chips[chip] ?? 0}
              set={(n) => p.setChips({ ...p.chips, [chip]: n })}
              min={0}
              max={2}
              hint={w ? (w.playable_next_gw ? "open next GW" : "window open (this GW)") : "no window"}
            />
          );
        })}
      </div>
      <div style={{ marginTop: 12 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="small muted">Budget used</span>
          <span className="small">
            {cost(p.totalCost)} / £100.0m {remaining < 0 && <span className="red">over by {cost(-remaining)}</span>}
          </span>
        </div>
        <div className="budget-bar">
          <div className={`budget-fill ${remaining < 0 ? "empty" : ""}`} style={{ width: `${pct}%` }} />
        </div>
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <button onClick={p.onSave} disabled={p.saving || !p.name.trim()}>
          {p.saving ? "Saving…" : p.savedId ? "Save changes" : "Save lineup"}
        </button>
        {p.savedId && (
          <>
            <button className="ghost" onClick={p.onSetCurrent}>Set as current</button>
            <button className="ghost" onClick={p.onGenerate}>Generate suggestions →</button>
            <span className="spacer" />
            <button className="danger ghost" onClick={p.onDelete}>Delete</button>
          </>
        )}
      </div>
    </div>
  );
}