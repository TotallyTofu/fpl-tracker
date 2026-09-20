import type { LineupPlayer } from "../types";
import { POS_SHORT, cost } from "../types";

interface Props {
  players: LineupPlayer[];
  selectedId?: number | null;
  onSelect?: (p: LineupPlayer) => void;
  compact?: boolean;
}

const BENCH_NUM = ["①", "②", "③", "④"];

function statusBadge(p: LineupPlayer) {
  if (p.status === "u" || p.status === "s" || p.can_select === 0)
    return <span className="badge out">❌</span>;
  if (p.status === "d" || p.chance_of_playing_next_round === 50)
    return <span className="badge warn">⚠️</span>;
  return null;
}

export default function PitchView({ players, selectedId, onSelect, compact }: Props) {
  const xi = players.filter((p) => p.role === "starter");
  const bench = players
    .filter((p) => p.role === "bench")
    .sort((a, b) => (a.bench_order || 0) - (b.bench_order || 0));
  const rows: { pos: number; label: string }[] = [
    { pos: 1, label: "GK" },
    { pos: 2, label: "DEF" },
    { pos: 3, label: "MID" },
    { pos: 4, label: "FWD" },
  ];

  const card = (p: LineupPlayer, benchIdx?: number) => (
    <div
      key={`${p.player_id}-${benchIdx ?? "xi"}`}
      className={`pcard ${selectedId === p.player_id ? "selected" : ""}`}
      onClick={() => onSelect?.(p)}
      title={`${p.web_name} — ${p.team_name ?? ""} ${cost(p.now_cost)}`}
    >
      <div className="pname">
        {benchIdx !== undefined && <span className="muted">{BENCH_NUM[benchIdx]} </span>}
        {p.web_name}
      </div>
      {!compact && (
        <div className="pmeta">
          {POS_SHORT[p.element_type]} · {p.team_name?.slice(0, 3).toUpperCase()} · {cost(p.now_cost)}
        </div>
      )}
      <div className="ptags">
        {p.is_captain && <span className="badge c">C</span>}{" "}
        {p.is_vice_captain && <span className="badge vc">VC</span>} {statusBadge(p)}
      </div>
    </div>
  );

  return (
    <div className="pitch">
      {rows.map((r) => {
        const row = xi.filter((p) => p.element_type === r.pos);
        return (
          <div className="pitch-row" key={r.pos}>
            {row.length === 0 && <span className="muted small">no {r.label}</span>}
            {row.map((p) => card(p))}
          </div>
        );
      })}
      <div className="pitch-row" style={{ borderTop: "1px dashed var(--border)", paddingTop: 8 }}>
        {bench.length === 0 && <span className="muted small">bench empty</span>}
        {bench.map((p, i) => card(p, i))}
      </div>
    </div>
  );
}