import { useState } from "react";
import { api } from "../api";
import type { LineupPlayer, SquadPlayer, Suggestion, TeamFixturesResponse } from "../types";
import { CHIP_LABEL, PROFILE_LABEL, fmtTime, money, signed } from "../types";
import PitchView from "./PitchView";

export interface Pair {
  out: { name: string; sell: number; ep: number | null } | null;
  inn: { name: string; cost: number; ep: number | null; team: string; pStart: number | null } | null;
}

/** Pair each sale with a purchase of the same position (FPL transfers are
 *  like-for-like); anything left over is listed on its own. */
export function pairTransfers(s: Suggestion, current: LineupPlayer[]): Pair[] {
  const posOf = new Map<number, number>();
  current.forEach((p) => posOf.set(p.player_id, p.element_type));
  s.lineup.squad.forEach((p) => posOf.set(p.player_id, p.element_type));
  const ins = s.diff.transfers_in.map((t) => ({ ...t, pos: posOf.get(t.player_id) ?? 0 }));
  const pairs: Pair[] = [];
  for (const o of s.diff.transfers_out) {
    const pos = posOf.get(o.player_id) ?? 0;
    const i = ins.findIndex((x) => x.pos === pos);
    const inn = i >= 0 ? ins.splice(i, 1)[0] : null;
    const sq = inn ? s.lineup.squad.find((p) => p.player_id === inn.player_id) : undefined;
    pairs.push({
      out: { name: o.web_name, sell: o.sell_value, ep: o.ep ?? null },
      inn: inn ? { name: inn.web_name, cost: inn.cost, ep: inn.ep ?? null, team: sq?.team_name ?? "", pStart: sq?.p_start ?? null } : null,
    });
  }
  for (const inn of ins) {
    const sq = s.lineup.squad.find((p) => p.player_id === inn.player_id);
    pairs.push({ out: null, inn: { name: inn.web_name, cost: inn.cost, ep: inn.ep ?? null, team: sq?.team_name ?? "", pStart: sq?.p_start ?? null } });
  }
  return pairs;
}

function applySteps(s: Suggestion, current: LineupPlayer[], pairs: Pair[]): string[] {
  const name = (id: number) => s.lineup.squad.find((p) => p.player_id === id)?.web_name ?? "?";
  const steps: string[] = [];
  if (pairs.length) {
    steps.push("Transfers: " + pairs.map((p) =>
      p.out && p.inn ? `sell ${p.out.name}, buy ${p.inn.name}` : p.out ? `sell ${p.out.name}` : `buy ${p.inn!.name}`,
    ).join(". ") + ".");
  } else {
    steps.push("Transfers: none.");
  }
  const curXi = new Set(current.filter((p) => p.role === "starter").map((p) => p.player_id));
  const newXi = new Set(s.lineup.xi);
  const start = s.lineup.xi.filter((id) => !curXi.has(id)).map(name);
  const benchNow = [...curXi].filter((id) => !newXi.has(id) && s.lineup.squad.some((p) => p.player_id === id)).map(name);
  if (start.length || benchNow.length) {
    steps.push(`Pick team: ${start.length ? `start ${start.join(", ")}` : ""}${start.length && benchNow.length ? "; " : ""}${benchNow.length ? `bench ${benchNow.join(", ")}` : ""}.`);
  } else {
    steps.push("Pick team: no changes to your starting XI.");
  }
  steps.push(`Captain ${name(s.lineup.captain)}, vice-captain ${name(s.lineup.vice_captain)}.`);
  const benchIds = s.lineup.bench;
  if (benchIds.length) {
    const [gk, ...subs] = benchIds;
    steps.push(`Bench: ${name(gk)} in goal, then ${subs.map(name).join(", ")}.`);
  }
  steps.push(s.diff.chip_played ? `Chip: play ${CHIP_LABEL[s.diff.chip_played] ?? s.diff.chip_played}.` : "Chip: none.");
  return steps;
}

export default function PlanDetail({ s, current, fixtures, canApply, onApplied }: {
  s: Suggestion;
  current: LineupPlayer[];
  fixtures: TeamFixturesResponse | null;
  canApply: boolean;
  onApplied?: () => void;
}) {
  const [applying, setApplying] = useState(false);
  const [applyErr, setApplyErr] = useState<string | null>(null);
  const [appliedAt, setAppliedAt] = useState<string | null>(s.applied_at ?? null);
  const pairs = pairTransfers(s, current);
  const steps = applySteps(s, current, pairs);
  const newIds = new Set(s.diff.transfers_in.map((t) => t.player_id));
  // the keeps this plan was made with (not today's), absent on plans made before the feature
  const keptIds = new Set(s.diff.kept_ids ?? []);
  const blocked = Boolean(s.diff.transfer_cap_exceeded);
  const label = PROFILE_LABEL[s.profile] ?? s.profile;

  const apply = async () => {
    if (!window.confirm("Mark this plan as done in FPL? My Team will be updated to this squad, "
      + "your free transfers and money in the bank will be adjusted"
      + (s.diff.chip_played ? `, and ${CHIP_LABEL[s.diff.chip_played]} will be logged as played.` : "."))) return;
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
    <div className="split">
      <section className="main" aria-labelledby="plan-pitch-h">
        <div className="page-head" style={{ alignItems: "baseline" }}>
          <h2 id="plan-pitch-h" style={{ margin: 0 }}>{label}: your GW{s.target_gw} team</h2>
          <span className="legend">
            <span><span className="dot" /> new signing</span>
            {keptIds.size > 0 && <span><span className="dot kept" /> kept by you</span>}
            <span><span className="badge warn">75%</span> injury flag</span>
            <span>number = projected points</span>
          </span>
        </div>
        <PitchView players={s.lineup.squad as SquadPlayer[]} compact fixtures={fixtures} gw={s.target_gw} newIds={newIds} keptIds={keptIds} />
      </section>

      <div className="side">
        <section className="card" aria-labelledby="tr-h">
          <h2 id="tr-h">Transfers</h2>
          {pairs.length === 0 && <p className="help">No transfers: keep your squad.</p>}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {pairs.map((p, i) => {
              const gain = p.out?.ep != null && p.inn?.ep != null ? p.inn.ep - p.out.ep : null;
              return (
                <div className="move" key={i}>
                  <span className="who">
                    <span className="tag out">OUT</span>
                    <b>{p.out?.name ?? "—"}</b>
                    {p.out && <span className="meta">sells {money(p.out.sell)}{p.out.ep != null ? ` · ${p.out.ep.toFixed(1)} pts` : ""}</span>}
                  </span>
                  <svg width="20" height="20" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M4 11h14M13 6l5 5-5 5" /></svg>
                  <span className="who">
                    <span className="tag in">IN</span>
                    <b>{p.inn?.name ?? "—"}</b>
                    {p.inn && <span className="meta">{money(p.inn.cost)}{p.inn.ep != null ? ` · ${p.inn.ep.toFixed(1)} pts` : ""}{p.inn.pStart != null && p.inn.pStart < 0.8 ? ` · starts ~${Math.round(p.inn.pStart * 100)}%` : ""}</span>}
                  </span>
                  <span className={`gain ${gain != null && gain < 0 ? "neg" : ""}`}>{gain != null ? signed(gain) : ""}</span>
                </div>
              );
            })}
          </div>
          <p className="help" style={{ marginTop: 10 }}>
            {s.diff.chip_covers
              ? `All transfers free (${CHIP_LABEL[s.diff.chip_played ?? ""] ?? "chip"}).`
              : `${s.diff.free_transfers_used} of ${s.diff.bank_before ?? "?"} free transfers used; ${s.diff.bank_after} left this gameweek.`}
            {" "}Money in the bank after: {money(s.diff.budget_after ?? null)}{s.diff.money_known ? "" : " (estimated: add your bank in My Team)"}.
          </p>
        </section>

        <section className="card" aria-labelledby="why-h">
          <h2 id="why-h">Why this plan</h2>
          <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 6 }}>
            {s.rationale.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </section>

        <section className="card" aria-labelledby="chips-h">
          <h2 id="chips-h">Chips</h2>
          <ul className="list" style={{ margin: "0 -20px" }}>
            {s.chip_advice.map((c) => (
              <li key={c.chip} className="list-row" style={{ alignItems: "flex-start" }}>
                <span className={`badge ${c.recommendation}`} style={{ minWidth: 70, justifyContent: "center" }}>{c.recommendation}</span>
                <span style={{ flex: 1, minWidth: 200 }}>
                  <b>{CHIP_LABEL[c.chip] ?? c.chip}</b>
                  <br />
                  <span className="small muted">{c.reason}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section className="card" aria-labelledby="apply-h">
          <h2 id="apply-h">Do it on the FPL site</h2>
          <ol style={{ margin: "0 0 14px", paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6 }}>
            {steps.map((st, i) => <li key={i}>{st}</li>)}
          </ol>
          {appliedAt ? (
            <div className="alert ok">Marked as done {fmtTime(appliedAt)}</div>
          ) : !canApply ? (
            <div className="alert info">Sandbox team: plans can't be applied. Generate from your real team instead.</div>
          ) : blocked ? (
            <div className="alert bad">Over the 20-transfer limit without a Wildcard or Free Hit: not allowed in FPL.</div>
          ) : (
            <button type="button" className="lg" style={{ width: "100%" }} onClick={apply} disabled={applying}>
              {applying ? "Saving…" : "I've made these changes in FPL"}
            </button>
          )}
          {applyErr && <div className="err">{applyErr}</div>}
        </section>
      </div>
    </div>
  );
}
