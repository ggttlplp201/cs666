"""Entry-lag decay maths for the trade-up event class.

The question these back: how much of a trade-up event's move survives arriving
late? The answer decides whether the class needs a speed race we cannot win, or
just a monitor that notices within days.
"""

from __future__ import annotations

from dataclasses import dataclass

from system_a.event_study import DAY
from system_a.trade_up_lag import FEE, HOLD_DAYS, lagged_return

EVENT_TS = 1_761_091_200.0  # 2025-10-22


@dataclass
class Bar:
    ts: float
    buff_lowest_sell_cny: float


def _series(prices: dict[int, float]) -> list[Bar]:
    """{day offset from event: ask price} -> the store's series shape."""
    return [Bar(EVENT_TS + d * DAY, p) for d, p in sorted(prices.items())]


def test_return_is_net_of_spread_and_fee():
    """Buy at ask+half-spread, sell at ask-half-spread, pay the fee — a flat
    price must therefore LOSE money, not break even."""
    s = _series({0: 100.0, HOLD_DAYS: 100.0})
    r = lagged_return(s, EVENT_TS, 0, spread=0.04)
    assert r is not None and r < 0
    expected = (100 * 0.98 * (1 - FEE)) / (100 * 1.02) - 1
    assert abs(r - expected) < 1e-9


def test_a_doubling_survives_costs():
    s = _series({0: 100.0, HOLD_DAYS: 200.0})
    r = lagged_return(s, EVENT_TS, 0, spread=0.04)
    assert r is not None and r > 0.80


def test_lag_entry_buys_at_the_lagged_price_not_the_event_price():
    """The whole point: entering late must pay the POST-spike price."""
    s = _series({0: 100.0, 7: 300.0, HOLD_DAYS: 400.0, 7 + HOLD_DAYS: 400.0})
    day0 = lagged_return(s, EVENT_TS, 0, spread=0.0)
    late = lagged_return(s, EVENT_TS, 7, spread=0.0)
    assert day0 is not None and late is not None
    assert late < day0, "a late entry must not get the day-0 price"


def test_sparse_series_cannot_masquerade_as_a_late_entry():
    """If no bar exists near the requested lag, return None rather than
    silently entering days later — that would flatter the late lags with a
    price from a different point in the decay."""
    s = _series({0: 100.0, 20: 150.0, HOLD_DAYS + 20: 150.0})
    assert lagged_return(s, EVENT_TS, 7, spread=0.0) is None


def test_missing_exit_bar_returns_none():
    s = _series({0: 100.0})           # nothing HOLD_DAYS later
    assert lagged_return(s, EVENT_TS, 0, spread=0.0) is None


def test_zero_entry_price_is_not_a_trade():
    s = _series({0: 0.0, HOLD_DAYS: 100.0})
    assert lagged_return(s, EVENT_TS, 0, spread=0.0) is None


def test_wider_spread_always_costs_more():
    s = _series({0: 100.0, HOLD_DAYS: 150.0})
    tight = lagged_return(s, EVENT_TS, 0, spread=0.01)
    wide = lagged_return(s, EVENT_TS, 0, spread=0.10)
    assert tight is not None and wide is not None
    assert wide < tight
