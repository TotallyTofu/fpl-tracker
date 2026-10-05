import type { SquadPlayer } from "../types";
import { POS_SHORT, cost } from "../types";

// A23: accepts both stored LineupPlayer[] and suggested SquadPlayer[]
// (LineupPlayer is structurally a SquadPlayer with an extra bought_cost).
interface Props {
  players: SquadPlayer[];
  selectedId?: number | null;
  onSelect?: (p: SquadPlayer) => void;
  /** Provide these to enable drag & drop between the XI and the bench.
   *  Omitted (e.g. the compact suggestion view) → read-only, no dragging. */
  onMoveToBench?: (playerId: number) => void;
  onMoveToXi?: (playerId: number) => void;
  onBenchReorder?: (draggedId: number, targetId: number) => void;
  compact?: boolean;
}

const BENCH_NUM = ["①", "②", "③", "④"];

function statusBadge(p: SquadPlayer) {
  if (p.status === "u" || p.status === "s" || p.can_select === 0)
    return <span className="badge out">❌</span>;
  if (p.status === "d" || p.chance_of_playing_next_round === 50)
    return <span className="badge warn">⚠️</span>;
  return null;
}

export default function PitchView({
  players,
  selectedId,
  onSelect,
  onMoveToBench,
  onMoveToXi,
  onBenchReorder,
  compact,
}: Props) {
  const draggable = Boolean(onMoveToBench || onMoveToXi);
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

  const readDragId = (e: React.DragEvent) => Number(e.dataTransfer.getData("text/plain")) || 0;

  const card = (p: SquadPlayer, benchIdx?: number) => (
    <div
      key={`${p.player_id}-${benchIdx ?? "xi"}`}
      className={`pcard ${selectedId === p.player_id ? "selected" : ""}`}
      draggable={draggable}
      onDragStart={(e) => {
        e.dataTransfer.setData("text/plain", String(p.player_id));
        e.dataTransfer.effectAllowed = "move";
      }}
      onDrop={
        benchIdx !== undefined && onBenchReorder
          ? (e) => {
              // Drop *on* a bench card: bench player → reorder; starter → demote.
              e.preventDefault();
              e.stopPropagation();
              const id = readDragId(e);
              if (!id || id === p.player_id) return;
              const draggedP = players.find((x) => x.player_id === id);
              if (draggedP?.role === "bench") onBenchReorder(id, p.player_id);
              else onMoveToBench?.(id);
            }
          : undefined
      }
      onDragOver={benchIdx !== undefined && onBenchReorder ? (e) => e.preventDefault() : undefined}
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
        {p.is_captain ? <span className="badge c">C</span> : null}{" "}
        {p.is_vice_captain ? <span className="badge vc">VC</span> : null} {statusBadge(p)}
      </div>
    </div>
  );

  return (
    <div className="pitch">
      <div
        onDragOver={draggable && onMoveToXi ? (e) => e.preventDefault() : undefined}
        onDrop={
          draggable && onMoveToXi
            ? (e) => {
                e.preventDefault();
                const id = readDragId(e);
                if (id) onMoveToXi(id);
              }
            : undefined
        }
      >
        {rows.map((r) => {
          const row = xi.filter((p) => p.element_type === r.pos);
          return (
            <div className="pitch-row" key={r.pos}>
              {row.length === 0 && <span className="muted small">no {r.label}</span>}
              {row.map((p) => card(p))}
            </div>
          );
        })}
      </div>
      <div
        className="pitch-row"
        style={{ borderTop: "1px dashed var(--border)", paddingTop: 8 }}
        onDragOver={draggable && onMoveToBench ? (e) => e.preventDefault() : undefined}
        onDrop={
          draggable && onMoveToBench
            ? (e) => {
                e.preventDefault();
                const id = readDragId(e);
                if (id) onMoveToBench(id);
              }
            : undefined
        }
      >
        {bench.length === 0 && <span className="muted small">bench empty</span>}
        {bench.map((p, i) => card(p, i))}
      </div>
    </div>
  );
}