"""Event-driven daily backtest harness (System B §9).

Honest by construction:
- decisions at day t see only `panel.up_to(t)` (structural no-lookahead),
- orders fill at day t+1 prices via PaperBroker (thin-book caps + slippage + fees),
- T+7 enforced by the Ledger,
- survivorship: the panel includes whatever items existed; items may start/stop.

The same Strategy object runs here and in live paper mode — one code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from .data import MarketPanel, PanelView
from .execution import PaperBroker
from .journal import Journal
from .ledger import Ledger
from .schema import Order


class Strategy(Protocol):
    def on_cycle(self, view: PanelView, ledger: Ledger, journal: Journal) -> list[Order]:
        """Return orders to submit for next-day settlement."""
        ...


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    cash_curve: pd.Series
    deployed_curve: pd.Series
    ledger: Ledger
    journal: Journal
    fills: list = field(default_factory=list)

    # ------------------------------------------------------------- metrics
    def summary(self) -> dict:
        eq = self.equity_curve.dropna()
        if len(eq) < 2:
            return {"error": "not enough data"}
        rets = eq.pct_change().dropna()
        total_ret = eq.iloc[-1] / eq.iloc[0] - 1
        n_days = (eq.index[-1] - eq.index[0]).days or 1
        ann = (1 + total_ret) ** (365 / n_days) - 1
        dd = (eq / eq.cummax() - 1).min()
        vol = rets.std() * np.sqrt(365)
        sharpe = (rets.mean() * 365) / (rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
        closed = [l for l in self.ledger.lots if not l.open]
        wins = [l for l in closed if l.realized_pnl() > 0]
        gross_win = sum(l.realized_pnl() for l in wins)
        gross_loss = -sum(l.realized_pnl() for l in closed if l.realized_pnl() <= 0)
        trade_rets = [l.realized_pnl() / l.cost for l in closed if l.cost > 0]
        holds = [(l.sell_day - l.buy_day).days for l in closed if l.sell_day]
        return {
            # per-trade edge (the go-live gate's primary object: a thinly
            # deployed sim can have great edge and small portfolio return)
            "avg_trade_return_net": float(np.mean(trade_rets)) if trade_rets else None,
            "median_trade_return_net": float(np.median(trade_rets)) if trade_rets else None,
            "median_hold_days": float(np.median(holds)) if holds else None,
            "start": str(eq.index[0].date()),
            "end": str(eq.index[-1].date()),
            "total_return": float(total_ret),
            "annualized_return": float(ann),
            "max_drawdown": float(dd),
            "ann_vol": float(vol),
            "sharpe": float(sharpe),
            "n_trades_closed": len(closed),
            "n_lots_open_at_end": len(self.ledger.open_lots()),
            "win_rate": len(wins) / len(closed) if closed else float("nan"),
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
            "realized_pnl": float(self.ledger.realized_pnl()),
            "final_equity": float(eq.iloc[-1]),
        }

    def attribution(self) -> pd.DataFrame:
        """Per-exit-reason and per-entry-rule realized P&L (Shared §9)."""
        closed = [l for l in self.ledger.lots if not l.open]
        if not closed:
            return pd.DataFrame()
        rows = [
            {
                "item": l.item,
                "entry_rule": l.thesis.split("|")[0] if l.thesis else "",
                "exit_reason": l.exit_reason,
                "hold_days": (l.sell_day - l.buy_day).days if l.sell_day else None,
                "pnl": l.realized_pnl(),
                "ret_pct": (l.sell_price / l.buy_price - 1) if l.sell_price else None,
            }
            for l in closed
        ]
        return pd.DataFrame(rows)


def exit_side_prices(view: PanelView) -> dict[str, float]:
    """Bid-side (buy_price) marks for conservative inventory valuation.

    Stale/delisted items are marked at their LAST OBSERVED bid, not skipped —
    skipping would make Ledger fall back to cost basis, hiding losses from
    equity and the loss-limit halts indefinitely."""
    out = {}
    for item in view.items:
        row = view.today(item)
        if row is not None:
            out[item] = float(row["buy_price"])
        else:
            h = view.history(item)
            if not h.empty:
                out[item] = float(h["buy_price"].iloc[-1])
    return out


def run_backtest(
    panel: MarketPanel,
    strategy: Strategy,
    starting_cash: float,
    fee_pct: float = 0.015,
    slippage_pct: float = 0.005,
    fill_fraction: float = 0.25,
    trade_lock_days: int = 7,
    settlement_days: int = 7,
    start: date | None = None,
    end: date | None = None,
    warmup_days: int = 60,
    journal: Journal | None = None,
    thesis_lookup: Callable[[Order], tuple[str, str]] | None = None,
) -> BacktestResult:
    journal = journal or Journal(None)
    broker = PaperBroker(
        panel=panel,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        fill_fraction=fill_fraction,
    )
    ledger = Ledger(starting_cash=starting_cash, trade_lock_days=trade_lock_days,
                    settlement_days=settlement_days)

    cal = panel.calendar()
    if start:
        cal = cal[cal >= pd.Timestamp(start)]
    if end:
        cal = cal[cal <= pd.Timestamp(end)]
    if len(cal) <= warmup_days:
        raise ValueError("not enough calendar days for warmup")
    cal = cal[warmup_days:]

    equity, cash_c, deployed = {}, {}, {}
    for ts in cal:
        d = ts.date()
        # 0) mature sale receivables (BUFF T+7 settlement) into spendable cash
        ledger.settle_cash(d)
        # 1) settle yesterday's orders at today's prices
        fills = broker.settle(d)
        for f in fills:
            thesis, invalidation = ("", "")
            if thesis_lookup is not None:
                thesis, invalidation = thesis_lookup(f.order)
            lot = ledger.apply_fill(f, thesis=thesis, invalidation=invalidation)
            journal.trade(lot, day=d, note=f"fill {f.order.side.value} {f.qty} @ {f.fill_price:.2f}")

        # 2) decide on today's data (view truncated at today)
        view = panel.up_to(d)
        orders = strategy.on_cycle(view, ledger, journal)
        for o in orders:
            if o.side is None:
                continue
            if o.side.value == "buy":
                broker.place_buy(o)
            else:
                broker.place_sell(o)

        # 3) mark the book at exit-side prices net of sell fee
        marks = exit_side_prices(view)
        eq = ledger.equity(marks, fee_pct)
        equity[ts] = eq
        cash_c[ts] = ledger.cash
        deployed[ts] = ledger.marked_value(marks) * (1 - fee_pct) / eq if eq > 0 else 0.0

    return BacktestResult(
        equity_curve=pd.Series(equity).sort_index(),
        cash_curve=pd.Series(cash_c).sort_index(),
        deployed_curve=pd.Series(deployed).sort_index(),
        ledger=ledger,
        journal=journal,
        fills=broker.fills,
    )
