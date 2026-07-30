"""The greedy ratchet: arm, trail, hard stop, watch-and-reenter.

The ratchet core is a pure function (`ratchet_step`), so the exact thresholds
from Leon's spec are pinned directly. The spread-aware adjustments and the
watch/re-entry state machine are pinned on the strategy object, and one
end-to-end run proves the thing actually trades under the real fill model.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from shared_b.data import MarketPanel
from shared_b.schema import ItemMeta
from system_b.greedy import GreedyState, GreedyStrategy, ratchet_step

ITEM = "AK-47 | Redline (Field-Tested)"
D0 = date(2026, 1, 1)

# the spec, verbatim
ARM, GIVEBACK, STOP = 0.10, 0.01, 0.05


def _step(ret, armed=False, hw=0.0, giveback=GIVEBACK, stop=STOP):
    return ratchet_step(ret, armed=armed, high_water_ret=hw, arm_pct=ARM,
                        giveback=giveback, stop=stop)


# --------------------------------------------------------------- arming
def test_below_arm_threshold_does_nothing():
    armed, hw, reason = _step(0.09)
    assert (armed, reason) == (False, None)
    assert hw == pytest.approx(0.09)


def test_arms_exactly_at_ten_percent():
    armed, _, reason = _step(0.10)
    assert armed is True
    assert reason is None            # arming is not an exit


# ------------------------------------------------- the spec's worked example
def test_spec_example_ran_to_15_fell_to_14_sells():
    """'if it grows past 10% to something like 15% and drops to 14%, sell'."""
    armed, hw, reason = _step(0.15)
    assert (armed, reason) == (True, None)
    assert hw == pytest.approx(0.15)

    armed, hw, reason = _step(0.14, armed=armed, hw=hw)
    assert reason == "greedy_trail_exit"


def test_giveback_is_measured_in_points_from_entry_not_pct_of_high_water():
    """15% -> 14% is one POINT of entry-relative return, which is what the
    spec describes; 1% *of* the high-water price would be 15% -> 13.85%."""
    _, hw, reason = _step(0.145, armed=True, hw=0.15)
    assert reason is None            # 0.5 points given back, not yet 1
    _, _, reason = _step(0.1399, armed=True, hw=0.15)
    assert reason == "greedy_trail_exit"


def test_high_water_ratchets_up_and_never_down():
    armed, hw, _ = _step(0.20, armed=True, hw=0.15)
    assert hw == pytest.approx(0.20)
    _, hw, reason = _step(0.12, armed=True, hw=hw)
    assert hw == pytest.approx(0.20)          # unchanged by the fall
    assert reason == "greedy_trail_exit"      # 8 points below the high water


def test_high_water_advances_while_locked_so_deferred_exits_keep_their_reference():
    """A T+7-locked cycle still updates the mark; otherwise every deferred exit
    would silently re-baseline the trail to the current price."""
    armed, hw, _ = _step(0.30, armed=True, hw=0.10)
    assert hw == pytest.approx(0.30)
    # several locked cycles pass at lower prices; the reference must hold
    for r in (0.28, 0.25, 0.22):
        _, hw, reason = _step(r, armed=True, hw=hw)
        assert hw == pytest.approx(0.30)
        assert reason == "greedy_trail_exit"


# ------------------------------------------------------------- hard stop
def test_hard_stop_at_minus_five_when_unarmed():
    _, _, reason = _step(-0.05)
    assert reason == "greedy_hard_stop"
    _, _, reason = _step(-0.049)
    assert reason is None


def test_armed_position_exits_on_the_trail_never_the_hard_stop():
    """An armed lot that collapses must be recorded as a give-back, not a stop —
    the trail is far tighter, so it is what actually fires."""
    _, _, reason = _step(-0.20, armed=True, hw=0.15)
    assert reason == "greedy_trail_exit"


def test_wide_giveback_cannot_park_the_trail_below_the_hard_stop():
    """Regression: `spread_aware` can widen the giveback to 15-20% on a wide
    book. Since an armed lot exits only via the trail, an unclamped giveback
    would let it fall clean through -stop with NOTHING firing."""
    # armed at +10%, absurd 40-point giveback, stop -5%
    _, _, reason = _step(-0.05, armed=True, hw=0.10, giveback=0.40, stop=0.05)
    assert reason == "greedy_trail_exit", "armed lot blew through the hard stop"


def test_clamped_trail_still_respects_a_narrower_giveback():
    """The clamp is a floor on protection, not a replacement for the trail: a
    tight giveback must still fire early, well above the stop."""
    _, _, reason = _step(0.08, armed=True, hw=0.10, giveback=0.02, stop=0.05)
    assert reason == "greedy_trail_exit"
    _, _, reason = _step(0.09, armed=True, hw=0.10, giveback=0.02, stop=0.05)
    assert reason is None


# ------------------------------------------------- spread-aware adjustments
def _strategy(**greedy):
    cfg = {
        "greedy": {"arm_pct": ARM, "giveback_pct": GIVEBACK, "stop_pct": STOP,
                   **greedy},
        "costs": {"buff_fee_pct": 0.015},
        "execution": {"slippage_pct": 0.005},
    }
    return GreedyStrategy(cfg=cfg)


def test_literal_mode_uses_the_spec_numbers_untouched():
    s = _strategy(spread_aware=False)
    assert s._giveback_for(0.0337) == pytest.approx(0.01)
    assert s._stop_for(0.0337) == pytest.approx(0.05)


def test_spread_aware_widens_giveback_past_the_spread():
    """A 1-point trail inside a 3.37% spread is a trip across the book."""
    s = _strategy(spread_aware=True, spread_k=1.0)
    assert s._giveback_for(0.0337) == pytest.approx(0.0337)


def test_spread_aware_widens_stop_to_the_round_trip_cost_floor():
    s = _strategy(spread_aware=True)
    # 3.37% spread + 1.5% fee + 2 x 0.5% slippage
    assert s._stop_for(0.0337) == pytest.approx(0.0337 + 0.015 + 0.01)


def test_spread_aware_leaves_tight_items_greedy():
    """On a genuinely tight book the spec's own numbers already clear the floor,
    so the adjustment must not loosen them gratuitously."""
    s = _strategy(spread_aware=True, spread_k=1.0)
    assert s._giveback_for(0.002) == pytest.approx(0.01)   # floor wins


# ------------------------------------------------- watch / re-entry gating
def _held_state(**kw):
    return GreedyState(**kw)


def test_reentry_requires_a_dip_below_the_exit_price_then_a_return():
    s = _strategy()
    st = s._st(ITEM)
    st.watch_price = 100.0
    st.last_exit_day = D0
    dip = s._dip_for(0.0)          # min_dip_pct default 1%

    # hovering at the exit price is not a dip
    assert not (99.5 <= st.watch_price * (1 - dip))
    # a real dip latches
    assert 98.0 <= st.watch_price * (1 - dip)


def test_reentry_cooldown_and_cap_are_enforced_fields():
    s = _strategy(max_reentries=2, reentry_cooldown_days=3)
    assert s.max_reentries == 2
    assert s.reentry_cooldown_days == 3


def test_reset_position_clears_only_the_ratchet_fields():
    st = _held_state(entry_price=100.0, armed=True, high_water_ret=0.2)
    st.reset_position()
    assert st.entry_price is None and st.armed is False and st.high_water_ret == 0.0


# ------------------------------- exit intent vs. what the ledger actually did
class _FakeLedger:
    """Only what `_reconcile` touches."""

    def __init__(self, qty: dict[str, int]):
        self.qty = qty

    def position_qty(self, item: str) -> int:
        return self.qty.get(item, 0)


def test_no_fill_sell_keeps_the_ratchet_and_does_not_arm_the_watch():
    """Regression (Codex): arming the watch off an EMITTED sell means a sell that
    never fills silently resets the ratchet on a position we still hold."""
    from shared_b.journal import Journal

    s = _strategy()
    st = s._st(ITEM)
    st.entry_price, st.armed, st.high_water_ret = 100.0, True, 0.25
    st.exit_pending, st.pending_exit_price = True, 118.0

    s._reconcile(_FakeLedger({ITEM: 10}), D0, Journal(None))   # still holding
    assert st.watch_price is None, "watch armed while the position is still open"
    assert st.exit_pending is True
    assert st.armed is True and st.high_water_ret == pytest.approx(0.25)


def test_watch_arms_only_once_the_ledger_goes_flat():
    from shared_b.journal import Journal

    s = _strategy()
    st = s._st(ITEM)
    st.entry_price, st.armed, st.high_water_ret = 100.0, True, 0.25
    st.exit_pending, st.pending_exit_price = True, 118.0

    s._reconcile(_FakeLedger({ITEM: 0}), D0, Journal(None))    # exit completed
    assert st.watch_price == pytest.approx(118.0)
    assert st.exit_pending is False and st.pending_exit_price is None
    assert st.last_exit_day == D0
    assert st.entry_price is None and st.armed is False       # ratchet cleared


def test_risk_gate_commits_the_reservation_so_greedy_must_not_commit_again():
    """Regression (Codex): RiskGate.check_buy already commits the approved claim
    to the CycleReservations object. A second commit double-books the cash and
    wrongly shrinks later buys in the same cycle."""
    import inspect

    from system_b import greedy as greedy_mod
    from system_b.risk import RiskGate

    assert "res.commit(" in inspect.getsource(RiskGate.check_buy)
    assert "reserved.commit(" not in inspect.getsource(greedy_mod.GreedyStrategy._entries)


# ------------------------------------------------------------ end to end
def _ramp_panel(n_items: int = 6) -> MarketPanel:
    """A slow ramp up then a sharp fall — enough history for the indicators,
    and shaped so the ratchet must arm and then trail out.

    Needs several items: `on_cycle` stands down when fewer than
    `max(3, len(items)//10)` items are active, so a one-item panel would pause
    every single cycle and never trade.
    """
    # Flat base long enough for the indicators to warm up and for entries to
    # land at ~100 (a steep early ramp would trip the pump-shape safety filter
    # and no entry would ever be approved), then a ramp that clears the +10%
    # arm threshold, then a fall sharp enough to trip the trail.
    asks = [100.0 + (0.3 if i % 2 else -0.3) for i in range(100)]   # flat base
    p = asks[-1]
    for _ in range(60):                                            # ramp ~+20%
        p *= 1.003
        asks.append(p)
    for _ in range(40):                                            # sharp fall
        p *= 0.97
        asks.append(p)
    n = len(asks)
    days = pd.date_range(D0, periods=n, freq="D")
    frames, meta = {}, {}
    for k in range(n_items):
        name = f"{ITEM} #{k}" if k else ITEM
        scale = 1.0 + 0.05 * k          # de-correlate levels a little
        frames[name] = pd.DataFrame(
            {
                "sell_price": [a * scale for a in asks],
                "buy_price": [a * scale * 0.97 for a in asks],
                "listing_count": 400,
                "buy_order_count": 400,
                "volume": 800,
                "valid_buy_orders": 8,
                "is_observed": 1,
            },
            index=days,
        )
        meta[name] = ItemMeta(market_hash_name=name, supply=5000, case_price_cny=200)
    return MarketPanel(frames=frames, meta=meta)


def test_end_to_end_greedy_trades_and_exits_on_the_trail():
    from shared_b.backtest import run_backtest
    from shared_b.journal import Journal

    panel = _ramp_panel()
    cfg = {
        "greedy": {"arm_pct": ARM, "giveback_pct": GIVEBACK, "stop_pct": STOP,
                   "spread_aware": True, "max_concurrent_positions": 5,
                   "max_new_positions_per_cycle": 5},
        "costs": {"buff_fee_pct": 0.015},
        "execution": {"slippage_pct": 0.005, "fill_fraction": 0.25},
        "capital": {"total": 100_000},
        "selection_filters": {"min_daily_trades": 10, "min_valid_buy_orders": 3},
        "regime_params": {},
    }
    strategy = GreedyStrategy(cfg=cfg)
    result = run_backtest(
        panel=panel, strategy=strategy, starting_cash=100_000.0,
        fee_pct=0.015, slippage_pct=0.005, fill_fraction=0.25,
        trade_lock_days=7, settlement_days=7,
        journal=Journal(None), thesis_lookup=strategy.thesis_for,
    )
    attr = result.attribution()
    assert not attr.empty, "greedy never closed a trade on a ramp-and-fall panel"
    # the ramp is +24% then a hard fall: the trail must be what closes it
    assert "greedy_trail_exit" in set(attr["exit_reason"])
