"""Execution layer behind a stable interface (System A/B §6).

Strategy code only ever talks to `ExecutionInterface` (place_buy, place_sell,
get_inventory, get_wallet). `PaperBroker` is the shadow/backtest backend: it
fills orders against the NEXT day's observed market with thin-book caps and
slippage — decisions at day t, fills at t+1, so paper results can't cheat.

A real BUFF backend (official API or session) plugs in behind the same
interface later; credentials come from the environment at runtime only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .data import MarketPanel
from .schema import Fill, Order, Side


class ExecutionInterface:
    def place_buy(self, order: Order) -> None:
        raise NotImplementedError

    def place_sell(self, order: Order) -> None:
        raise NotImplementedError

    def get_inventory(self) -> dict[str, int]:
        raise NotImplementedError

    def get_wallet(self) -> float:
        raise NotImplementedError


@dataclass
class PaperBroker(ExecutionInterface):
    """Fill model (System B §9: 'ignoring fills/fees/lock produces a beautiful,
    fake equity curve'):

    - Buys fill at next day's sell_price (ask) if limit >= it, plus slippage.
    - Sells fill at next day's buy_price (bid) if limit <= it, minus slippage.
    - Fill qty capped at `fill_fraction` of that day's executed volume AND at
      listing/bid depth — you cannot buy volume that didn't trade.
    - Fee charged on the sell side (BUFF ~2.5%).
    """

    panel: MarketPanel
    fee_pct: float = 0.015   # BUFF CS2 sell fee since 2026-04-14 (was 2.5%)
    buy_fee_pct: float = 0.0
    slippage_pct: float = 0.005
    fill_fraction: float = 0.25
    pending: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    _seen_order_ids: set[str] = field(default_factory=set)
    _inventory: dict[str, int] = field(default_factory=dict)
    _wallet: float = 0.0

    def place_buy(self, order: Order) -> None:
        self._enqueue(order, Side.BUY)

    def place_sell(self, order: Order) -> None:
        self._enqueue(order, Side.SELL)

    def _enqueue(self, order: Order, side: Side) -> None:
        if order.client_order_id in self._seen_order_ids:
            return  # idempotency: same client id never double-executes
        if order.qty <= 0:
            return
        order.side = side
        self._seen_order_ids.add(order.client_order_id)
        self.pending.append(order)

    def get_inventory(self) -> dict[str, int]:
        return dict(self._inventory)

    def get_wallet(self) -> float:
        return self._wallet

    # ------------------------------------------------------------------ sim
    def settle(self, fill_day: date) -> list[Fill]:
        """Attempt to fill all pending orders against `fill_day`'s market data.
        Orders that can't fill (no data / limit not reached / zero depth) expire —
        System B re-decides next cycle rather than chasing.

        Book capacity is consumed PER ITEM-DAY across orders: several lots
        exiting one item on the same day share one day's volume/depth, they
        don't each get a fresh book."""
        ts = pd.Timestamp(fill_day)
        done: list[Fill] = []
        capacity: dict[tuple[str, Side], int] = {}
        for order in self.pending:
            df = self.panel.frames.get(order.item)
            if df is None or ts not in df.index:
                continue
            row = df.loc[ts]
            key = (order.item, order.side)
            if key not in capacity:
                volume = int(max(row["volume"], 0))
                by_volume = max(int(volume * self.fill_fraction), 1 if volume > 0 else 0)
                depth_col = "listing_count" if order.side == Side.BUY else "buy_order_count"
                capacity[key] = min(by_volume, int(max(row[depth_col], 0)))
            fill = self._try_fill(order, row, fill_day, max_qty=capacity[key])
            if fill is not None:
                capacity[key] -= fill.qty
                done.append(fill)
        self.pending = []
        self.fills.extend(done)
        for f in done:
            if f.order.side == Side.BUY:
                self._inventory[f.order.item] = self._inventory.get(f.order.item, 0) + f.qty
                self._wallet -= f.qty * f.fill_price + f.fee
            else:
                self._inventory[f.order.item] = self._inventory.get(f.order.item, 0) - f.qty
                self._wallet += f.qty * f.fill_price - f.fee
        return done

    def _try_fill(self, order: Order, row: pd.Series, fill_day: date,
                  max_qty: int) -> Fill | None:
        """Limit semantics are hard bounds: a buy never pays above its limit,
        a sell never receives below its limit — slippage applies inside them."""
        volume = int(max(row["volume"], 0))
        if volume == 0 or max_qty <= 0:
            return None
        if order.side == Side.BUY:
            ask = float(row["sell_price"])
            if order.limit_price < ask:
                return None
            px = min(ask * (1 + self.slippage_pct), order.limit_price)
            qty = min(order.qty, max_qty)
            return Fill(order=order, fill_day=fill_day, fill_price=px, qty=qty,
                        fee=qty * px * self.buy_fee_pct)
        bid = float(row["buy_price"])
        if order.limit_price > bid:
            return None
        px = max(bid * (1 - self.slippage_pct), order.limit_price)
        qty = min(order.qty, max_qty)
        return Fill(order=order, fill_day=fill_day, fill_price=px, qty=qty,
                    fee=qty * px * self.fee_pct)
