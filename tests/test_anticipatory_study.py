import pytest

from shared.schema import Item
from shared.store import SnapshotStore
from system_a.anticipatory_study import _exit_net, _limit_fill
from system_a.event_study import DAY


def _bar(name, price, ts):
    return Item(
        market_hash_name=name, buff_lowest_sell_cny=price,
        buff_highest_buy_cny=price, buff_listing_count=0,
        buff_buy_order_count=0, buff_volume_24h=None, ts=ts,
    )


def _series(prices, t0=0.0):
    return [_bar("X", p, t0 + i * DAY) for i, p in enumerate(prices)]


class TestLimitFill:
    def test_fills_only_when_price_touches_limit(self):
        series = _series([100, 99, 97, 101])
        # limit 98: day-2 median 97 <= 98 → fills on day 2
        assert _limit_fill(series, 98.0, -1.0, 10 * DAY) == 2 * DAY
        # limit 96: never touched → missed trade
        assert _limit_fill(series, 96.0, -1.0, 10 * DAY) is None

    def test_never_fills_at_or_after_event(self):
        series = _series([100, 90])   # crash on day 1 = event day
        assert _limit_fill(series, 95.0, -1.0, 1 * DAY) is None  # window ends


class TestExitNet:
    def test_lock_served_pre_event_sells_into_announcement(self):
        # fill day 0, event day 10 → lock (7d) already served → exit at event bar
        series = _series([100] * 10 + [120, 121])
        net = _exit_net(series, 95.0, 0.0, 10 * DAY, 0.04, 0.025, 7)
        expected = 120 * 0.98 * 0.975 / 95.0 - 1
        assert net == pytest.approx(expected)

    def test_late_fill_waits_for_unlock(self):
        # fill day 8, event day 10 → unlock day 15 → exit at day-15 bar
        series = _series([100] * 10 + [120] * 3 + [110, 108, 105])
        net = _exit_net(series, 100.0, 8 * DAY, 10 * DAY, 0.0, 0.0, 7)
        assert net == pytest.approx(0.05)   # day-15 price 105, no frictions
