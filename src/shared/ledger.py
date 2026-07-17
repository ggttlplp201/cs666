"""T+7-aware lot ledger (docs/System-A §5.3).

Every buy creates a lot with unlock_time = buy_ts + trade_lock_days. Nothing
upstream may sell a lot before its unlock; locked inventory counts as
non-liquid capital for exposure math. Marked and realized P&L are tracked
separately (realized only lands after locks clear and lots actually sell).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from shared.schema import Fill

DAY = 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name TEXT NOT NULL,
    qty INTEGER NOT NULL,
    buy_price REAL NOT NULL,
    buy_ts REAL NOT NULL,
    unlock_ts REAL NOT NULL,
    sell_price REAL,
    sell_fee REAL,
    sell_ts REAL,
    buy_order_id TEXT UNIQUE
);
"""


@dataclass(frozen=True)
class Lot:
    lot_id: int
    market_hash_name: str
    qty: int
    buy_price: float
    buy_ts: float
    unlock_ts: float
    sell_price: float | None = None
    sell_fee: float | None = None
    sell_ts: float | None = None
    buy_order_id: str | None = None   # None for lots created by split_lot

    @property
    def is_open(self) -> bool:
        return self.sell_ts is None

    def is_locked(self, now_ts: float) -> bool:
        return self.is_open and now_ts < self.unlock_ts


class Ledger:
    def __init__(self, path: Path | str = ":memory:", trade_lock_days: float = 7.0):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)
        self.trade_lock_days = trade_lock_days

    def record_buy(self, fill: Fill) -> Lot:
        """Idempotent on fill.client_order_id (§6): replaying the same fill —
        e.g. crash-recovery reconciliation — returns the existing lot instead
        of double-counting inventory."""
        existing = self.conn.execute(
            "SELECT lot_id FROM lots WHERE buy_order_id = ?",
            (fill.client_order_id,),
        ).fetchone()
        if existing:
            return self.get(existing[0])
        unlock_ts = fill.ts + self.trade_lock_days * DAY
        cur = self.conn.execute(
            "INSERT INTO lots (market_hash_name, qty, buy_price, buy_ts, unlock_ts,"
            " buy_order_id) VALUES (?,?,?,?,?,?)",
            (fill.market_hash_name, fill.qty, fill.price_cny, fill.ts, unlock_ts,
             fill.client_order_id),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    def record_sell(self, lot_id: int, fill: Fill) -> Lot:
        lot = self.get(lot_id)
        if not lot.is_open:
            raise ValueError(f"lot {lot_id} already sold")
        if fill.ts < lot.unlock_ts:
            raise ValueError(
                f"lot {lot_id} is T+7 locked until {lot.unlock_ts}; sell at {fill.ts} refused"
            )
        if fill.qty != lot.qty:
            raise ValueError("partial lot sells not supported — split lots at buy time")
        self.conn.execute(
            "UPDATE lots SET sell_price = ?, sell_fee = ?, sell_ts = ? WHERE lot_id = ?",
            (fill.price_cny, fill.fee_cny, fill.ts, lot_id),
        )
        self.conn.commit()
        return self.get(lot_id)

    def split_lot(self, lot_id: int, qty: int) -> Lot:
        """Split `qty` units off an open lot into a new lot (same cost basis
        and unlock), enabling scale-out exits into thin books. Returns the
        new qty-sized lot."""
        lot = self.get(lot_id)
        if not lot.is_open:
            raise ValueError(f"lot {lot_id} already sold")
        if not 0 < qty < lot.qty:
            raise ValueError(f"split qty {qty} invalid for lot of {lot.qty}")
        self.conn.execute(
            "UPDATE lots SET qty = ? WHERE lot_id = ?", (lot.qty - qty, lot_id)
        )
        cur = self.conn.execute(
            "INSERT INTO lots (market_hash_name, qty, buy_price, buy_ts, unlock_ts)"
            " VALUES (?,?,?,?,?)",
            (lot.market_hash_name, qty, lot.buy_price, lot.buy_ts, lot.unlock_ts),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    def get(self, lot_id: int) -> Lot:
        row = self.conn.execute(
            "SELECT * FROM lots WHERE lot_id = ?", (lot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no lot {lot_id}")
        return Lot(*row)

    def open_lots(self, item: str | None = None) -> list[Lot]:
        query = "SELECT * FROM lots WHERE sell_ts IS NULL"
        params: tuple = ()
        if item:
            query += " AND market_hash_name = ?"
            params = (item,)
        return [Lot(*r) for r in self.conn.execute(query, params).fetchall()]

    def sellable_lots(self, item: str, now_ts: float) -> list[Lot]:
        return [l for l in self.open_lots(item) if not l.is_locked(now_ts)]

    def position_qty(self, item: str) -> int:
        return sum(l.qty for l in self.open_lots(item))

    def position_cost(self, item: str) -> float:
        return sum(l.qty * l.buy_price for l in self.open_lots(item))

    def locked_capital(self, now_ts: float) -> float:
        """Cost basis of lots still inside their T+7 lock."""
        return sum(
            l.qty * l.buy_price for l in self.open_lots() if l.is_locked(now_ts)
        )

    def deployed_capital(self) -> float:
        return sum(l.qty * l.buy_price for l in self.open_lots())

    def realized_pnl(self) -> float:
        rows = self.conn.execute(
            "SELECT qty, buy_price, sell_price, sell_fee FROM lots"
            " WHERE sell_ts IS NOT NULL"
        ).fetchall()
        return sum(q * (sp - bp) - fee for q, bp, sp, fee in rows)

    def marked_pnl(self, marks: dict[str, float], fee_pct: float) -> float:
        """Unrealized P&L marking open lots at current highest-buy, net of the
        exit fee we would pay — never call a locked position profitable gross."""
        total = 0.0
        for lot in self.open_lots():
            mark = marks.get(lot.market_hash_name)
            if mark is None:
                continue
            total += lot.qty * (mark * (1 - fee_pct) - lot.buy_price)
        return total
