"""Hard selection filters (Shared §4.3) + risk gate (System B §8) invariants."""

from datetime import date

import pandas as pd
import pytest

from shared_b.ledger import Ledger
from shared_b.schema import Fill, ItemMeta, Order, Regime, Side
from system_b.filters import hard_filter_reasons
from system_b.risk import RiskGate, RiskState

SEL = {
    "supply_sweet_spot": [2000, 10000],
    "supply_broad": [10000, 30000],
    "supply_hard_exclude_above": 50000,
    "case_price_min_cny": 80,
    "min_valid_buy_orders": 3,
    "min_daily_trades": 10,
}


def _feat(**kw):
    base = {
        "valid_buy_orders": 5, "buy_order_count": 8, "volume_avg_20": 25.0,
        "pump_flag": 0, "attention_late": 0.0,
    }
    base.update(kw)
    s = pd.Series(base)
    s.name = kw.get("name", "ITEM")
    return s


def _meta(**kw):
    base = dict(market_hash_name="ITEM", supply=5000, case_price_cny=100)
    base.update(kw)
    return ItemMeta(**base)


def test_all_gates_pass():
    assert hard_filter_reasons(_feat(), _meta(), SEL, set()) == []


@pytest.mark.parametrize(
    "meta_kw,feat_kw,expected",
    [
        (dict(supply=60000), {}, "supply>50000"),
        (dict(supply=500), {}, "supply_out_of_band"),
        (dict(case_price_cny=20), {}, "case_price<80"),
        ({}, dict(valid_buy_orders=1), "valid_buy_orders<3"),
        ({}, dict(volume_avg_20=4.0), "avg_volume<10"),
        ({}, dict(pump_flag=1), "late_stage_pump_shape"),
        ({}, dict(attention_late=2.0), "late_parabolic_attention"),
    ],
)
def test_each_gate(meta_kw, feat_kw, expected):
    reasons = hard_filter_reasons(_feat(**feat_kw), _meta(**meta_kw), SEL, set())
    assert any(expected in r for r in reasons), reasons


def test_valid_buy_orders_unknown_falls_back_to_bid_count():
    # vendor can't provide validity (cs2.sh) -> -1; falls back to buy_order_count
    f = _feat(valid_buy_orders=-1, buy_order_count=2)
    assert any("buy_orders" in r for r in hard_filter_reasons(f, _meta(), SEL, set()))
    f2 = _feat(valid_buy_orders=-1, buy_order_count=6)
    assert hard_filter_reasons(f2, _meta(), SEL, set()) == []


def test_blocklist():
    f = _feat(name="BAD")
    assert "blocklisted" in hard_filter_reasons(f, _meta(), SEL, {"BAD"})


# --------------------------------------------------------------------- risk
CFG = {
    "meta": {"kill_switch": False},
    "capital": {"total": 100_000, "max_locked_pct": 0.70},
    "position_sizing": {"per_item_allocation_pct": 0.34, "per_item_max_layers": 6,
                        "per_category_max_layers": 6, "volume_relative_k": 0.35},
    "regime_ceilings_pct": {"bull": 0.8, "sideways": 0.5, "bear": 0.3, "weak": 0.2},
    "category_budget_pct": {"mid_tier_primary": 0.20, "small_item": 0.10, "other": 0.10},
    "risk_controls": {"blocklist": [], "daily_loss_limit_pct": -0.05,
                      "weekly_loss_limit_pct": -0.10,
                      "reentry_cooldown_after_stop_days": 3},
    "turnover": {"max_entries_per_item_30d": 4},
    "volatility_targeting": {"enabled": True, "target_daily_vol": 0.02},
}


def _gate():
    return RiskGate(dict(CFG), RiskState())


def _order(item="A", qty=10, px=100.0, day=date(2026, 1, 10)):
    return Order(item=item, side=Side.BUY, qty=qty, limit_price=px, day=day)


def _check(gate, order, ledger, *, regime=Regime.SIDEWAYS, category="mid_tier_primary",
           adv=100.0, vol=0.02, is_add=False, halted=None, equity=100_000.0, marks=None):
    return gate.check_buy(
        order, day=order.day, regime=regime, category=category, equity=equity,
        marks=marks or {}, ledger=ledger, avg_daily_volume=adv, garch_vol=vol,
        is_add=is_add, halted=halted or [],
    )


def test_bear_blocks_all_buys():
    d = _check(_gate(), _order(), Ledger(100_000), regime=Regime.BEAR)
    assert not d.approved and "bear_no_new_buys" in d.reasons
    d2 = _check(_gate(), _order(), Ledger(100_000), regime=Regime.BEAR, is_add=True)
    assert not d2.approved and "never_average_down_in_bear" in d2.reasons


def test_weak_regime_small_items_only():
    d = _check(_gate(), _order(), Ledger(100_000), regime=Regime.WEAK, category="glove")
    assert not d.approved
    d2 = _check(_gate(), _order(qty=2), Ledger(100_000), regime=Regime.WEAK, category="small_item")
    assert d2.approved


def test_volume_relative_cap():
    d = _check(_gate(), _order(qty=50), Ledger(100_000), adv=20.0)
    assert d.approved and d.qty <= int(0.35 * 20)


def test_vol_targeting_shrinks_when_vol_high():
    lo = _check(_gate(), _order(qty=10), Ledger(100_000), vol=0.02)
    hi = _check(_gate(), _order(qty=10), Ledger(100_000), vol=0.08)
    assert hi.qty < lo.qty


def test_reentry_cooldown_after_stop():
    g = _gate()
    g.state.record_stop("A", date(2026, 1, 9))
    d = _check(g, _order(day=date(2026, 1, 10)), Ledger(100_000))
    assert not d.approved and "reentry_cooldown_after_stop" in d.reasons
    d2 = _check(g, _order(day=date(2026, 1, 13)), Ledger(100_000))
    assert d2.approved


def test_turnover_cap():
    g = _gate()
    for i in range(4):
        g.state.record_entry("A", date(2026, 1, 1 + i))
    d = _check(g, _order(day=date(2026, 1, 10)), Ledger(100_000))
    assert not d.approved and "turnover_cap" in d.reasons


def test_halt_blocks():
    d = _check(_gate(), _order(), Ledger(100_000), halted=["kill_switch"])
    assert not d.approved


def test_per_item_cap_shrinks():
    # mid_tier budget 20k, per-item alloc = 20k*0.34 = 6.8k -> 68 units at 100
    d = _check(_gate(), _order(qty=500), Ledger(100_000), adv=10_000)
    assert d.approved and d.qty <= 68


def test_regime_ceiling_caps_deployment():
    led = Ledger(100_000)
    # already deployed ~49k marked -> sideways ceiling 50% leaves ~1k room
    o = Order(item="B", side=Side.BUY, qty=490, limit_price=100.0, day=date(2026, 1, 2))
    led.apply_fill(Fill(order=o, fill_day=date(2026, 1, 2), fill_price=100.0, qty=490, fee=0.0))
    marks = {"B": 100.0}
    d = _check(_gate(), _order(item="C", qty=50), led, marks=marks, adv=10_000,
               equity=led.equity(marks))
    assert d.qty <= 11  # ~1k room / 100
    assert "shrunk_to_regime_ceiling" in d.reasons


def test_daily_loss_limit_halts():
    g = _gate()
    g.record_equity(date(2026, 1, 9), 100_000)
    halted = g.trading_halted(date(2026, 1, 10), 94_000)   # -6% day
    assert "daily_loss_limit" in halted
