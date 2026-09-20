import type { OptimizerWeights } from "../types";

const DEFAULTS: OptimizerWeights = { ep: 0.7, form: 0.15, fixture: 0.15 };

const DESC: Record<keyof OptimizerWeights, string> = {
  ep: "EP — official FPL points projection (next GW). The backbone of every suggestion.",
  form: "Form — average points over the player's last 5 gameweeks. Raises it to chase in-form players.",
  fixture: "Fixture — opponent difficulty for the next GW (xG-based). Raises it to favour easier opponents.",
};

function round2(n: number) {
  return Math.round(n * 100) / 100;
}

export default function WeightSlider({
  value,
  onChange,
}: {
  value: OptimizerWeights;
  onChange: (w: OptimizerWeights) => void;
}) {
  const sum = value.ep + value.form + value.fixture;
  const sumRounded = Math.round(sum * 100) / 100;
  const sumOk = Math.abs(sumRounded - 1.0) <= 0.01;

  const set = (k: keyof OptimizerWeights, v: number) => onChange({ ...value, [k]: v });

  const normalize = () => {
    if (sum <= 0) return onChange({ ...DEFAULTS });
    // Round ep + form, give fixture the remainder so the sum is exactly 1.00.
    const ep = round2(value.ep / sum);
    const form = round2(value.form / sum);
    const fixture = Math.max(0, round2(1 - ep - form));
    onChange({ ep, form, fixture });
  };

  const rows: (keyof OptimizerWeights)[] = ["ep", "form", "fixture"];

  return (
    <div>
      {rows.map((k) => (
        <div key={k} className="row" style={{ gap: 10, marginBottom: 8 }} title={DESC[k]}>
          <span style={{ width: 72 }} className="small">
            {k === "ep" ? "EP" : k === "form" ? "Form" : "Fixture"}
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={value[k]}
            onChange={(e) => set(k, Number(e.target.value))}
            style={{ flex: 1 }}
          />
          <span className="mono small" style={{ width: 44, textAlign: "right" }}>
            {value[k].toFixed(2)}
          </span>
        </div>
      ))}
      <div className="row" style={{ alignItems: "center", gap: 10, marginTop: 6 }}>
        <span className={`small ${sumOk ? "ok" : "err"}`}>
          sum = {sum.toFixed(2)} {sumOk ? "✓" : "(must be 1.00)"}
        </span>
        <button className="sm ghost" onClick={normalize} disabled={sumOk}>
          Auto-normalize
        </button>
        <button className="sm ghost" onClick={() => onChange({ ...DEFAULTS })}>
          Reset to defaults (0.7 / 0.15 / 0.15)
        </button>
      </div>
      <div className="small muted" style={{ marginTop: 6 }}>
        {rows.map((k) => (
          <div key={k} style={{ marginBottom: 2 }}>
            <b>{k.toUpperCase()}</b>: {DESC[k].split("—")[1]?.trim()}
          </div>
        ))}
        <div style={{ marginTop: 4 }}>Applies to the next Generate — no rebuild needed.</div>
      </div>
    </div>
  );
}