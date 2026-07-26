"""Cooldown-aware ledger (System A §5.3, System B §5): lots, locks, layers,
marked vs realized P&L.

Every buy creates a Lot with `unlock_day = buy_day + trade_lock_days`. The
scheduler cannot even consider selling a locked lot, and locked inventory is
non-liquid capital for exposure math. Realized P&L only lands after locks
clear and items actually sell (System B §8.5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from .schema import Fill, Lot, Side, unlock_day_for


@dataclass
class Ledger:
    starting_cash: float
    trade_lock_days: int = 7
    # BUFF releases seller funds only after the 7-day Trade Protection window
    # (BUFF announcement post 2025-07-16; verified 2026-07). Sale proceeds are
    # a receivable until then — they cannot fund new buys.
    settlement_days: int = 7
    # None = fresh ledger (defaults to starting_cash). A sentinel, NOT 0.0 —
    # a fully-deployed persisted ledger legitimately has cash == 0.0 and must
    # not be "refilled" on reload.
    cash: float | None = None
    lots: list[Lot] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    pending_settlements: list[tuple[date, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cash is None:
            self.cash = self.starting_cash

    def settle_cash(self, on_day: date) -> float:
        """Mature sale receivables into spendable cash. Call once per cycle."""
        matured = [amt for d, amt in self.pending_settlements if d <= on_day]
        self.pending_settlements = [(d, a) for d, a in self.pending_settlements if d > on_day]
        for amt in matured:
            self.cash += amt
        return sum(matured)

    def receivables(self) -> float:
        return sum(a for _, a in self.pending_settlements)

    # ------------------------------------------------------------------ fills
    def apply_fill(self, fill: Fill, thesis: str = "", invalidation: str = "") -> Lot | None:
        """Apply a broker fill. Buys create lots; sells close the referenced lot."""
        self.fills.append(fill)
        o = fill.order
        if o.side == Side.BUY:
            cost = fill.qty * fill.fill_price + fill.fee
            self.cash -= cost
            lot = Lot(
                lot_id=uuid.uuid4().hex[:12],
                item=o.item,
                qty=fill.qty,
                buy_price=fill.fill_price,
                buy_fee=fill.fee,
                buy_day=fill.fill_day,
                unlock_day=unlock_day_for(fill.fill_day, self.trade_lock_days),
                batch_index=o.batch_index,
                thesis=thesis,
                invalidation=invalidation,
            )
            self.lots.append(lot)
            return lot
        # SELL — must reference an open, unlocked lot
        lot = self.find_lot(o.lot_id)
        if lot is None or not lot.open:
            raise ValueError(f"sell fill references unknown/closed lot {o.lot_id}")
        if lot.locked(fill.fill_day):
            raise ValueError(f"lot {lot.lot_id} is T+{self.trade_lock_days} locked until {lot.unlock_day}")
        if fill.qty > lot.qty:
            raise ValueError(f"sell qty {fill.qty} > lot qty {lot.qty}")
        if fill.qty < lot.qty:
            # partial exit: split the lot; remainder keeps its lock history
            # and its pro-rata share of the entry fee
            frac_sold = fill.qty / lot.qty
            rest = Lot(
                lot_id=uuid.uuid4().hex[:12],
                item=lot.item,
                qty=lot.qty - fill.qty,
                buy_price=lot.buy_price,
                buy_fee=lot.buy_fee * (1 - frac_sold),
                buy_day=lot.buy_day,
                unlock_day=lot.unlock_day,
                batch_index=lot.batch_index,
                thesis=lot.thesis,
                invalidation=lot.invalidation,
            )
            self.lots.append(rest)
            lot.buy_fee *= frac_sold
            lot.qty = fill.qty
        lot.sell_day = fill.fill_day
        lot.sell_price = fill.fill_price
        lot.sell_fee = fill.fee
        lot.exit_reason = o.reason
        proceeds = fill.qty * fill.fill_price - fill.fee
        if self.settlement_days > 0:
            self.pending_settlements.append(
                (fill.fill_day + timedelta(days=self.settlement_days), proceeds)
            )
        else:
            self.cash += proceeds
        return lot

    def find_lot(self, lot_id: str | None) -> Lot | None:
        if lot_id is None:
            return None
        for lot in self.lots:
            if lot.lot_id == lot_id:
                return lot
        return None

    # --------------------------------------------------------------- queries
    def open_lots(self, item: str | None = None) -> list[Lot]:
        return [l for l in self.lots if l.open and (item is None or l.item == item)]

    def unlocked_lots(self, on_day: date, item: str | None = None) -> list[Lot]:
        return [l for l in self.open_lots(item) if not l.locked(on_day)]

    def position_qty(self, item: str) -> int:
        return sum(l.qty for l in self.open_lots(item))

    def position_cost(self, item: str) -> float:
        return sum(l.cost for l in self.open_lots(item))

    def first_entry_price(self, item: str) -> float | None:
        lots = self.open_lots(item)
        if not lots:
            return None
        return min(lots, key=lambda l: (l.buy_day, l.batch_index)).buy_price

    def last_batch(self, item: str) -> Lot | None:
        lots = self.open_lots(item)
        if not lots:
            return None
        return max(lots, key=lambda l: (l.buy_day, l.batch_index))

    def held_items(self) -> list[str]:
        return sorted({l.item for l in self.open_lots()})

    # ------------------------------------------------------------ valuation
    def marked_value(self, prices: dict[str, float]) -> float:
        """Mark-to-market of open inventory at exit-side (buy/bid) prices, net of sell fee."""
        total = 0.0
        for lot in self.open_lots():
            px = prices.get(lot.item, lot.buy_price)
            total += lot.qty * px
        return total

    def equity(self, prices: dict[str, float], fee_pct: float = 0.0) -> float:
        return self.cash + self.receivables() + self.marked_value(prices) * (1 - fee_pct)

    def locked_value(self, on_day: date, prices: dict[str, float]) -> float:
        total = 0.0
        for lot in self.open_lots():
            if lot.locked(on_day):
                total += lot.qty * prices.get(lot.item, lot.buy_price)
        return total

    def realized_pnl(self) -> float:
        return sum(l.realized_pnl() for l in self.lots if not l.open)

    def exposure_by_category(self, prices: dict[str, float], categories: dict[str, str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for lot in self.open_lots():
            cat = categories.get(lot.item, "other")
            out[cat] = out.get(cat, 0.0) + lot.qty * prices.get(lot.item, lot.buy_price)
        return out

    def deployed_value(self, prices: dict[str, float]) -> float:
        return self.marked_value(prices)

    def deployed_pct(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        return self.marked_value(prices) / eq if eq > 0 else 0.0

    # ---------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        from .schema import to_record

        return {
            "starting_cash": self.starting_cash,
            "trade_lock_days": self.trade_lock_days,
            "settlement_days": self.settlement_days,
            "cash": self.cash,
            "pending_settlements": [(d.isoformat(), a) for d, a in self.pending_settlements],
            "lots": [to_record(l) for l in self.lots],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        led = cls(
            starting_cash=d["starting_cash"],
            trade_lock_days=d.get("trade_lock_days", 7),
            settlement_days=d.get("settlement_days", 7),
            cash=d["cash"],
        )
        led.pending_settlements = [
            (date.fromisoformat(x), a) for x, a in d.get("pending_settlements", [])
        ]
        for ld in d.get("lots", []):
            lot = Lot(
                lot_id=ld["lot_id"], item=ld["item"], qty=ld["qty"],
                buy_price=ld["buy_price"], buy_fee=ld.get("buy_fee", 0.0),
                buy_day=date.fromisoformat(ld["buy_day"]),
                unlock_day=date.fromisoformat(ld["unlock_day"]),
                batch_index=ld.get("batch_index", 0),
                thesis=ld.get("thesis", ""), invalidation=ld.get("invalidation", ""),
                sell_day=date.fromisoformat(ld["sell_day"]) if ld.get("sell_day") else None,
                sell_price=ld.get("sell_price"), sell_fee=ld.get("sell_fee", 0.0),
                exit_reason=ld.get("exit_reason", ""),
            )
            led.lots.append(lot)
        return led
