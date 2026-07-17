"""Backtester (docs/System-A §9, System-B §9).

Replays a snapshot series through a strategy callback, executing on the same
PaperBackend + Ledger used in live paper mode, so the backtest cannot drift
from the paper semantics: 2.5% seller fee, T+7 lock, fills capped at
k * volume_24h, buys at ask / sells at bid. A strategy only ever sees data up
to the current snapshot (no look-ahead by construction).

Includes an event-study runner (§9 "event-study validation"): buy on labeled
event dates, exit on the first snapshot after unlock, report net-of-fee
returns per event.

Scope note: strategies run RAW — ctx.buy() models market frictions (fees,
lock, depth) but does NOT apply System A's risk gate (blocklist, regime
ceilings, kill switch). That is intentional for measuring market reactions;
to backtest gated System-A behavior, replay through the full engine
(system_a.runner --replay), which routes every order through the RiskGate.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

from shared.execution import PaperBackend
from shared.ledger import Ledger, Lot
from shared.schema import Fill, Item, Order, OrderSide


class BacktestContext:
    """What a strategy may see and do at one snapshot step."""

    def __init__(self, backtester: "Backtester"):
        self._bt = backtester
        self.now_ts: float = 0.0
        self.market: dict[str, Item] = {}

    @property
    def ledger(self) -> Ledger:
        return self._bt.ledger

    @property
    def wallet(self) -> float:
        return self._bt.backend.get_wallet()

    def history(self, name: str) -> list[Item]:
        """Snapshots for `name` up to and including now — never the future."""
        return self._bt._history.get(name, [])

    def buy(self, name: str, qty: int, limit_price: float | None = None) -> Fill | None:
        item = self.market.get(name)
        if item is None or qty <= 0:
            return None
        limit = limit_price if limit_price is not None else item.buff_lowest_sell_cny
        order = Order(self._bt._next_id("b"), OrderSide.BUY, name, qty, limit)
        fill = self._bt.backend.place_buy(order)
        if fill:
            self._bt.ledger.record_buy(fill)
        return fill

    def sell_lot(self, lot: Lot, limit_price: float | None = None) -> Fill | None:
        """Sell one whole lot. Refuses locked lots and books too thin to take
        the full lot this step (scale out later instead of fantasy fills)."""
        if lot.is_locked(self.now_ts):
            return None
        item = self.market.get(lot.market_hash_name)
        if item is None:
            return None
        depth_cap = max(1, int(self._bt.backend.fill_volume_cap_k * item.buff_volume_24h))
        if lot.qty > depth_cap:
            return None
        limit = limit_price if limit_price is not None else item.buff_highest_buy_cny
        order = Order(
            self._bt._next_id("s"), OrderSide.SELL, lot.market_hash_name, lot.qty, limit
        )
        fill = self._bt.backend.place_sell(order)
        if fill:
            self._bt.ledger.record_sell(lot.lot_id, fill)
        return fill


@dataclass
class BacktestResult:
    start_equity: float = 0.0    # wallet before the first cycle, the fixed base
    equity_curve: list[tuple[float, float]] = field(default_factory=list)  # (ts, equity)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    buys: int = 0
    sells: int = 0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else 0.0

    @property
    def net_return_pct(self) -> float:
        return (
            (self.final_equity - self.start_equity) / self.start_equity
            if self.start_equity
            else 0.0
        )


Strategy = Callable[[BacktestContext], None]


class Backtester:
    def __init__(
        self,
        snapshots: list[list[Item]],
        wallet_cny: float,
        fee_pct: float,
        fill_volume_cap_k: float,
        trade_lock_days: float,
    ):
        self.snapshots = snapshots
        self.fee_pct = fee_pct
        self.backend = PaperBackend(wallet_cny, fee_pct, fill_volume_cap_k)
        self.ledger = Ledger(trade_lock_days=trade_lock_days)
        self._history: dict[str, list[Item]] = {}
        self._ids = itertools.count()

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def run(self, strategy: Strategy) -> BacktestResult:
        result = BacktestResult(start_equity=self.backend.get_wallet())
        ctx = BacktestContext(self)
        for snapshot in self.snapshots:
            market = {i.market_hash_name: i for i in snapshot}
            for item in snapshot:
                self._history.setdefault(item.market_hash_name, []).append(item)
            self.backend.set_market(market)
            ctx.market = market
            ctx.now_ts = snapshot[0].ts if snapshot else ctx.now_ts

            strategy(ctx)

            marks = {
                name: item.buff_highest_buy_cny for name, item in market.items()
            }
            inventory_value = sum(
                lot.qty * marks.get(lot.market_hash_name, lot.buy_price) * (1 - self.fee_pct)
                for lot in self.ledger.open_lots()
            )
            result.equity_curve.append(
                (ctx.now_ts, self.backend.get_wallet() + inventory_value)
            )
        result.realized_pnl = self.ledger.realized_pnl()
        result.fees_paid = sum(
            f.fee_cny for f in self.backend._fills_by_order.values() if f
        )
        fills = [f for f in self.backend._fills_by_order.values() if f]
        result.buys = sum(1 for f in fills if f.side == OrderSide.BUY)
        result.sells = sum(1 for f in fills if f.side == OrderSide.SELL)
        return result


@dataclass(frozen=True)
class EventSpec:
    """A labeled historical update→reaction event for the event study."""
    day: int                     # snapshot index at which the event is detected
    item: str
    direction: str = "bullish"   # only bullish events are tradeable (long-only)


@dataclass
class EventOutcome:
    event: EventSpec
    entry_price: float | None = None
    exit_price: float | None = None
    net_pnl: float | None = None       # after exit fee

    @property
    def traded(self) -> bool:
        return self.net_pnl is not None


def event_study(
    snapshots: list[list[Item]],
    events: list[EventSpec],
    budget_per_event_cny: float,
    fee_pct: float,
    fill_volume_cap_k: float,
    trade_lock_days: float,
) -> list[EventOutcome]:
    """Buy each bullish event at its detection snapshot, exit on the first
    snapshot after the T+7 unlock. Measures whether the reaction survived the
    lock net of fees — the §5.1 durability question."""
    outcomes = {e: EventOutcome(e) for e in events}
    by_day: dict[int, list[EventSpec]] = {}
    for e in events:
        if e.direction == "bullish":
            by_day.setdefault(e.day, []).append(e)
    day_counter = itertools.count()

    def strategy(ctx: BacktestContext) -> None:
        day = next(day_counter)
        for event in by_day.get(day, []):
            item = ctx.market.get(event.item)
            if item is None:
                continue
            qty = int(budget_per_event_cny // item.buff_lowest_sell_cny)
            fill = ctx.buy(event.item, qty)
            if fill:
                outcomes[event].entry_price = fill.price_cny
        for lot in list(ctx.ledger.open_lots()):
            fill = ctx.sell_lot(lot)
            if fill:
                for event, outcome in outcomes.items():
                    if (
                        event.item == fill.market_hash_name
                        and outcome.entry_price is not None
                        and outcome.exit_price is None
                    ):
                        outcome.exit_price = fill.price_cny
                        outcome.net_pnl = (
                            fill.qty * (fill.price_cny - outcome.entry_price)
                            - fill.fee_cny
                        )

    Backtester(
        snapshots,
        wallet_cny=budget_per_event_cny * max(1, len(events)),
        fee_pct=fee_pct,
        fill_volume_cap_k=fill_volume_cap_k,
        trade_lock_days=trade_lock_days,
    ).run(strategy)
    return list(outcomes.values())
