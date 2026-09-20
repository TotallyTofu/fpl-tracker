import type { RuleError } from "../types";

export default function ValidationPanel({ errors }: { errors: RuleError[] }) {
  const hard = errors.filter((e) => e.severity === "error");
  const warn = errors.filter((e) => e.severity === "warning");
  if (hard.length === 0 && warn.length === 0)
    return (
      <div className="panel ok">
        ✓ Valid lineup — ready to save and generate suggestions
      </div>
    );
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Validation</h2>
      {hard.map((e, i) => (
        <div key={`h${i}`} className="err">
          <span className="code">{e.code}</span> {e.message}
        </div>
      ))}
      {warn.map((e, i) => (
        <div key={`w${i}`} className="yellow">
          <span className="code">{e.code}</span> {e.message}
        </div>
      ))}
    </div>
  );
}