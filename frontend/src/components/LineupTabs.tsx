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
  lineups,
  activeId,
  onSelect,
  onNew,
  onRename,
  onDuplicate,
  onDelete,
  onSetCurrent,
  onSuggest,
}: Props) {
  const [menuId, setMenuId] = useState<number | null>(null);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");

  const commitRename = () => {
    if (renamingId !== null && draft.trim()) onRename?.(renamingId, draft.trim());
    setRenamingId(null);
  };

  return (
    <div className="tabs">
      {lineups.map((l) => (
        <span
          key={l.id}
          className={`tab ${activeId === l.id ? "active" : ""}`}
          onClick={() => onSelect(l.id)}
          title={l.is_current ? "current lineup" : l.kind === "test" ? "test lineup (sandbox)" : ""}
        >
          {renamingId === l.id ? (
            <input
              className="tab-rename"
              value={draft}
              autoFocus
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") setRenamingId(null);
              }}
            />
          ) : (
            <>
              {l.name}
              {l.is_current ? " ★" : ""}
              {l.kind === "test" ? " ⚗" : ""}
            </>
          )}
          <span
            className="tab-menu-btn"
            onClick={(e) => {
              e.stopPropagation();
              setMenuId(menuId === l.id ? null : l.id);
            }}
          >
            ⋯
          </span>
          {menuId === l.id && (
            <span className="tab-menu" onClick={(e) => e.stopPropagation()}>
              <button
                onClick={() => {
                  setDraft(l.name);
                  setRenamingId(l.id);
                  setMenuId(null);
                }}
              >
                Rename
              </button>
              <button
                onClick={() => {
                  onDuplicate?.(l.id);
                  setMenuId(null);
                }}
              >
                Duplicate as test ⚗
              </button>
              <button
                onClick={() => {
                  onSuggest?.(l.id);
                  setMenuId(null);
                }}
              >
                Run suggestions
              </button>
              <button
                onClick={() => {
                  onSetCurrent?.(l.id);
                  setMenuId(null);
                }}
              >
                Set as current
              </button>
              <button
                className="danger"
                onClick={() => {
                  onDelete?.(l.id);
                  setMenuId(null);
                }}
              >
                Delete
              </button>
            </span>
          )}
        </span>
      ))}
      <span className="tab" onClick={onNew} style={{ border: "1px dashed var(--border)" }}>
        + New
      </span>
    </div>
  );
}