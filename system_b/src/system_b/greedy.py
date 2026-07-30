"""Greedy ratchet strategy — trailing-profit exits, hard stop, watch-and-reenter.

A deliberately simple price-action strategy, specified by Leon (2026-07-30) as a
counterweight to the positional strategy's structural selection:

  1. Buy any item that clears the LIQUIDITY/safety gates (no accumulation
     signals, no structural composite — `safety_only` hard filters).
  2. Once the position is up `arm_pct` (default +10%), the ratchet ARMS and
     tracks a high-water return. If the return then gives back `giveback_pct`
     from that high water (default 1 point — "ran to +15%, fell to +14%, sell"),
     sell the whole position immediately.
  3. If the position is ever down `stop_pct` (default -5%) and NOT yet armed,
     sell the whole position immediately and take no further action on it.
  4. Either way the item goes on a WATCH list at our exit price. If the price
     later dips below that level and comes back up to it, buy in again and run
     the same ratchet. Bounded by `max_reentries` and `reentry_cooldown_days`.

Returns are measured ASK-side (`ask / entry_ask - 1`), matching the positional
strategy so the two are comparable, and matching how a human reads a price
chart. Execution still crosses to the bid.

WHAT THE MEASUREMENTS SAY ABOUT THESE DEFAULTS
----------------------------------------------
`docs/EXIT_COST_FLOOR.md` measured the real panel's round-trip cost floor at
~5.87% (median ask->bid spread 3.37% + 1.5% fee + 1% slippage). Consequences
the literal spec runs into:

- A 1-point giveback is well INSIDE the 3.37% spread, so on a typical item it
  fires on quote noise almost as soon as it arms.
- A -5% ask-side stop realizes about -10.5% net, not -5%.
- A +10% arm exiting at +9% realizes only about +2.7% net.

So the literal spec needs a ~79% win rate merely to break even. `spread_aware`
(default ON) is the improvement: per-item, the giveback floors at
`spread_k * spread_pct` and the stop widens to at least the cost floor, so a
trigger always represents a real price move rather than a trip across the
spread. Set `spread_aware: false` to run the literal spec — `run_backtest
--greedy-literal` does exactly that, so both are measurable side by side.

THE T+7 TRADE LOCK IS THE BINDING CONSTRAINT
--------------------------------------------
BUFF locks a bought item for `cooldown.trade_lock_days` (7). A ratchet cannot
"sell immediately" inside that window. The high-water mark is tracked through
the lock and the exit fires at the first unlocked cycle where the condition
still holds; `lock_blocked_exits` in the cycle journal counts how often the
lock deferred an exit, which is the honest measure of how much of this
strategy's premise survives the venue's rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from shared_b.backtest import exit_side_prices
from shared_b.data import PanelView
from shared_b.journal import Journal
from shared_b.ledger import Ledger
from shared_b.regime import classify_regime
from shared_b.schema import Order, Side
from shared_b.signal_bus import NullBus, SignalBus

from .features import VolCache, build_feature_frame
from .filters import apply_hard_filters
from .risk import CycleReservations, RiskGate, RiskState


# Thresholds in this strategy are specified at exact round numbers ("arms at
# +10%", "give back 1 point from +15% and sell"), but 0.15 - 0.01 is not exactly
# 0.14 in binary floating point, so an exact-boundary trigger would silently
# fail to fire. Every comparison is therefore inclusive to within EPS.
EPS = 1e-9


def ratchet_step(
    ret: float,
    *,
    armed: bool,
    high_water_ret: float,
    arm_pct: float,
    giveback: float,
    stop: float,
) -> tuple[bool, float, str | None]:
    """The whole ratchet, as a pure function of the current ask-side return.

    Returns `(armed, high_water_ret, reason)` where reason is None to hold,
    "greedy_trail_exit", or "greedy_hard_stop".

    The high-water mark always advances first — it must keep rising even on a
    cycle where the lot is T+7 locked and no exit can be issued, otherwise the
    trail would reset its reference every time an exit is deferred.

    Once armed, ONLY the trail can exit. That is deliberate: an armed position
    that collapses trips the trail long before it could reach the hard stop, and
    letting the stop also fire would mean a position that ran to +15% could exit
    at -5% and be recorded as a stop rather than a give-back.
    """
    high_water_ret = max(high_water_ret, ret)
    if not armed and ret >= arm_pct - EPS:
        armed = True
    if armed:
        # The trail must never be able to sit BELOW the hard stop. A wide-spread
        # item under `spread_aware` can get a giveback of 15-20%, and since an
        # armed position is exited only by the trail, such a lot could otherwise
        # fall straight through -stop with nothing firing at all. Clamping the
        # giveback to `high_water_ret + stop` guarantees the trail triggers at
        # -stop in the worst case, while keeping the "armed exits on the trail"
        # semantics (the exit is still reported as a give-back).
        effective_giveback = min(giveback, high_water_ret + stop)
        if ret <= high_water_ret - effective_giveback + EPS:
            return armed, high_water_ret, "greedy_trail_exit"
        return armed, high_water_ret, None
    if ret <= -stop + EPS:
        return armed, high_water_ret, "greedy_hard_stop"
    return armed, high_water_ret, None


@dataclass
class GreedyState:
    """Per-item ratchet + watch state. One position per item (no laddering:
    averaging down contradicts a greedy stop)."""

    # --- held ---
    entry_price: float | None = None   # ask paid at entry; the ratchet's basis
    armed: bool = False
    high_water_ret: float = 0.0        # best ask-side return seen since entry
    # --- exiting (sell emitted, but the fill is not known until it settles) ---
    exit_pending: bool = False
    pending_exit_price: float | None = None
    # --- watch (set once the exit COMPLETES, cleared on re-entry fill) ---
    watch_price: float | None = None   # our exit-decision ask == "our sell price"
    dipped_below: bool = False         # price must leave and RETURN to re-arm
    # --- bookkeeping ---
    reentries: int = 0
    last_exit_day: date | None = None

    def reset_position(self) -> None:
        self.entry_price = None
        self.armed = False
        self.high_water_ret = 0.0


@dataclass
class GreedyStrategy:
    """Implements the same `Strategy` protocol as PositionalStrategy, so it runs
    unchanged under `shared_b.backtest.run_backtest` and live paper mode."""

    cfg: dict
    bus: SignalBus = field(default_factory=NullBus)
    vol_cache: VolCache = field(default_factory=VolCache)
    risk: RiskGate | None = None
    state: dict[str, GreedyState] = field(default_factory=dict)
    last_order_day: dict[str, date] = field(default_factory=dict)
    last_sell_day: dict[str, date] = field(default_factory=dict)
    theses: dict[str, tuple[str, str]] = field(default_factory=dict)
    last_features: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        if self.risk is None:
            self.risk = RiskGate(self.cfg, RiskState())
        g = self.cfg.get("greedy", {}) or {}
        self.arm_pct = float(g.get("arm_pct", 0.10))
        self.giveback_pct = float(g.get("giveback_pct", 0.01))
        self.stop_pct = float(g.get("stop_pct", 0.05))
        self.spread_aware = bool(g.get("spread_aware", True))
        self.spread_k = float(g.get("spread_k", 1.0))
        self.max_reentries = int(g.get("max_reentries", 3))
        self.reentry_cooldown_days = int(g.get("reentry_cooldown_days", 3))
        self.max_new_per_cycle = int(g.get("max_new_positions_per_cycle", 5))
        self.max_positions = int(g.get("max_concurrent_positions", 20))
        self.entry_discount = float(g.get("entry_limit_discount_pct", 0.0))
        # a re-entry must follow a real dip, not a wiggle around our exit price
        self.min_dip_pct = float(g.get("min_dip_pct", 0.01))

    # ---------------------------------------------------------------- helpers
    def _st(self, item: str) -> GreedyState:
        return self.state.setdefault(item, GreedyState())

    def _cost_floor(self, spread_pct: float) -> float:
        """Round-trip cost of a position: spread + sell fee + slippage both ways."""
        fee = float(self.cfg.get("costs", {}).get("buff_fee_pct", 0.015))
        slip = float(self.cfg.get("execution", {}).get("slippage_pct", 0.005))
        return spread_pct + fee + 2 * slip

    def _giveback_for(self, spread_pct: float) -> float:
        if not self.spread_aware:
            return self.giveback_pct
        # a giveback inside the spread is a trip across the book, not a move
        return max(self.giveback_pct, self.spread_k * spread_pct)

    def _stop_for(self, spread_pct: float) -> float:
        if not self.spread_aware:
            return self.stop_pct
        # never crystallize a loss the spread alone could have manufactured
        return max(self.stop_pct, self._cost_floor(spread_pct))

    def _dip_for(self, spread_pct: float) -> float:
        if not self.spread_aware:
            return self.min_dip_pct
        return max(self.min_dip_pct, 0.5 * spread_pct)

    # ------------------------------------------------------------------ cycle
    def on_cycle(self, view: PanelView, ledger: Ledger, journal: Journal) -> list[Order]:
        day = view.day.date()
        orders: list[Order] = []

        active = view.active_items()
        if len(active) < max(3, len(view.items) // 10):
            journal.log("pause", day=day, reason="stale_or_thin_feed", active=len(active))
            return orders

        # regime is NOT a gate here (greedy is pure price action) but the risk
        # gate needs a reading, and the journal should record it for comparison
        regime = classify_regime(view, **self.cfg.get("regime_params", {}))
        as_of = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        bus_signals = self.bus.read(tiers=(2, 3), as_of=as_of)

        features = build_feature_frame(view, regime, bus_signals, self.vol_cache, self.cfg)
        self.last_features = features
        if features.empty:
            return orders

        marks = exit_side_prices(view)
        marks.update({i: float(features.loc[i, "bid"]) for i in features.index})
        fee = float(self.cfg.get("costs", {}).get("buff_fee_pct", 0.015))
        equity = ledger.equity(marks, fee)
        halted = self.risk.trading_halted(day, equity)
        self.risk.record_equity(day, equity)
        self.risk.set_categories({i: (view.meta[i].category if i in view.meta else "other")
                                  for i in features.index})

        stats = {"armed": 0, "lock_blocked_exits": 0, "watching": 0}
        orders += self._exits(view, ledger, journal, features, day, stats)
        if not halted:
            orders += self._entries(view, ledger, journal, features, regime, day,
                                    marks, equity, stats)
        elif halted:
            journal.log("halt", day=day, reasons=halted)

        stats["watching"] = sum(1 for s in self.state.values() if s.watch_price is not None)
        journal.cycle(
            day=day, regime=regime.regime.value, equity=equity, cash=ledger.cash,
            deployed_pct=ledger.marked_value(marks) * (1 - fee) / equity if equity > 0 else 0,
            locked_value=ledger.locked_value(day, marks),
            extra={"n_scoreable": len(features), "greedy": stats},
        )
        return orders

    # ------------------------------------------------------------------ exits
    def _reconcile(self, ledger: Ledger, day: date, journal: Journal) -> None:
        """Settle exit intent against what the ledger actually did.

        A sell can fail to fill or fill only partially (thin book, TTL expiry),
        so the watch list must be armed from the LEDGER going flat, never from
        merely having emitted a sell. Otherwise a no-fill would reset the
        ratchet and restart it from scratch on a position we still hold, and a
        partial exit would leave the remainder untrailed.
        """
        for item, st in self.state.items():
            if not st.exit_pending:
                continue
            if ledger.position_qty(item) > 0:
                continue                      # still holding: keep trailing
            st.watch_price = st.pending_exit_price
            st.dipped_below = False
            st.last_exit_day = day
            st.exit_pending = False
            st.pending_exit_price = None
            st.reset_position()
            journal.decision(day=day, item=item, action="watch_armed",
                             rule="greedy_exit_complete", regime=None, score=None,
                             detail={"watch_price": st.watch_price})

    def _exits(self, view: PanelView, ledger: Ledger, journal: Journal,
               features: pd.DataFrame, day: date, stats: dict) -> list[Order]:
        orders: list[Order] = []
        sell_in_flight = max(1, int(self.cfg.get("execution", {}).get("order_ttl_days", 1)))
        self._reconcile(ledger, day, journal)

        for item in ledger.held_items():
            row = view.today(item)
            if row is None:
                continue
            ask = float(row["sell_price"])
            bid = float(row["buy_price"])
            spread = (ask - bid) / ask if ask > 0 else 0.0
            st = self._st(item)

            # The ratchet's basis is ALWAYS the ledger's actual cost, never the
            # ask we decided on: the fill lands at t+1 at
            # min(next_ask*(1+slip), limit), so the decision-time ask is not
            # what we paid, and a strategy that measured its own return off an
            # unpaid price would arm and trail against a fiction. This also
            # covers a restart, a partial fill, and a lot the ledger created
            # from an order we no longer track.
            open_lots = ledger.open_lots(item)
            if not open_lots:
                continue
            qty_total = sum(lot.qty for lot in open_lots)
            if qty_total <= 0:
                continue
            basis = sum(lot.qty * lot.buy_price for lot in open_lots) / qty_total
            if st.entry_price is None:
                st.entry_price = basis
            elif abs(st.entry_price - basis) > 1e-9:
                # cost basis moved (partial fill settling, or a lot adopted):
                # re-anchor, and rescale the high-water mark so the ratchet does
                # not jump a threshold purely because the denominator changed
                old_hw_price = st.entry_price * (1.0 + st.high_water_ret)
                st.entry_price = basis
                st.high_water_ret = old_hw_price / basis - 1.0

            # A re-entry order has now demonstrably filled, so the watch is
            # spent. Consuming the budget here rather than at order placement
            # means an order that never fills does not silently burn a re-entry.
            if st.watch_price is not None and not st.exit_pending:
                st.reentries += 1
                st.watch_price = None
                st.dipped_below = False

            ret = ask / st.entry_price - 1.0
            giveback = self._giveback_for(spread)
            stop = self._stop_for(spread)
            was_armed = st.armed
            st.armed, st.high_water_ret, reason = ratchet_step(
                ret, armed=st.armed, high_water_ret=st.high_water_ret,
                arm_pct=self.arm_pct, giveback=giveback, stop=stop)
            if st.armed and not was_armed:
                journal.decision(day=day, item=item, action="arm_ratchet",
                                 rule=f"ret>={self.arm_pct:.0%}", regime=None,
                                 score=None, detail={"ret": ret})
            if st.armed:
                stats["armed"] += 1
            if reason is None:
                continue

            sellable = ledger.unlocked_lots(day, item)
            if not sellable:
                stats["lock_blocked_exits"] += 1
                journal.decision(
                    day=day, item=item, action="exit_blocked", rule=reason,
                    regime=None, score=None,
                    detail={"ret": ret, "hw": st.high_water_ret,
                            "note": "T+7 trade lock — all lots still locked"})
                continue

            # Greedy means the WHOLE position leaves at once — no scaling out.
            # `day_sell_cap`-style depth throttling is deliberately omitted; the
            # broker still caps the fill at book depth and fill_fraction, so an
            # oversized exit partially fills and re-fires next cycle.
            sold_lot_ids: set[str] = set()
            for lot in sellable:
                prev = self.last_sell_day.get(lot.lot_id)
                if prev is not None and (day - prev).days <= sell_in_flight:
                    continue  # a sell for this lot is already working
                o = Order(
                    item=item, side=Side.SELL, qty=lot.qty,
                    # price to fill, like the positional stops: the limit is a
                    # floor in the fill model, so this guarantees the exit
                    # rather than costing 5%
                    limit_price=bid * (1 - 0.05),
                    day=day, reason=reason, lot_id=lot.lot_id,
                )
                journal.decision(
                    day=day, item=item, action=f"sell_{lot.qty}", rule=reason,
                    regime=None, score=None,
                    detail={"lot": lot.lot_id, "ret_at_ask": ret,
                            "high_water_ret": st.high_water_ret,
                            "giveback": giveback, "stop": stop,
                            "buy_price": lot.buy_price})
                orders.append(o)
                self.last_sell_day[lot.lot_id] = day
                sold_lot_ids.add(lot.lot_id)

            # Record exit INTENT only. The watch list is armed by `_reconcile`
            # once the ledger actually goes flat, because a sell can fail to
            # fill or fill partially. `pending_exit_price` is the decision-day
            # ASK, deliberately: re-entry compares today's ask against it, so an
            # ask-to-ask comparison is the like-for-like one. Anchoring the
            # watch to the realized bid-side fill instead would sit a whole
            # spread below the market and re-trigger almost immediately.
            if sold_lot_ids:
                st.exit_pending = True
                st.pending_exit_price = ask
        return orders

    # ---------------------------------------------------------------- entries
    def _entries(self, view: PanelView, ledger: Ledger, journal: Journal,
                 features: pd.DataFrame, regime, day: date, marks: dict,
                 equity: float, stats: dict) -> list[Order]:
        cfg = self.cfg
        sel = cfg.get("selection_filters", {})
        blocklist = set(cfg.get("risk_controls", {}).get("blocklist", []) or [])

        # LIQUIDITY-ONLY entry gate: the structural gates (supply, case_price,
        # aesthetics) are human-supplied and still placeholders in the 97-item
        # draft universe, so enforcing them would reject the whole universe.
        passing, rejected = apply_hard_filters(
            features, view.meta, sel, blocklist, safety_only=True)
        if passing.empty:
            return []

        # No structural score and no model here, so rank by tradeability: the
        # most liquid item is the one whose ratchet can actually be executed.
        passing = passing.sort_values("volume_avg_20", ascending=False)

        total_capital = float(cfg.get("capital", {}).get("total", equity))
        in_flight = max(1, int(cfg.get("execution", {}).get("order_ttl_days", 1)))
        n_open = len(ledger.held_items())
        new_positions = 0
        reserved = CycleReservations()
        orders: list[Order] = []

        for item, feat in passing.iterrows():
            item = str(item)
            if new_positions >= self.max_new_per_cycle or n_open >= self.max_positions:
                break
            if ledger.position_qty(item) > 0:
                continue  # one position per item; greedy never averages down
            last_od = self.last_order_day.get(item)
            if last_od is not None and (day - last_od).days <= in_flight:
                continue

            st = self._st(item)
            ask = float(feat["price"])
            spread = float(feat["spread_pct"])
            rule = "greedy_new_position"

            if st.watch_price is not None:
                # --- re-entry path: price must dip below our exit, then return -
                if st.reentries >= self.max_reentries:
                    continue
                if st.last_exit_day is not None and \
                        (day - st.last_exit_day).days < self.reentry_cooldown_days:
                    continue
                dip = self._dip_for(spread)
                if ask <= st.watch_price * (1 - dip):
                    st.dipped_below = True
                if not st.dipped_below:
                    continue
                if ask < st.watch_price:
                    continue  # still below; wait for it to come back to us
                rule = "greedy_reentry_at_exit_price"

            order = Order(
                item=item, side=Side.BUY, qty=1,
                limit_price=ask * (1 - self.entry_discount),
                day=day, reason=rule,
            )
            category = view.meta[item].category if item in view.meta else "other"
            alloc = self.risk.item_allocation(category, total_capital)
            order.qty = max(int(alloc // order.limit_price), 0)
            if order.qty <= 0:
                continue

            decision = self.risk.check_buy(
                order, day=day, regime=regime.regime, category=category,
                equity=equity, marks=marks, ledger=ledger,
                avg_daily_volume=float(feat["volume_avg_20"]),
                garch_vol=float(feat["garch_vol"]), is_add=False,
                halted=[], reserved=reserved,
            )
            journal.decision(
                day=day, item=item,
                action=(f"buy_{decision.qty}" if decision.approved else "veto_buy"),
                rule=rule + ("" if decision.approved else ":" + ";".join(decision.reasons)),
                regime=regime.regime.value, score=float(feat["volume_avg_20"]),
                signals={"spread_pct": spread,
                         "giveback": self._giveback_for(spread),
                         "stop": self._stop_for(spread),
                         "risk_adjustments": decision.reasons},
                detail={"limit": order.limit_price,
                        "watch_price": st.watch_price,
                        "reentries": st.reentries})
            if not decision.approved or decision.qty <= 0:
                continue

            order.qty = decision.qty
            # NOTE: do not call reserved.commit here — RiskGate.check_buy already
            # committed this order's claim to `reserved` on approval (risk.py).
            # Committing again double-counts the cash/category/item claim and
            # wrongly shrinks or vetoes later buys in the same cycle.
            self.last_order_day[item] = day
            self.risk.state.record_entry(item, day)
            # Deliberately do NOT set entry_price / consume the watch here. This
            # order may never fill (the limit is bounded by the next day's ask),
            # and until a lot exists there is no position to run a ratchet on.
            # `_exits` anchors the basis to the ledger and spends the watch on
            # the first cycle it actually sees the lot.
            st.armed = False
            st.high_water_ret = 0.0
            self.theses[order.client_order_id] = (
                f"greedy ratchet: arm +{self.arm_pct:.0%}, trail "
                f"{self._giveback_for(spread):.2%}, stop -{self._stop_for(spread):.2%}",
                "trail giveback or hard stop, whichever comes first",
            )
            orders.append(order)
            new_positions += 1
            n_open += 1
        return orders

    # ------------------------------------------------- backtest/live plumbing
    def thesis_for(self, order: Order) -> tuple[str, str]:
        return self.theses.get(order.client_order_id, ("", ""))

    def set_targets(self, targets: pd.DataFrame) -> None:
        """No model to refit — accepted so the backtest driver stays uniform."""
        return None
