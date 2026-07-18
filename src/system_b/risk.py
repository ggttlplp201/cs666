"""Risk gate (System B §8 / Shared §5, §8): every order passes through here
last, and it may veto or shrink — discretionary advice as hard constraints.

Enforced:
- kill switch, daily/weekly loss limits (halt new buys)
- regime deployment ceilings (bull 80% / sideways 50% / bear 30% / weak: small items only)
- layers framework: per-category and per-item caps, baseline dry powder
- volume-relative sizing (max_units = k * avg_daily_volume) — exit-ability
- max simultaneously-locked capital %
- volatility-targeted batch scaling (inverse to GARCH forecast)
- never average down in bear; re-entry cooldown after a stop; turnover cap
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from shared.ledger import Ledger
from shared.schema import Order, Regime, Side


@dataclass
class RiskState:
    """Mutable across cycles: stop events, entries, equity high-water marks."""

    stop_days: dict[str, date] = field(default_factory=dict)       # item -> last stop-loss day
    entry_days: dict[str, list[date]] = field(default_factory=dict)  # item -> buy days (turnover cap)
    equity_by_day: dict[date, float] = field(default_factory=dict)
    halted_until: date | None = None   # loss-limit latch: stays halted, no silent un-latching

    def record_stop(self, item: str, day: date) -> None:
        self.stop_days[item] = day

    def record_entry(self, item: str, day: date) -> None:
        self.entry_days.setdefault(item, []).append(day)

    def entries_in_window(self, item: str, day: date, window_days: int) -> int:
        cutoff = day - timedelta(days=window_days)
        return sum(1 for d in self.entry_days.get(item, []) if d >= cutoff)

    # persisted across paper-runner invocations — without this, loss-limit
    # halts, stop cooldowns, and turnover caps never bind in paper mode
    def to_dict(self) -> dict:
        return {
            "stop_days": {k: v.isoformat() for k, v in self.stop_days.items()},
            "entry_days": {k: [d.isoformat() for d in v] for k, v in self.entry_days.items()},
            "equity_by_day": {k.isoformat(): v for k, v in self.equity_by_day.items()},
            "halted_until": self.halted_until.isoformat() if self.halted_until else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        return cls(
            stop_days={k: date.fromisoformat(v) for k, v in d.get("stop_days", {}).items()},
            entry_days={k: [date.fromisoformat(x) for x in v]
                        for k, v in d.get("entry_days", {}).items()},
            equity_by_day={date.fromisoformat(k): v
                           for k, v in d.get("equity_by_day", {}).items()},
            halted_until=(date.fromisoformat(d["halted_until"])
                          if d.get("halted_until") else None),
        )


@dataclass
class RiskDecision:
    approved: bool
    qty: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class CycleReservations:
    """Capital/exposure claimed by orders already approved THIS cycle.

    check_buy validates against ledger state, which only reflects *settled*
    fills — without reserving, N same-cycle orders each see the full cash/cap
    room and the ledger goes negative when they all fill (double-spend).
    """

    cash: float = 0.0
    deployed: float = 0.0
    locked: float = 0.0
    by_category: dict[str, float] = field(default_factory=dict)
    by_item_value: dict[str, float] = field(default_factory=dict)
    by_item_qty: dict[str, int] = field(default_factory=dict)

    def commit(self, item: str, category: str, qty: int, unit_cost: float) -> None:
        value = qty * unit_cost
        self.cash += value
        self.deployed += value
        self.locked += value
        self.by_category[category] = self.by_category.get(category, 0.0) + value
        self.by_item_value[item] = self.by_item_value.get(item, 0.0) + value
        self.by_item_qty[item] = self.by_item_qty.get(item, 0) + qty


class RiskGate:
    def __init__(self, cfg: dict, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState()

    # ---------------------------------------------------------------- limits
    def trading_halted(self, day: date, equity_now: float) -> list[str]:
        """Kill switch + drawdown limits (Shared §8.4/§8.5). Halts NEW BUYS.

        A tripped loss limit LATCHES for `halt_days_after_loss_limit` days —
        without the latch the halt silently un-latches as the drawdown baseline
        slides, defeating the 'tripped limit halts new buys' rule."""
        reasons = []
        if self.cfg.get("meta", {}).get("kill_switch", False):
            reasons.append("kill_switch")
        rc = self.cfg.get("risk_controls", {})
        if self.state.halted_until is not None and day < self.state.halted_until:
            reasons.append("loss_limit_latch")
        tripped = False
        eqs = self.state.equity_by_day
        if eqs:
            days = sorted(eqs)
            prev = [d for d in days if d < day]
            if prev:
                d1 = prev[-1]
                if eqs[d1] > 0 and equity_now / eqs[d1] - 1 < rc.get("daily_loss_limit_pct", -0.05):
                    reasons.append("daily_loss_limit")
                    tripped = True
                week_ago = [d for d in prev if d <= day - timedelta(days=7)]
                if week_ago:
                    d7 = week_ago[-1]
                    if eqs[d7] > 0 and equity_now / eqs[d7] - 1 < rc.get("weekly_loss_limit_pct", -0.10):
                        reasons.append("weekly_loss_limit")
                        tripped = True
        if tripped:
            halt_days = int(rc.get("halt_days_after_loss_limit", 5))
            until = day + timedelta(days=halt_days)
            if self.state.halted_until is None or until > self.state.halted_until:
                self.state.halted_until = until
        return reasons

    def record_equity(self, day: date, equity: float) -> None:
        self.state.equity_by_day[day] = equity

    # ----------------------------------------------------------------- sizing
    def deployment_ceiling(self, regime: Regime) -> float:
        """Shared §5's baseline 50% deployed IS the sideways ceiling — the knob
        `position_sizing.baseline_deployed_pct` backs it when the regime table
        doesn't override."""
        ceilings = self.cfg.get("regime_ceilings_pct", {})
        baseline = float(self.cfg.get("position_sizing", {}).get("baseline_deployed_pct", 0.5))
        return float(ceilings.get(regime.value, baseline))

    def category_budget(self, category: str, total_capital: float) -> float:
        split = self.cfg.get("category_budget_pct", {})
        default = split.get("other", 0.10)
        return total_capital * float(split.get(category, default))

    def item_allocation(self, category: str, total_capital: float) -> float:
        budget = self.category_budget(category, total_capital)
        per_item = float(self.cfg.get("position_sizing", {}).get("per_item_allocation_pct", 0.34))
        max_layers = float(self.cfg.get("position_sizing", {}).get("per_item_max_layers", 6))
        return min(budget * per_item, budget * max_layers / 10.0)

    def vol_scale(self, garch_vol: float) -> float:
        vt = self.cfg.get("volatility_targeting", {})
        if not vt.get("enabled", True):
            return 1.0
        target = float(vt.get("target_daily_vol", 0.02))
        if garch_vol <= 0:
            return 1.0
        return float(min(1.5, max(0.25, target / garch_vol)))

    # ---------------------------------------------------------------- the gate
    def check_buy(
        self,
        order: Order,
        *,
        day: date,
        regime: Regime,
        category: str,
        equity: float,
        marks: dict[str, float],
        ledger: Ledger,
        avg_daily_volume: float,
        garch_vol: float,
        is_add: bool,
        halted: list[str],
        reserved: CycleReservations | None = None,
    ) -> RiskDecision:
        cfg = self.cfg
        reasons: list[str] = []
        qty = order.qty
        res = reserved if reserved is not None else CycleReservations()

        if halted:
            return RiskDecision(False, 0, halted)

        rc = cfg.get("risk_controls", {})
        if order.item in set(rc.get("blocklist", []) or []):
            return RiskDecision(False, 0, ["blocklisted"])

        # regime restrictions (System B §3c)
        if regime == Regime.BEAR:
            if is_add:
                return RiskDecision(False, 0, ["never_average_down_in_bear"])
            return RiskDecision(False, 0, ["bear_no_new_buys"])
        if regime == Regime.WEAK and category not in ("small_item",):
            return RiskDecision(False, 0, ["weak_regime_small_items_only"])

        # re-entry cooldown after a stop-loss (no revenge-trading) — applies to
        # ADDS too: averaging into an item that just stopped IS revenge-buying
        cool = int(rc.get("reentry_cooldown_after_stop_days",
                          rc.get("revenue_cooldown_after_stop_days", 3)))
        last_stop = self.state.stop_days.get(order.item)
        if last_stop is not None and (day - last_stop).days < cool:
            return RiskDecision(False, 0, ["reentry_cooldown_after_stop"])

        # turnover cap (1-2 trades/month spirit)
        max_entries = int(cfg.get("turnover", {}).get("max_entries_per_item_30d", 4))
        if self.state.entries_in_window(order.item, day, 30) >= max_entries:
            return RiskDecision(False, 0, ["turnover_cap"])

        total_capital = float(cfg.get("capital", {}).get("total", equity))
        deployed = ledger.marked_value(marks) + res.deployed

        # global deployment ceiling by regime
        ceiling = self.deployment_ceiling(regime)
        order_value = qty * order.limit_price
        if equity > 0 and (deployed + order_value) / equity > ceiling:
            room = max(ceiling * equity - deployed, 0.0)
            qty = int(room // order.limit_price)
            reasons.append("shrunk_to_regime_ceiling")

        # max locked capital %
        max_locked = float(cfg.get("capital", {}).get("max_locked_pct", 0.70))
        locked = ledger.locked_value(day, marks) + res.locked
        if equity > 0 and (locked + qty * order.limit_price) / equity > max_locked:
            room = max(max_locked * equity - locked, 0.0)
            qty = min(qty, int(room // order.limit_price))
            reasons.append("shrunk_to_locked_cap")

        # per-category cap (<= 6 layers of category budget)
        cat_budget = self.category_budget(category, total_capital)
        cat_layers = float(cfg.get("position_sizing", {}).get("per_category_max_layers", 6))
        cat_exposure = res.by_category.get(category, 0.0) + sum(
            lot.qty * marks.get(lot.item, lot.buy_price)
            for lot in ledger.open_lots()
            if self._category_of(lot.item) == category
        )
        cat_cap = cat_budget * cat_layers / 10.0
        if cat_exposure + qty * order.limit_price > cat_cap:
            room = max(cat_cap - cat_exposure, 0.0)
            qty = min(qty, int(room // order.limit_price))
            reasons.append("shrunk_to_category_cap")

        # per-item cap
        item_cap = self.item_allocation(category, total_capital)
        item_cost = ledger.position_cost(order.item) + res.by_item_value.get(order.item, 0.0)
        if item_cost + qty * order.limit_price > item_cap:
            room = max(item_cap - item_cost, 0.0)
            qty = min(qty, int(room // order.limit_price))
            reasons.append("shrunk_to_item_cap")

        # volume-relative cap (exit-ability): max UNITS held <= k * ADV
        k = float(cfg.get("position_sizing", {}).get("volume_relative_k", 0.35))
        max_units = int(k * max(avg_daily_volume, 0.0))
        held = ledger.position_qty(order.item) + res.by_item_qty.get(order.item, 0)
        if held + qty > max_units:
            qty = max(max_units - held, 0)
            reasons.append("shrunk_to_volume_cap")

        # volatility targeting: scale DOWN when forecast vol is high
        scale = self.vol_scale(garch_vol)
        if scale < 1.0:
            qty = int(qty * scale)
            reasons.append(f"vol_scaled_{scale:.2f}")

        # cash check (cannot spend locked/inventory value or cash claimed by
        # this cycle's earlier approvals); reserve slippage — fills land at
        # min(ask*(1+slip), limit) so limit already bounds cost, but keep the
        # slip buffer for safety against config drift
        slip = float(cfg.get("execution", {}).get("slippage_pct", 0.005))
        unit_cost = order.limit_price * (1 + slip)
        available = ledger.cash - res.cash
        if qty * unit_cost > available:
            qty = int(max(available, 0.0) // unit_cost)
            reasons.append("shrunk_to_cash")

        if qty <= 0:
            return RiskDecision(False, 0, reasons or ["no_room"])
        res.commit(order.item, category, qty, unit_cost)
        return RiskDecision(True, qty, reasons)

    # category lookup is injected each cycle (metadata lives with the panel)
    _cat_map: dict[str, str] = {}

    def set_categories(self, cat_map: dict[str, str]) -> None:
        self._cat_map = cat_map

    def _category_of(self, item: str) -> str:
        return self._cat_map.get(item, "other")
