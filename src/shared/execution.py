"""Execution interface + PAPER backend (docs/System-A §6).

Strategy code only ever sees the ExecutionBackend interface; the fragile
BUFF-specific backends (official API / session automation) slot in behind it
later. The paper backend fills against the latest observed book with fills
depth-capped at a fraction of daily volume — entering a thin book at full
size is exactly the fantasy the docs warn about.

Layering note (Shared §12): T+7 lock enforcement lives in the Ledger and the
risk gate, not here — the backend executes what the decision layer already
validated. Idempotency: a client_order_id is honored exactly once.
"""

from __future__ import annotations

from typing import Protocol

from shared.schema import Fill, Item, Order, OrderSide


class ExecutionBackend(Protocol):
    def place_buy(self, order: Order) -> Fill | None: ...
    def place_sell(self, order: Order) -> Fill | None: ...
    def get_inventory(self) -> dict[str, int]: ...
    def get_wallet(self) -> float: ...


class PaperBackend:
    def __init__(self, wallet_cny: float, fee_pct: float, fill_volume_cap_k: float):
        self.wallet = wallet_cny
        self.fee_pct = fee_pct
        self.fill_volume_cap_k = fill_volume_cap_k
        self.inventory: dict[str, int] = {}
        self._market: dict[str, Item] = {}
        self._fills_by_order: dict[str, Fill | None] = {}

    def set_market(self, snapshot: dict[str, Item]) -> None:
        """Called once per cycle with the latest normalized snapshot."""
        self._market = snapshot

    def _depth_cap(self, item: Item) -> int:
        return max(1, int(self.fill_volume_cap_k * item.buff_volume_24h))

    def place_buy(self, order: Order) -> Fill | None:
        if order.client_order_id in self._fills_by_order:
            return self._fills_by_order[order.client_order_id]
        item = self._market.get(order.market_hash_name)
        fill: Fill | None = None
        if item is not None and order.limit_price_cny >= item.buff_lowest_sell_cny:
            qty = min(order.qty, self._depth_cap(item))
            cost = qty * item.buff_lowest_sell_cny
            if qty > 0 and cost <= self.wallet:
                self.wallet -= cost  # buyer pays no fee on BUFF; seller side pays
                self.inventory[item.market_hash_name] = (
                    self.inventory.get(item.market_hash_name, 0) + qty
                )
                fill = Fill(
                    client_order_id=order.client_order_id, side=OrderSide.BUY,
                    market_hash_name=item.market_hash_name, qty=qty,
                    price_cny=item.buff_lowest_sell_cny, fee_cny=0.0, ts=item.ts,
                )
        self._fills_by_order[order.client_order_id] = fill
        return fill

    def place_sell(self, order: Order) -> Fill | None:
        if order.client_order_id in self._fills_by_order:
            return self._fills_by_order[order.client_order_id]
        item = self._market.get(order.market_hash_name)
        held = self.inventory.get(order.market_hash_name, 0)
        fill: Fill | None = None
        if (
            item is not None
            and held > 0
            and order.limit_price_cny <= item.buff_highest_buy_cny
        ):
            qty = min(order.qty, held, self._depth_cap(item))
            if qty > 0:
                gross = qty * item.buff_highest_buy_cny
                fee = gross * self.fee_pct
                self.wallet += gross - fee
                self.inventory[order.market_hash_name] = held - qty
                fill = Fill(
                    client_order_id=order.client_order_id, side=OrderSide.SELL,
                    market_hash_name=order.market_hash_name, qty=qty,
                    price_cny=item.buff_highest_buy_cny, fee_cny=fee, ts=item.ts,
                )
        self._fills_by_order[order.client_order_id] = fill
        return fill

    def get_inventory(self) -> dict[str, int]:
        return {k: v for k, v in self.inventory.items() if v > 0}

    def get_wallet(self) -> float:
        return self.wallet


def reconcile(backend: ExecutionBackend, ledger_positions: dict[str, int]) -> list[str]:
    """Per-cycle reconciliation (§6): backend inventory vs ledger open lots.
    Returns human-readable discrepancies; any entry should pause trading."""
    problems = []
    inventory = backend.get_inventory()
    for item in sorted(set(inventory) | set(ledger_positions)):
        backend_qty = inventory.get(item, 0)
        ledger_qty = ledger_positions.get(item, 0)
        if backend_qty != ledger_qty:
            problems.append(
                f"{item}: backend holds {backend_qty}, ledger says {ledger_qty}"
            )
    return problems
