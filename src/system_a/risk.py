"""Risk gate (System A §8) — every buy passes through here last.

All limits come from config; the gate returns an approved quantity (possibly
shrunk) plus the rule name for provenance. A refusal returns qty 0 and the
gate that refused. Discretionary advice is enforced as hard constraints
(Shared §8): kill switch, blocklist, regime suppression, liquidity floors,
momentum-chase layer cap, exit-liquidity sizing, locked-capital ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.configuration import Config
from shared.ledger import Ledger
from shared.schema import Item, Regime, Signal


@dataclass(frozen=True)
class GateResult:
    qty: int
    rule: str      # "approved" or the refusing/shrinking gate

    @property
    def approved(self) -> bool:
        return self.qty > 0


class RiskGate:
    def __init__(self, config: Config, ledger: Ledger):
        self.config = config
        self.ledger = ledger
        self.runtime_blocklist: set[str] = set()
        self._kill_switch_tripped = False

    # --- kill switch -----------------------------------------------------
    @property
    def kill_switch(self) -> bool:
        return bool(self.config.get("meta.kill_switch")) or self._kill_switch_tripped

    def trip_kill_switch(self, reason: str) -> None:
        self._kill_switch_tripped = True
        self._trip_reason = reason

    # --- blocklist (config + runtime pump flags) -------------------------
    def blocklisted(self, name: str) -> bool:
        return (
            name in (self.config.get("risk_controls.blocklist") or [])
            or name in self.runtime_blocklist
        )

    def block(self, name: str) -> None:
        self.runtime_blocklist.add(name)

    # --- the buy gate ----------------------------------------------------
    def check_buy(
        self,
        item: Item,
        regime: Regime,
        signal: Signal,
        requested_qty: int,
        wallet: float,
        now_ts: float,
        baseline_volume_24h: float | None = None,
    ) -> GateResult:
        if self.kill_switch:
            return GateResult(0, "kill_switch")
        if self.blocklisted(item.market_hash_name):
            return GateResult(0, "blocklist")

        # Regime awareness (§8.1a): bear → only durably-structural (Tier 2)
        # events; weak → no reactive buys at all (small-items game is B's).
        if regime == Regime.WEAK:
            return GateResult(0, "regime_weak")
        if regime == Regime.BEAR and signal.tier != 2:
            return GateResult(0, "regime_bear_non_structural")

        # Liquidity floor (§4.1 step 3 / Shared §4.3): must be exitable.
        # On feeds without executed volume (cs2.sh Developer tier) the
        # ≥min_daily_trades filter is NOT computable — never proxy it from
        # listings. Default is to refuse (conservative); flip
        # selection_filters.allow_unknown_volume to trade on depth alone.
        filters = self.config.require("selection_filters")
        if item.buff_volume_24h is None:
            if not filters.get("allow_unknown_volume", False):
                return GateResult(0, "liquidity_unknown_data_gap")
        elif item.buff_volume_24h < filters["min_daily_trades"]:
            return GateResult(0, "liquidity_floor")
        if item.buff_buy_order_count < filters["min_valid_buy_orders"]:
            return GateResult(0, "buy_order_floor")

        # Momentum-chase cap (§8.1 / Shared §5): ≤ N layers of capital per item.
        capital_total = self.config.require("capital.total")
        layers_total = self.config.require("position_sizing.layers_total")
        chase_layers = self.config.require("system_a.momentum_chase_max_layers")
        per_item_cap = capital_total * chase_layers / layers_total
        headroom = per_item_cap - self.ledger.position_cost(item.market_hash_name)
        price = item.buff_lowest_sell_cny
        qty = min(requested_qty, int(headroom // price))
        if qty <= 0:
            return GateResult(0, "position_cap")

        # Exit-liquidity sizing (§5.2): never hold more than you can offload
        # AFTER the lock — size against baseline volume, not an event-spiked
        # print that will have normalized by unlock time.
        k = self.config.require("position_sizing.volume_relative_k")
        sizing_volume = item.buff_volume_24h
        if sizing_volume is None:
            # No executed volume on this feed tier: the only guaranteed exit
            # is the standing bids, so size against bid-side depth instead.
            sizing_volume = item.buff_buy_order_count
        elif baseline_volume_24h is not None:
            sizing_volume = min(sizing_volume, baseline_volume_24h)
        max_units_by_volume = int(k * sizing_volume)
        already_held = self.ledger.position_qty(item.market_hash_name)
        qty = min(qty, max_units_by_volume - already_held)
        if qty <= 0:
            return GateResult(0, "volume_size_cap")

        # Regime deployment ceiling (Shared §2 core table): total deployed
        # capital may not exceed the regime's share of the book.
        ceiling_pct = self.config.require("regime_ceilings_pct")[regime.value]
        deployment_headroom = (
            ceiling_pct * capital_total - self.ledger.deployed_capital()
        )
        qty = min(qty, int(deployment_headroom // price))
        if qty <= 0:
            return GateResult(0, "deployment_ceiling")

        # Locked-capital ceiling (§8.2): T+7 freezes capital; cap the frozen share.
        max_locked = self.config.require("capital.max_locked_pct") * capital_total
        locked_headroom = max_locked - self.ledger.locked_capital(now_ts)
        qty = min(qty, int(locked_headroom // price))
        if qty <= 0:
            return GateResult(0, "locked_capital_cap")

        # Wallet reality.
        qty = min(qty, int(wallet // price))
        if qty <= 0:
            return GateResult(0, "insufficient_wallet")

        return GateResult(qty, "approved")
