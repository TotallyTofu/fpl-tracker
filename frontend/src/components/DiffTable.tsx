import type { Diff } from "../types";
import { cost } from "../types";

export default function DiffTable({ diff }: { diff: Diff }) {
  return (
    <div>
      {diff.transfer_cap_exceeded && (
        <div className="err" style={{ marginBottom: 8 }}>
          ⚠ {Math.max(diff.transfers_in.length, diff.transfers_out.length)} transfers exceeds the
          20-transfer hard cap — invalid without a Wildcard/Free Hit
        </div>
      )}
      <div className="grid2">
        <div>
          <h2 style={{ marginTop: 0 }}>In ({diff.transfers_in.length})</h2>
          {diff.transfers_in.length === 0 && <div className="muted small">no new players</div>}
          {diff.transfers_in.map((t) => (
            <div key={t.player_id} className="diff-in">
              + {t.web_name}{" "}
              <span className="muted">
                ({cost(t.cost)}
                {t.ep != null ? `, EP ${t.ep.toFixed(1)}` : ""})
              </span>
            </div>
          ))}
        </div>
        <div>
          <h2 style={{ marginTop: 0 }}>Out ({diff.transfers_out.length})</h2>
          {diff.transfers_out.length === 0 && <div className="muted small">no sales</div>}
          {diff.transfers_out.map((t) => (
            <div key={t.player_id} className="diff-out">
              − {t.web_name}{" "}
              <span className="muted">
                (sells {cost(t.sell_value)}
                {t.ep != null ? `, EP ${t.ep.toFixed(1)}` : ""})
              </span>
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
            <td className="muted">Budget after</td>
            <td>
              {cost(diff.budget_after ?? 1000 - diff.total_cost_after)}
              {diff.budget_after != null &&
                diff.budget_after < 1000 - diff.total_cost_after && (
                  <span className="muted small">
                    {" "}
                    (sell-on fees applied)
                  </span>
                )}
            </td>
          </tr>
          <tr>
            <td className="muted">Free transfers used</td>
            <td>
              {diff.chip_covers
                ? "0 — covered by Wildcard/Free Hit"
                : diff.bank_before != null
                  ? `${diff.free_transfers_used} / ${diff.bank_before}`
                  : String(diff.free_transfers_used)}
            </td>
          </tr>
          <tr>
            <td className="muted">Bank after</td>
            <td>{diff.bank_after}</td>
          </tr>
          <tr>
            <td className="muted">Penalty</td>
            <td className={diff.penalty_points > 0 ? "err" : "ok"}>
              {diff.penalty_points > 0
                ? `−${diff.penalty_points} pts`
                : diff.chip_covers
                  ? "covered by chip"
                  : "none"}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}