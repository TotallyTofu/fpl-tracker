import { api } from "../api";
import { useEffect, useState } from "react";
import type { LineupPlayer } from "../types";
import { POS_SHORT, cost } from "../types";

export default function TeamStrip() {
  const [players, setPlayers] = useState<LineupPlayer[] | null>(null);

  useEffect(() => {
    api
      .listLineups()
      .then((r) => {
        const cur = r.lineups.find((l) => l.is_current) ?? r.lineups[0];
        if (cur) return api.getLineup(cur.id).then((l) => setPlayers(l.players));
        setPlayers([]);
      })
      .catch(() => setPlayers([]));
  }, []);

  if (!players) return <div className="muted small">loading…</div>;
  if (players.length === 0)
    return <div className="muted small">No team yet — add one on the My Team page.</div>;

  const rows: number[] = [1, 2, 3, 4];
  return (
    <div className="pitch" style={{ background: "var(--panel2)" }}>
      {rows.map((pos) => {
        const row = players.filter((p) => p.role === "starter" && p.element_type === pos);
        if (!row.length) return null;
        return (
          <div className="pitch-row" key={pos}>
            {row.map((p) => (
              <div key={p.player_id} className="pcard" style={{ cursor: "default", minWidth: 80 }}>
                <div className="pname">{p.web_name}</div>
                <div className="pmeta">
                  {POS_SHORT[p.element_type]} · {cost(p.now_cost)}
                </div>
                <div className="ptags">
                  {p.is_captain && <span className="badge c">C</span>}{" "}
                  {p.is_vice_captain && <span className="badge vc">VC</span>}
                </div>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}