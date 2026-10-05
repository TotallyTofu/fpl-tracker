import type { Suggestion } from "../types";
import { CHIP_LABEL, PROFILE_LABEL, money, signed } from "../types";

const BLURB: Record<string, string> = {
  max_ep: "Most projected points without going over your free transfers.",
  differential: "Favours players few other managers own.",
  safe: "Weights each pick by how likely he is to play.",
};

const ORDER: Record<string, number> = { max_ep: 0, differential: 1, safe: 2 };

/** The plans of the newest run: one per profile, made within two minutes of
 *  the newest row (older versions stamped each profile separately). */
export function latestRun(rows: Suggestion[]): Suggestion[] {
  if (!rows.length) return [];
  const newest = rows.reduce((a, b) => (a.id > b.id ? a : b));
  const t0 = new Date(newest.generated_at).getTime();
  const byProfile = new Map<string, Suggestion>();
  for (const r of [...rows].sort((a, b) => b.id - a.id)) {
    if (r.target_gw !== newest.target_gw) continue;
    if (t0 - new Date(r.generated_at).getTime() > 120_000) continue;
    if (!byProfile.has(r.profile)) byProfile.set(r.profile, r);
  }
  return [...byProfile.values()].sort((a, b) => (ORDER[a.profile] ?? 9) - (ORDER[b.profile] ?? 9));
}

export function planNet(s: Suggestion): number {
  return s.projected_points.net_after_transfers ?? s.projected_points.adjusted;
}

export function planTag(s: Suggestion, best: boolean): { text: string; cls: string } {
  const pen = s.diff.penalty_points;
  if (s.diff.transfer_cap_exceeded) return { text: "Not allowed", cls: "out" };
  if (pen > 0) return { text: `−${pen} pt hit`, cls: "out" };
  if (s.variant_of) return { text: `Same team as ${PROFILE_LABEL[s.variant_of] ?? s.variant_of}`, cls: "skip" };
  if (best) return { text: "Best", cls: "dark" };
  return { text: s.profile === "safe" ? "Steady" : "Alternative", cls: "skip" };
}

export default function PlanCard({ s, best, selected, onSelect }: {
  s: Suggestion; best: boolean; selected: boolean; onSelect: () => void;
}) {
  const d = s.diff;
  const net = planNet(s);
  const cur = s.projected_points.current_team;
  const vs = cur != null ? net - cur : null;
  const n = Math.max(d.transfers_in.length, d.transfers_out.length);
  const cap = s.lineup.squad.find((p) => p.player_id === s.lineup.captain)?.web_name ?? "—";
  const tag = planTag(s, best);
  const transfers = d.chip_covers
    ? `${n}, free with ${CHIP_LABEL[d.chip_played ?? ""] ?? "a chip"}`
    : d.bank_before != null
      ? `${n} of ${d.bank_before} free`
      : String(n);
  return (
    <button type="button" className="plan" aria-pressed={selected} onClick={onSelect}>
      <span className="plan-top">
        <span className="plan-name">{PROFILE_LABEL[s.profile] ?? s.profile}</span>
        <span className={`badge ${tag.cls}`}>{tag.text}</span>
      </span>
      <span className="small muted">{BLURB[s.profile] ?? ""}</span>
      <span className="plan-num">
        <span className="n">{net.toFixed(1)}</span>
        <span style={{ display: "flex", flexDirection: "column", fontSize: 13 }}>
          <b>points, after hits</b>
          {vs != null && <span className={vs >= 0 ? "up" : "down"}>{signed(vs)} vs no changes</span>}
        </span>
      </span>
      {d.penalty_points > 0 && (
        <span className="alert bad">
          {n} transfers is {n - (d.bank_before ?? 0)} more than you have free: −{d.penalty_points} points.
        </span>
      )}
      <span className="plan-grid">
        <span><span className="k">Transfers</span><span className="v">{transfers}</span></span>
        <span>
          <span className="k">Points hit</span>
          <span className={`v ${d.penalty_points ? "down" : ""}`}>{d.penalty_points ? `−${d.penalty_points}` : "None"}</span>
        </span>
        <span><span className="k">Captain</span><span className="v">{cap}</span></span>
        <span>
          <span className="k">Money left{d.money_known ? "" : " (est.)"}</span>
          <span className="v">{money(d.budget_after ?? null)}</span>
        </span>
      </span>
    </button>
  );
}
