import { useState } from "react";
import type { LineupSummary } from "../types";

interface Props {
  lineups: LineupSummary[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onRename?: (id: number, name: string) => void;
  onDuplicate?: (id: number) => void;
  onDelete?: (id: number) => void;
  onSetCurrent?: (id: number) => void;
  onSuggest?: (id: number) => void;
}

export default function LineupTabs({
  lineups, activeId, onSelect, onNew, onRename, onDuplicate, onDelete, onSetCurrent, onSuggest,
}: Props) {
  const [menuId, setMenuId] = useState<number | null>(null);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");

  const commitRename = () => {
    if (renamingId !== null && draft.trim()) onRename?.(renamingId, draft.trim());
    setRenamingId(null);
  };
  const act = (fn?: (id: number) => void, id?: number) => {
    if (fn && id !== undefined) fn(id);
    setMenuId(null);
  };

  return (
    <div className="tabs" role="tablist" aria-label="Saved teams">
      {lineups.map((l) => (
        <span key={l.id} className={`tab ${activeId === l.id ? "active" : ""} ${l.kind === "test" ? "test" : ""}`}>
          {renamingId === l.id ? (
            <input
              className="tab-rename"
              value={draft}
              autoFocus
              aria-label="New team name"
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") setRenamingId(null);
              }}
            />
          ) : (
            <button type="button" role="tab" aria-selected={activeId === l.id} onClick={() => onSelect(l.id)}
              style={{ background: "none", border: 0, color: "inherit", padding: 0, minHeight: 0, fontWeight: 600 }}>
              {l.name}
              {l.is_current ? <span className="small" style={{ opacity: 0.75, marginLeft: 6 }}>current</span> : null}
              {l.kind === "test" ? <span className="small" style={{ opacity: 0.75, marginLeft: 6 }}>sandbox</span> : null}
            </button>
          )}
          <button type="button" className="tab-menu-btn" aria-label={`More actions for ${l.name}`}
            aria-expanded={menuId === l.id}
            style={{ background: "none", border: 0, color: "inherit", minHeight: 0, padding: "0 4px" }}
            onClick={() => setMenuId(menuId === l.id ? null : l.id)}>
            ⋯
          </button>
          {menuId === l.id && (
            <span className="tab-menu" role="menu">
              <button type="button" role="menuitem" onClick={() => { setDraft(l.name); setRenamingId(l.id); setMenuId(null); }}>Rename</button>
              <button type="button" role="menuitem" onClick={() => act(onDuplicate, l.id)}>Duplicate as sandbox</button>
              <button type="button" role="menuitem" onClick={() => act(onSuggest, l.id)}>Make transfer plans</button>
              <button type="button" role="menuitem" onClick={() => act(onSetCurrent, l.id)}>Set as my current team</button>
              <button type="button" role="menuitem" className="danger" onClick={() => act(onDelete, l.id)}>Delete</button>
            </span>
          )}
        </span>
      ))}
      <button type="button" className="ghost" onClick={onNew} style={{ borderRadius: 999, borderStyle: "dashed" }}>New team</button>
    </div>
  );
}
