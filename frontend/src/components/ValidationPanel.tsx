import type { RuleError } from "../types";

const CHECKS: { title: string; codes: string[] }[] = [
  { title: "15 players: 2 GK, 5 DEF, 5 MID, 3 FWD", codes: ["SQUAD_SIZE", "SQUAD_COMPOSITION"] },
  { title: "No club over 3 players", codes: ["CLUB_LIMIT"] },
  { title: "Valid XI: 1 GK, at least 3 DEF and 1 FWD", codes: ["XI_SIZE", "XI_POSITION_MIN"] },
  { title: "Bench: GK sub first, then 3 outfield subs", codes: ["BENCH_SIZE", "BENCH_ORDER", "BENCH_GK_SLOT"] },
  { title: "Captain and vice-captain in the XI", codes: ["CAPTAIN_NOT_IN_XI", "VICE_CAPTAIN_NOT_IN_XI", "CAPTAIN_VC_SAME"] },
  { title: "Free transfers 0 to 5", codes: ["BANK_RANGE"] },
];

function Icon({ ok }: { ok: boolean }) {
  return ok ? (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" style={{ flex: "none", marginTop: 1 }}>
      <circle cx="10" cy="10" r="9" fill="var(--good-bg)" />
      <path d="M6 10.5l2.6 2.5L14 7.5" stroke="var(--good)" strokeWidth="2" fill="none" />
    </svg>
  ) : (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" style={{ flex: "none", marginTop: 1 }}>
      <circle cx="10" cy="10" r="9" fill="var(--bad-bg)" />
      <path d="M7 7l6 6M13 7l-6 6" stroke="var(--bad)" strokeWidth="2" />
    </svg>
  );
}

export default function ValidationPanel({ errors }: { errors: RuleError[] }) {
  const hard = errors.filter((e) => e.severity === "error");
  const warn = errors.filter((e) => e.severity === "warning");
  return (
    <section className="card" aria-labelledby="rules-h">
      <h2 id="rules-h">Rule check</h2>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
        {CHECKS.map((c) => {
          const fails = hard.filter((e) => c.codes.includes(e.code));
          return (
            <li key={c.title} style={{ display: "flex", gap: 10 }}>
              <Icon ok={fails.length === 0} />
              <span>
                <b>{c.title}</b>
                {fails.map((e, i) => <span key={i} className="small red" style={{ display: "block" }}>{e.message}</span>)}
              </span>
            </li>
          );
        })}
      </ul>
      {warn.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 12 }}>
          {warn.map((e, i) => <div key={i} className="alert warn" style={{ fontWeight: 500 }}>{e.message}</div>)}
        </div>
      )}
    </section>
  );
}
