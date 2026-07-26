"""Regression tests for the adversarial-review findings (2026-07-17)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from shared_b.backtest import exit_side_prices
from shared_b.data import MarketPanel
from shared_b.execution import PaperBroker
from shared_b.ledger import Ledger
from shared_b.schema import Fill, Order, Regime, Side
from system_b.risk import CycleReservations, RiskGate, RiskState

CFG = {
    "meta": {"kill_switch": False},
    "capital": {"total": 100_000, "max_locked_pct": 0.70},
    "position_sizing": {"per_item_allocation_pct": 0.34, "per_item_max_layers": 6,
                        "per_category_max_layers": 6, "volume_relative_k": 0.35},
    "regime_ceilings_pct": {"bull": 0.8, "sideways": 0.5, "bear": 0.3, "weak": 0.2},
    "category_budget_pct": {"mid_tier_primary": 0.20, "other": 0.10},
    "risk_controls": {"blocklist": [], "daily_loss_limit_pct": -0.05,
                      "weekly_loss_limit_pct": -0.10,
                      "reentry_cooldown_after_stop_days": 3},
    "turnover": {"max_entries_per_item_30d": 10},
    "volatility_targeting": {"enabled": False},
    "execution": {"slippage_pct": 0.005},
}


def _check(gate, order, ledger, reserved, category="mid_tier_primary"):
    return gate.check_buy(
        order, day=order.day, regime=Regime.SIDEWAYS, category=category,
        equity=100_000, marks={}, ledger=ledger, avg_daily_volume=10_000,
        garch_vol=0.02, is_add=False, halted=[], reserved=reserved,
    )


def test_same_cycle_orders_cannot_overspend_cash():
    """Finding 1 (critical): N same-cycle approvals must share one cash pool."""
    gate = RiskGate(dict(CFG), RiskState())
    led = Ledger(starting_cash=300.0)
    reserved = CycleReservations()
    total_committed = 0.0
    for i in range(3):
        o = Order(item=f"I{i}", side=Side.BUY, qty=2, limit_price=99.0,
                  day=date(2026, 1, 10))
        d = _check(gate, o, led, reserved)
        if d.approved:
            total_committed += d.qty * 99.0 * 1.005
    assert total_committed <= 300.0 + 1e-9


def test_same_cycle_reservation_applies_to_item_cap():
    gate = RiskGate(dict(CFG), RiskState())
    led = Ledger(starting_cash=1_000_000.0)
    reserved = CycleReservations()
    # per-item cap = 20k * 0.34 = 6.8k -> 68 units @100; two orders same item
    o1 = Order(item="A", side=Side.BUY, qty=60, limit_price=100.0, day=date(2026, 1, 10))
    o2 = Order(item="A", side=Side.BUY, qty=60, limit_price=100.0, day=date(2026, 1, 10))
    d1 = _check(gate, o1, led, reserved)
    d2 = _check(gate, o2, led, reserved)
    assert (d1.qty + (d2.qty if d2.approved else 0)) * 100.0 <= 6_800 * 1.01


def _panel_one_item(ask=100.0, bid=98.0, volume=20, listings=50, bids=30):
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {"sell_price": ask, "buy_price": bid, "listing_count": listings,
         "buy_order_count": bids, "volume": volume, "valid_buy_orders": 5},
        index=idx,
    )
    return MarketPanel(frames={"X": df})


def test_buy_never_fills_above_limit():
    """Finding 2 (major): limit is a hard price bound."""
    panel = _panel_one_item(ask=99.5)
    broker = PaperBroker(panel=panel, slippage_pct=0.005)
    o = Order(item="X", side=Side.BUY, qty=1, limit_price=99.5, day=date(2026, 1, 1))
    broker.place_buy(o)
    fills = broker.settle(date(2026, 1, 2))
    assert len(fills) == 1
    assert fills[0].fill_price <= 99.5 + 1e-12


def test_sell_never_fills_below_limit():
    panel = _panel_one_item(bid=97.52)
    broker = PaperBroker(panel=panel, slippage_pct=0.005)
    o = Order(item="X", side=Side.SELL, qty=1, limit_price=97.51,
              day=date(2026, 1, 1), lot_id="L")
    broker.place_sell(o)
    fills = broker.settle(date(2026, 1, 2))
    assert len(fills) == 1
    assert fills[0].fill_price >= 97.51 - 1e-12


def test_book_capacity_shared_across_same_item_orders():
    """Finding 6 (minor): several exits share ONE day's book, not one each."""
    panel = _panel_one_item(volume=20, bids=100)
    broker = PaperBroker(panel=panel, fill_fraction=0.25)  # 5 units/day capacity
    for i in range(3):
        broker.place_sell(Order(item="X", side=Side.SELL, qty=4, limit_price=90.0,
                                day=date(2026, 1, 1), lot_id=f"L{i}"))
    fills = broker.settle(date(2026, 1, 2))
    assert sum(f.qty for f in fills) <= 5


def test_fully_deployed_ledger_survives_reload():
    """Finding 3 (minor): cash == 0.0 must NOT resurrect starting_cash."""
    led = Ledger(starting_cash=1_000.0)
    o = Order(item="X", side=Side.BUY, qty=10, limit_price=100.0, day=date(2026, 1, 1))
    led.apply_fill(Fill(order=o, fill_day=date(2026, 1, 1), fill_price=100.0,
                        qty=10, fee=0.0))
    assert led.cash == 0.0
    led2 = Ledger.from_dict(led.to_dict())
    assert led2.cash == 0.0


def test_partial_split_prorates_buy_fee():
    """Finding 7 (minor): entry fee splits pro-rata across the lot pieces."""
    led = Ledger(starting_cash=10_000.0)
    o = Order(item="X", side=Side.BUY, qty=4, limit_price=100.0, day=date(2026, 1, 1))
    led.apply_fill(Fill(order=o, fill_day=date(2026, 1, 1), fill_price=100.0,
                        qty=4, fee=8.0))
    lot = led.open_lots("X")[0]
    s = Order(item="X", side=Side.SELL, qty=1, limit_price=100.0,
              day=date(2026, 1, 9), lot_id=lot.lot_id)
    led.apply_fill(Fill(order=s, fill_day=date(2026, 1, 9), fill_price=110.0,
                        qty=1, fee=0.0))
    closed = [l for l in led.lots if not l.open][0]
    rest = led.open_lots("X")[0]
    assert closed.buy_fee == pytest.approx(2.0)
    assert rest.buy_fee == pytest.approx(6.0)
    assert closed.realized_pnl() == pytest.approx(1 * 10.0 - 2.0)


def test_stale_items_marked_at_last_bid_not_cost():
    """Finding 8 (major): delisted items keep their last observed mark."""
    idx = pd.date_range("2026-01-01", periods=5, freq="D")
    df = pd.DataFrame(
        {"sell_price": [100, 100, 100, 60, 50], "buy_price": [98, 98, 98, 58, 48],
         "listing_count": 50, "buy_order_count": 10, "volume": 10,
         "valid_buy_orders": 5},
        index=idx,
    )
    panel = MarketPanel(frames={"X": df})
    view = panel.up_to(pd.Timestamp("2026-01-20"))   # feed stale for 15 days
    assert view.today("X") is None
    marks = exit_side_prices(view)
    assert marks["X"] == pytest.approx(48.0)         # last bid, not missing


def test_agent_same_day_rerun_is_noop(tmp_path, monkeypatch):
    """Finding 4 (major): second invocation on the same day must not double-run."""
    from shared_b.synthetic import generate
    from system_b import agent as agent_mod

    m = generate(n_items=8, n_days=80, seed=3)
    data_dir = tmp_path / "panel"
    m.panel.save(data_dir)
    state_path = tmp_path / "state.json"
    day = m.panel.calendar()[-1].date()
    r1 = agent_mod.run_cycle(data_dir, on_day=day, state_path=state_path)
    assert r1.get("status") != "already_ran"
    r2 = agent_mod.run_cycle(data_dir, on_day=day, state_path=state_path)
    assert r2.get("status") == "already_ran"
    # backward replay refused too
    from datetime import timedelta
    r3 = agent_mod.run_cycle(data_dir, on_day=day - timedelta(days=3), state_path=state_path)
    assert r3.get("status") == "already_ran"
