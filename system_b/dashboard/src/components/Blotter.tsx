import { fmtDay, fmtMoney, fmtNum, fmtPct } from "../lib/format";
import type { Trade } from "../types";

/** Trade blotter: every closed lot as a line — what was bought and sold, on
 * which dates, at which prices, and the resulting delta.
 *
 * Two delta columns on purpose. `Δ price` is the gross ask-to-bid move, which is
 * what a price chart shows you. `Δ net` is after the BUFF sell fee and
 * slippage, which is what the book actually received. On this venue the gap
 * between them is the whole story: the round-trip cost floor is ~5.9%, so a
 * trade can show a positive price move and still lose money. */
export function Blotter({ trades, emptyNote }: { trades: Trade[]; emptyNote?: string }) {
  if (trades.length === 0) {
    return <p className="sub">{emptyNote ?? "No closed trades in this run."}</p>;
  }
  // Runs produced before attribution.csv carried the blotter columns have the
  // trades but not the dates/prices. Say so, rather than rendering a table of
  // dashes that looks like a bug.
  // Careful: on an older artifact these keys are ABSENT, so a `!== null` test
  // passes on `undefined` and renders a table full of dashes.
  const hasBlotterFields = trades.some(
    (t) => Boolean(t.buy_day) || Number.isFinite(t.buy_price),
  );
  if (!hasBlotterFields) {
    return (
      <p className="sub">
        This run predates the blotter columns in <code>attribution.csv</code> — it has{" "}
        {trades.length} closed {trades.length === 1 ? "lot" : "lots"}, but no per-lot dates or
        prices. Re-run the backtest to populate them.
      </p>
    );
  }
  const sorted = [...trades].sort((a, b) => (a.buy_day < b.buy_day ? -1 : 1));
  const totalPnl = sorted.reduce((s, t) => s + (Number.isFinite(t.pnl) ? t.pnl : 0), 0);
  const wins = sorted.filter((t) => t.pnl > 0).length;

  return (
    <>
      <p className="sub">
        {sorted.length} closed {sorted.length === 1 ? "lot" : "lots"} · {wins} up /{" "}
        {sorted.length - wins} down · net{" "}
        <strong className={totalPnl < 0 ? "neg" : "pos"}>{fmtMoney(totalPnl)}</strong>
      </p>
      <div className="scroll-x">
        <table className="trades">
          <thead>
            <tr>
              <th>Item</th>
              <th className="num">Qty</th>
              <th>Bought</th>
              <th className="num">Buy ¥</th>
              <th>Sold</th>
              <th className="num">Sell ¥</th>
              <th className="num">Hold</th>
              <th className="num">Δ price</th>
              <th className="num">Δ net</th>
              <th>Exit rule</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((t, i) => (
              <tr key={i}>
                <td>{t.item}</td>
                <td className="num">{t.qty ?? "—"}</td>
                <td>{t.buy_day ? fmtDay(t.buy_day) : "—"}</td>
                <td className="num">{fmtNum(t.buy_price)}</td>
                <td>{t.sell_day ? fmtDay(t.sell_day) : "—"}</td>
                <td className="num">{fmtNum(t.sell_price)}</td>
                <td className="num">{t.hold_days ?? "—"}</td>
                <td
                  className={`num ${
                    t.ret_pct !== null && t.ret_pct < 0 ? "neg" : "pos"
                  }`}
                >
                  {fmtPct(t.ret_pct)}
                </td>
                <td className={`num ${t.pnl < 0 ? "neg" : "pos"}`}>{fmtMoney(t.pnl)}</td>
                <td>{t.exit_reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
