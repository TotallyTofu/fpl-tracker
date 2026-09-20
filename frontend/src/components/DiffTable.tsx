import type { Diff } from "../types";
import { cost } from "../types";

export default function DiffTable({ diff }: { diff: Diff }) {
  return (
    <div>
      <div className="grid2">
        <div>
          <h2 style={{ marginTop: 0 }}>In ({diff.transfers_in.length})</h2>
          {diff.transfers_in.length === 0 && <div className="muted small">no new players</div>}
          {diff.transfers_in.map((t) => (
            <div key={t.player_id} className="diff-in">
              + {t.web_name} <span className="muted">({cost(t.cost)})</span>
            </div>
          ))}
        </div>
        <div>
          <h2 style={{ marginTop: 0 }}>Out ({diff.transfers_out.length})</h2>
          {diff.transfers_out.length === 0 && <div className="muted small">no sales</div>}
          {diff.transfers_out.map((t) => (
            <div key={t.player_id} className="diff-out">
              − {t.web_name} <span className="muted">(sells {cost(t.sell_value)})</span>
            </div>
          ))}
        </div>
      </div>
      <table style={{ marginTop: 10 }}>
        <tbody>
          <tr>
            <td className="muted">Cost delta</td>
            <td className={diff.cost_delta > 0 ? "diff-in" : diff.cost_delta < 0 ? "diff-out" : ""}>
              {diff.cost_delta > 0 ? "+" : ""}
              {cost(diff.cost_delta)}
            </td>
          </tr>
          <tr>
            <td className="muted">Total after</td>
            <td>{cost(diff.total_cost_after)}</td>
          </tr>
          <tr>
            <td className="muted">Free transfers used</td>
            <td>
              {diff.free_transfers_used} / bank
            </td>
          </tr>
          <tr>
            <td className="muted">Bank after</td>
            <td>{diff.bank_after}</td>
          </tr>
          <tr>
            <td className="muted">Penalty</td>
            <td className={diff.penalty_points > 0 ? "err" : "ok"}>
              {diff.penalty_points > 0 ? `−${diff.penalty_points} pts` : "none"}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}