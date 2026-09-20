import { useSeason } from "../hooks/useSeason";
import { fmtTime } from "../types";

export default function News() {
  const { season } = useSeason();
  return (
    <div>
      <h1>News</h1>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>News pipeline</h2>
        <p className="muted">
          News arrives in <b>M2</b>: BBC Football RSS, r/FantasyPL (RSS), Planet FPL YouTube
          transcripts (Weekender / deadline streams), parsed into player signals by the LLM
          (or rule-based fallback). Those signals then feed the optimizer's availability and
          confidence terms and show up on the Dashboard and player rows.
        </p>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Season card</h2>
        <div className="row">
          <div>
            <div className="big-num">{season?.season ?? "—"}</div>
            <div className="muted">
              GW {season?.current_gw ?? "—"} / {season?.events_total ?? "?"}
            </div>
          </div>
          <div className="muted small">
            {season?.next_gw ? (
              <>
                Next: GW {season.next_gw} — deadline {fmtTime(season.deadline)}
              </>
            ) : (
              "no season data yet"
            )}
          </div>
        </div>
      </div>
    </div>
  );
}