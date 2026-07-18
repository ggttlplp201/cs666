import { fmtMoney, fmtPct } from "../lib/format";
import type { Trade } from "../types";

/** The table view — the always-reachable, non-hover home of every value. */
export function TradesTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) return <p className="sub">No closed trades in this run.</p>;
  return (
    <table className="trades">
      <thead>
        <tr>
          <th>Item</th>
          <th>Entry rule</th>
          <th>Exit reason</th>
          <th className="num">Hold (d)</th>
          <th className="num">Return</th>
          <th className="num">Net P&L</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t, i) => (
          <tr key={i}>
            <td>{t.item}</td>
            <td>{t.entry_rule}</td>
            <td>{t.exit_reason}</td>
            <td className="num">{t.hold_days ?? "—"}</td>
            <td className={`num ${t.ret_pct !== null && t.ret_pct < 0 ? "neg" : "pos"}`}>
              {fmtPct(t.ret_pct)}
            </td>
            <td className={`num ${t.pnl < 0 ? "neg" : "pos"}`}>{fmtMoney(t.pnl)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
