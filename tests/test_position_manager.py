import pytest

from shared.schema import Item
from system_a.position_manager import assess

DAY = 86400.0

PM = {
    "thesis_invalidation_days": 14,
    "thesis_min_progress_pct": 0.0,
    "patient_exit": {"grace_days": 5},
    "rules": {"stop_loss": True, "take_profit": True, "surge_euphoria": True,
              "large_consecutive_rises": True, "upper_band_green": True,
              "slow_decline_high_volume": True},
    "hold_rules": {"consecutive_small_rises": True, "sharp_drop_low_volume": True,
                   "upper_band_no_green": True, "big_crash_no_sell": True},
    "thresholds": {"surge_1d_pct": 0.07, "surge_3d_pct": 0.12,
                   "small_rise_max_pct": 0.03, "large_rise_day_pct": 0.05,
                   "crash_1d_pct": -0.10, "sharp_drop_pct": -0.05,
                   "slow_decline_3d_band": [-0.10, -0.02],
                   "upper_band_pct_b": 0.95, "green_bar_volume_ratio": 1.5,
                   "low_volume_ratio": 0.7},
}
IND = {"bollinger_window": 20, "bollinger_num_std": 2.0, "volume_baseline_window": 14}
BR = {"take_profit_pct": [0.10, 0.15], "stop_loss_cut_pct": -0.10}


def _series(prices, volumes=None):
    volumes = volumes or [30] * len(prices)
    return [
        Item(market_hash_name="X", buff_lowest_sell_cny=p, buff_highest_buy_cny=p,
             buff_listing_count=100, buff_buy_order_count=10,
             buff_volume_24h=volumes[i], ts=i * DAY)
        for i, p in enumerate(prices)
    ]


def _assess(prices, entry=100.0, held=10, volumes=None, pm=PM):
    return assess(_series(prices, volumes), entry, held, pm, IND, BR)


class TestExitRules:
    def test_stop_loss_beats_crash_suppression(self):
        # -11% day AND ret below stop: the hard stop must win over "don't
        # sell in a crash"
        r = _assess([100.0] * 5 + [89.0], entry=100.0)
        assert (r.action, r.rule) == ("exit", "stop_loss")

    def test_big_crash_suppresses_ta_exit_when_not_stopped(self):
        # -12% day but entry far below → ret still positive, no stop;
        # surge rules would not fire; crash suppression reports itself
        r = _assess([150.0] * 5 + [132.0], entry=100.0)
        assert (r.action, r.rule) == ("suppressed_exit", "big_crash_no_sell")

    def test_surge_euphoria_exits(self):
        r = _assess([100.0] * 5 + [108.0], entry=104.0)  # +8% day, ret +3.8%
        assert (r.action, r.rule) == ("exit", "surge_euphoria")

    def test_take_profit_fires_on_slow_grind(self):
        r = _assess([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111],
                    entry=100.0)
        assert (r.action, r.rule) == ("exit", "take_profit")

    def test_thesis_invalidation_after_n_days_flat(self):
        r = _assess([100.0, 100.5, 99.5, 100.0, 99.8, 100.0], entry=100.0, held=15)
        assert (r.action, r.rule) == ("exit", "thesis_invalidation")
        r2 = _assess([100.0, 100.5, 99.5, 100.0, 99.8, 100.0], entry=100.0, held=10)
        assert r2.action == "hold"

    def test_slow_decline_high_volume_distribution(self):
        prices = [100.0] * 12 + [99.0, 98.0, 97.0]
        volumes = [30] * 12 + [60, 60, 60]
        r = _assess(prices, entry=95.0, volumes=volumes)
        assert (r.action, r.rule) == ("exit", "slow_decline_high_volume")


class TestHoldRules:
    def test_shakeout_low_volume_holds(self):
        prices = [100.0] * 12 + [94.0]
        volumes = [30] * 12 + [10]
        r = _assess(prices, entry=95.0, volumes=volumes)
        assert (r.action, r.rule) == ("hold", "sharp_drop_low_volume")

    def test_consecutive_small_rises_hold(self):
        r = _assess([100, 101, 102, 103], entry=100.0)
        assert (r.action, r.rule) == ("hold", "consecutive_small_rises")

    def test_volume_rules_degrade_without_volume(self):
        prices = [100.0] * 12 + [94.0]
        r = assess(
            [Item("X", p, p, 100, 10, None, i * DAY) for i, p in enumerate(prices)],
            95.0, 10, PM, IND, BR,
        )
        assert "sharp_drop_low_volume" in r.unavailable_rules
        assert r.action == "hold"   # falls through, never proxied from listings

    def test_rules_individually_toggleable(self):
        pm = {**PM, "rules": {**PM["rules"], "surge_euphoria": False,
                              "take_profit": False}}
        r = _assess([100.0] * 5 + [108.0], entry=90.0, pm=pm)
        assert r.action != "exit"
