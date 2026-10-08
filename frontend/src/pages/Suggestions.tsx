import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api } from "../api";
import PlanCard, { latestRun, planNet } from "../components/PlanCard";
import PlanDetail from "../components/PlanDetail";
import { useSeason } from "../hooks/useSeason";
import type { Lineup, LineupSummary, Suggestion, TeamFixturesResponse } from "../types";
import { CHIP_LABEL, PROFILE_LABEL, fmtTime, money } from "../types";

const ORDER: Record<string, number> = { max_ep: 0, differential: 1, safe: 2 };
const CHIPS = ["bboost", "triple_captain", "wildcard", "freehit"] as const;


export default function Suggestions() {
  const { season } = useSeason();
  const loc = useLocation();
  const preselect = (loc.state as { lineup_id?: number } | null)?.lineup_id;

  const [lineups, setLineups] = useState<LineupSummary[]>([]);
  const [lineupId, setLineupId] = useState<number | "">("");
  const [lineup, setLineup] = useState<Lineup | null>(null);
  const [targetGw, setTargetGw] = useState<number | "">("");
  const [chip, setChip] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cards, setCards] = useState<Suggestion[]>([]);
  const [selected, setSelected] = useState<string>("max_ep");
  const [history, setHistory] = useState<Suggestion[]>([]);
  const [fixtures, setFixtures] = useState<TeamFixturesResponse | null>(null);

  useEffect(() => {
    api.listLineups().then((r) => {
      setLineups(r.lineups);
      setLineupId(preselect ?? r.lineups.find((l) => l.is_current)?.id ?? r.lineups[0]?.id ?? "");
    }).catch((e) => setError(String(e)));
    api.getTeamFixtures(3).then(setFixtures).catch(() => {});
  }, [preselect]);

  useEffect(() => {
    if (season?.next_gw && targetGw === "") setTargetGw(season.next_gw);
  }, [season, targetGw]);

  const showBatch = (batch: Suggestion[]) => {
    setCards(batch);
    const best = [...batch].filter((s) => !s.variant_of).sort((a, b) => planNet(b) - planNet(a))[0];
    setSelected(best?.profile ?? batch[0]?.profile ?? "max_ep");
  };

  const loadLineup = useCallback((lid: number) => {
    api.getLineup(lid).then(setLineup).catch(() => setLineup(null));
    api.listSuggestions(lid).then((r) => {
      setHistory(r.suggestions);
      showBatch(latestRun(r.suggestions));
    }).catch(() => {});
  }, []);
  useEffect(() => {
    if (lineupId !== "") loadLineup(lineupId);
  }, [lineupId, loadLineup]);

  const generate = async () => {
    if (lineupId === "") {
      setError("Save a team first (My team page).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.generateSuggestions({
        lineup_id: lineupId,
        target_gw: targetGw === "" ? undefined : targetGw,
        chip,
      });
      showBatch([...r.suggestions].sort((a, b) => (ORDER[a.profile] ?? 9) - (ORDER[b.profile] ?? 9)));
      api.listSuggestions(lineupId).then((h) => setHistory(h.suggestions)).catch(() => {});
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const gwOptions = useMemo(() => {
    const out: number[] = [];
    if (season?.next_gw) {
      out.push(season.next_gw);
      if (season.events_total && season.next_gw < season.events_total) out.push(season.next_gw + 1);
    }
    return out;
  }, [season]);

  const chipState = (c: string): { ok: boolean; note: string } => {
    const left = lineup?.chips?.[c] ?? 0;
    if (left <= 0) return { ok: false, note: "used" };
    const w = season?.chip_windows.find((x) => x.chip === c && x.playable_next_gw);
    if (!w && targetGw === season?.next_gw) return { ok: false, note: "not this GW" };
    return { ok: true, note: "" };
  };

  const isTest = lineup?.kind === "test";
  const kept = lineup?.players.filter((p) => p.keep) ?? [];
  const sel = cards.find((c) => c.profile === selected) ?? cards[0];
  const best = [...cards].filter((s) => !s.variant_of).sort((a, b) => planNet(b) - planNet(a))[0];
  const stale = cards.length > 0 && cards[0].target_gw !== targetGw;

  return (
    <div className="page">
      <section className="page-head">
        <div>
          <p className="eyebrow">{season?.next_gw ? `Gameweek ${targetGw || season.next_gw}` : "Gameweek"} · {lineup?.name ?? "no team"}</p>
          <h1>Transfer plans</h1>
          <p className="lede">
            {lineup ? <>{lineup.transfer_bank} free transfer{lineup.transfer_bank === 1 ? "" : "s"} · each extra costs 4 points · </> : null}
            {lineup?.money_known
              ? <>money in the bank {money(lineup.bank_money)}</>
              : <>bank not set (<Link to="/team">add it</Link> so plans only spend money you have)</>}
          </p>
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field">
            <label htmlFor="pl-lineup">Team</label>
            <select id="pl-lineup" value={lineupId} onChange={(e) => setLineupId(e.target.value ? Number(e.target.value) : "")}>
              {lineups.length === 0 && <option value="">no saved teams</option>}
              {lineups.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}{l.is_current ? " (current)" : ""}{l.kind === "test" ? " (sandbox)" : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="pl-gw">Gameweek</label>
            <select id="pl-gw" value={targetGw} onChange={(e) => setTargetGw(e.target.value ? Number(e.target.value) : "")}>
              {gwOptions.map((g) => <option key={g} value={g}>GW{g}{g === season?.next_gw ? " (next)" : ""}</option>)}
            </select>
          </div>
          <button type="button" className="lg" onClick={generate} disabled={busy || lineupId === ""}>
            {busy ? <><span className="spinner" /> Working out plans…</> : cards.length ? "Recalculate" : "Make plans"}
          </button>
        </div>
      </section>

      <fieldset className="card" style={{ margin: 0 }}>
        <legend style={{ padding: "0 6px", fontWeight: 700 }}>
          Chip this gameweek <span className="muted" style={{ fontWeight: 400 }}>(one at most)</span>
        </legend>
        <div className="seg" role="group" aria-label="Chip this gameweek">
          <button type="button" aria-pressed={chip === null} onClick={() => setChip(null)}>No chip</button>
          {CHIPS.map((c) => {
            const st = chipState(c);
            return (
              <button key={c} type="button" aria-pressed={chip === c} disabled={!st.ok}
                className={!st.ok ? "used" : ""} onClick={() => setChip(c)}>
                {CHIP_LABEL[c]}{!st.ok && <span className="small">{st.note}</span>}
              </button>
            );
          })}
        </div>
        <p className="help" style={{ marginTop: 10 }}>
          {chip === null
            ? "Plans assume no chip. Having a chip doesn't mean playing it: pick one here to see plans that use it."
            : chip === "wildcard" || chip === "freehit"
              ? `${CHIP_LABEL[chip]}: unlimited free transfers this gameweek${chip === "freehit" ? "; your squad returns next week" : ""}. Press ${cards.length ? "Recalculate" : "Make plans"}.`
              : `${CHIP_LABEL[chip]} is counted in the projections. Press ${cards.length ? "Recalculate" : "Make plans"}.`}
        </p>
        {kept.length > 0 && (
          <p className="help" style={{ marginTop: 6 }}>
            Kept, never sold{chip === "wildcard" || chip === "freehit" ? `, even with ${CHIP_LABEL[chip]}` : ""}:{" "}
            <b>{kept.map((p) => p.web_name).join(", ")}</b>. <Link to="/team">Change in My team</Link>
          </p>
        )}
      </fieldset>

      {error && <div className="alert bad">{error}</div>}
      {isTest && <div className="alert info">Sandbox team: you can make plans, but they can't be marked as done.</div>}
      {stale && <div className="alert warn">These plans are for GW{cards[0].target_gw}. Press Recalculate for GW{targetGw}.</div>}

      {cards.length === 0 && !busy && (
        <div className="card">
          <h2>No plans yet</h2>
          <p className="help">Press <b>Make plans</b> to get three options for {lineup?.name ?? "your team"}: the best projected,
            a differential and a safe plan. Each one stays within your free transfers unless a player has to be replaced.</p>
        </div>
      )}

      {cards.length > 0 && (
        <>
          <div className="plans">
            {cards.map((s) => (
              <PlanCard key={s.id} s={s} best={s.id === best?.id} selected={sel?.id === s.id}
                onSelect={() => setSelected(s.profile)} />
            ))}
          </div>
          <p className="help">Made {fmtTime(cards[0].generated_at)}{cards[0].diff.chip_played ? ` with ${CHIP_LABEL[cards[0].diff.chip_played]}` : ""}.</p>
          {sel && lineup && (
            <PlanDetail key={sel.id} s={sel} current={lineup.players} fixtures={fixtures}
              canApply={!isTest} onApplied={() => lineupId !== "" && loadLineup(lineupId)} />
          )}
        </>
      )}

      {history.length > 0 && (
        <details className="card">
          <summary>Earlier plans ({history.length})</summary>
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table>
              <thead>
                <tr><th>Made</th><th>GW</th><th>Plan</th><th>Chip</th><th className="num">Points after hits</th><th /></tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <td className="small muted">{fmtTime(h.generated_at)}</td>
                    <td>{h.target_gw}</td>
                    <td>{PROFILE_LABEL[h.profile] ?? h.profile}{h.applied_at ? " · done" : ""}</td>
                    <td>{h.diff.chip_played ? CHIP_LABEL[h.diff.chip_played] : "—"}</td>
                    <td className="num">{planNet(h).toFixed(1)}</td>
                    <td>
                      <button type="button" className="sm ghost" onClick={() => {
                        const batch = latestRun(history.filter((x) => x.id <= h.id));
                        setCards(batch);
                        setSelected(h.profile);
                        window.scrollTo({ top: 0, behavior: "smooth" });
                      }}>View</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
