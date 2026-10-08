import type { SquadPlayer, TeamFixturesResponse } from "../types";
import { POS_NAME, ROTATION_RISK, fixtureFor, money, startTitle } from "../types";

// Accepts both stored LineupPlayer[] and suggested SquadPlayer[].
interface Props {
  players: SquadPlayer[];
  selectedId?: number | null;
  onSelect?: (p: SquadPlayer) => void;
  /** Provide these to enable drag & drop between the XI and the bench.
   *  Omitted (the plan view) → read-only, no dragging. */
  onMoveToBench?: (playerId: number) => void;
  onMoveToXi?: (playerId: number) => void;
  onBenchReorder?: (draggedId: number, targetId: number) => void;
  compact?: boolean;
  fixtures?: TeamFixturesResponse | null;
  gw?: number | null;
  /** players to ring as new signings */
  newIds?: Set<number>;
  /** players the plans never sell: greyed, with a lock */
  keptIds?: Set<number>;
}

export const LockIcon = () => (
  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor"
    strokeWidth="2" aria-hidden="true">
    <rect x="3" y="7" width="10" height="7" rx="1.5" />
    <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
  </svg>
);

function flag(p: SquadPlayer): { text: string; out: boolean } | null {
  if (p.status === "u" || p.status === "s" || p.can_select === 0 || p.chance_of_playing_next_round === 0)
    return { text: "OUT", out: true };
  const c = p.chance_of_playing_next_round;
  if (p.status === "d" || p.status === "i" || (c != null && c < 100))
    return { text: c != null ? `${c}%` : "?", out: false };
  return null;
}

/** Rotation-risk chip: only when there is no FPL injury flag (the flag wins). */
function rotationRisk(p: SquadPlayer): { text: string; title: string } | null {
  if (flag(p) || p.p_start == null || p.p_start >= ROTATION_RISK) return null;
  return { text: `~${Math.round(p.p_start * 100)}%`, title: startTitle(p) };
}

const projection = (p: SquadPlayer) => {
  const v = p.ep ?? p.ep_next;
  return v == null ? "—" : v.toFixed(1);
};

const short = (p: SquadPlayer, fx?: TeamFixturesResponse | null) =>
  fx?.teams[String(p.team)]?.short ?? p.team_short ?? (p.team_name ?? "").slice(0, 3).toUpperCase();

export default function PitchView({
  players, selectedId, onSelect, onMoveToBench, onMoveToXi, onBenchReorder,
  compact, fixtures, gw, newIds, keptIds,
}: Props) {
  const draggable = Boolean(onMoveToBench || onMoveToXi);
  const xi = players.filter((p) => p.role === "starter");
  const bench = players
    .filter((p) => p.role === "bench")
    .sort((a, b) => (a.bench_order || 9) - (b.bench_order || 9));
  const gkSub = bench.find((p) => p.element_type === 1) ?? null;
  const outfieldSubs = bench.filter((p) => p !== gkSub);
  const readDragId = (e: React.DragEvent) => Number(e.dataTransfer.getData("text/plain")) || 0;
  const startDrag = (p: SquadPlayer) => (e: React.DragEvent) => {
    e.dataTransfer.setData("text/plain", String(p.player_id));
    e.dataTransfer.effectAllowed = "move";
  };

  const token = (p: SquadPlayer) => {
    const f = flag(p);
    const risk = rotationRisk(p);
    const fx = fixtureFor(fixtures ?? null, p.team, gw);
    const kept = keptIds?.has(p.player_id) ?? false;
    const cls = ["token", newIds?.has(p.player_id) ? "new" : "", kept ? "kept" : "",
      selectedId === p.player_id ? "selected" : ""].join(" ");
    return (
      <button
        type="button"
        key={p.player_id}
        className={cls}
        draggable={draggable}
        onDragStart={draggable ? startDrag(p) : undefined}
        onClick={() => onSelect?.(p)}
        disabled={!onSelect}
        aria-pressed={onSelect ? selectedId === p.player_id : undefined}
        title={`${p.web_name} · ${POS_NAME[p.element_type]} · ${p.team_name ?? ""} · ${money(p.now_cost)}${kept ? " · kept: plans never sell this player" : ""}`}
      >
        <span className="shirt">
          {short(p, fixtures)}
          {kept && <span className="lock-badge" aria-label="Kept"><LockIcon /></span>}
          {p.is_captain ? <span className="armband" aria-label="Captain">C</span> : null}
          {p.is_vice_captain ? <span className="armband vc" aria-label="Vice-captain">V</span> : null}
          {f && <span className={`flag-badge ${f.out ? "out" : ""}`}>{f.text}</span>}
          {risk && <span className="flag-badge risk" title={risk.title}>{risk.text}</span>}
        </span>
        <span className="nameplate">{p.web_name}</span>
        <span className="token-meta">
          {fx.label && <span className={`fdr ${fx.cls}`}>{fx.label}</span>}
          <span className="pts" title="projected points">{projection(p)}</span>
        </span>
      </button>
    );
  };

  const benchCard = (p: SquadPlayer, label: string, isGk: boolean) => {
    const f = flag(p);
    const risk = rotationRisk(p);
    const fx = fixtureFor(fixtures ?? null, p.team, gw);
    const kept = keptIds?.has(p.player_id) ?? false;
    return (
      <div
        key={p.player_id}
        className={`bench-slot ${isGk ? "gk" : ""}`}
        onDragOver={!isGk && onBenchReorder ? (e) => e.preventDefault() : undefined}
        onDrop={
          !isGk && onBenchReorder
            ? (e) => {
                e.preventDefault();
                e.stopPropagation();
                const id = readDragId(e);
                if (!id || id === p.player_id) return;
                const dragged = players.find((x) => x.player_id === id);
                if (dragged?.role === "bench") onBenchReorder(id, p.player_id);
                else onMoveToBench?.(id);
              }
            : undefined
        }
      >
        <span className="bench-label">{label}</span>
        <button
          type="button"
          className={`bench-card ${selectedId === p.player_id ? "selected" : ""} ${newIds?.has(p.player_id) ? "new" : ""} ${kept ? "kept" : ""}`}
          draggable={draggable}
          onDragStart={draggable ? startDrag(p) : undefined}
          onClick={() => onSelect?.(p)}
          disabled={!onSelect}
          aria-pressed={onSelect ? selectedId === p.player_id : undefined}
          title={kept ? `${p.web_name} · kept: plans never sell this player` : undefined}
        >
          <span className="shirt">
            {short(p, fixtures)}
            {kept && <span className="lock-badge" aria-label="Kept"><LockIcon /></span>}
          </span>
          <span>
            <span className="nm">
              {p.web_name} {f && <span className="badge warn">{f.text}</span>}
              {risk && <span className="badge outline" title={risk.title}>{risk.text}</span>}
            </span>
            <br />
            <span className="sub">
              {fx.label ? `${fx.label} · ` : ""}
              {projection(p)}
            </span>
          </span>
        </button>
      </div>
    );
  };

  const rows = [1, 2, 3, 4].map((pos) => xi.filter((p) => p.element_type === pos));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        className={`pitch ${compact ? "compact" : ""}`}
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
        <div className="pitch-lines" />
        <div className="pitch-half" />
        <div className="pitch-circle" />
        <div className="pitch-box top" />
        <div className="pitch-box bottom" />
        <div className="pitch-rows">
          {rows.map((row, i) => (
            <div className="pitch-row" key={i}>
              {row.length === 0 ? (
                <span className="small" style={{ color: "#ffffffaa" }}>no {POS_NAME[i + 1]}</span>
              ) : (
                row.map(token)
              )}
            </div>
          ))}
        </div>
      </div>
      <div
        className="bench"
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
        {bench.length === 0 && <span className="small muted">Bench empty</span>}
        {gkSub && benchCard(gkSub, "GK SUB", true)}
        {outfieldSubs.map((p, i) => benchCard(p, `SUB ${i + 1}`, false))}
      </div>
    </div>
  );
}
