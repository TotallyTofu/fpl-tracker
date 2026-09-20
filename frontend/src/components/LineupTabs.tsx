import type { LineupSummary } from "../types";

interface Props {
  lineups: LineupSummary[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
}

export default function LineupTabs({ lineups, activeId, onSelect, onNew }: Props) {
  return (
    <div className="tabs">
      {lineups.map((l) => (
        <span
          key={l.id}
          className={`tab ${activeId === l.id ? "active" : ""}`}
          onClick={() => onSelect(l.id)}
          title={l.is_current ? "current lineup" : ""}
        >
          {l.name}
          {l.is_current ? " ★" : ""}
        </span>
      ))}
      <span className="tab" onClick={onNew} style={{ border: "1px dashed var(--border)" }}>
        + New
      </span>
    </div>
  );
}