import pytest

from shared.bus import SignalBus
from shared.indicators import bandwidth_widening, bollinger, volume_price_state
from shared.regime import classify_regime
from shared.schema import Direction, Item, Regime, Signal, SignalType
from shared.synthetic import DAY, ItemSpec, generate_series

VP_KW = dict(
    baseline_window=14, flat_price_pct=0.01,
    volume_high_ratio=1.5, volume_low_ratio=0.7,
)


def _signal(tier=1, items=("A",), conf=0.5, ts=0.0, type_=SignalType.UPDATE_LEAK):
    return Signal(
        tier=tier, type=type_, items=items, direction=Direction.BULLISH,
        confidence=conf, first_seen_ts=ts,
    )


def _series(prices, volumes=None, listings=None, name="X"):
    volumes = volumes or [30] * len(prices)
    listings = listings or [100] * len(prices)
    return [
        Item(
            market_hash_name=name, buff_lowest_sell_cny=p,
            buff_highest_buy_cny=p * 0.97, buff_listing_count=listings[i],
            buff_buy_order_count=5, buff_volume_24h=volumes[i], ts=i * DAY,
        )
        for i, p in enumerate(prices)
    ]


class TestBus:
    def test_dedup_keeps_highest_confidence(self):
        bus = SignalBus()
        bus.publish(_signal(conf=0.5, ts=0.0))
        bus.publish(_signal(conf=0.9, ts=100.0))
        bus.publish(_signal(conf=0.2, ts=200.0))
        active = bus.active([1], now_ts=1000.0, max_age_hours=48)
        assert len(active) == 1
        assert active[0].confidence == 0.9

    def test_tier_filtering_and_decay(self):
        bus = SignalBus()
        bus.publish(_signal(tier=1, items=("A",), ts=0.0))
        bus.publish(_signal(tier=2, items=("B",), ts=0.0, type_=SignalType.CONFIRMED_UPDATE))
        now = 49 * 3600.0
        assert bus.active([1, 2], now, max_age_hours=48) == []
        fresh = _signal(tier=2, items=("C",), ts=now, type_=SignalType.CONFIRMED_UPDATE)
        bus.publish(fresh)
        assert bus.active([2], now, max_age_hours=48) == [fresh]
        assert bus.active([1], now, max_age_hours=48) == []


class TestBollinger:
    def test_flat_series_pct_b_centered(self):
        state = bollinger([100.0] * 20, window=20, num_std=2.0)
        assert state.middle == 100.0
        assert state.pct_b == 0.5
        assert state.bandwidth == 0.0
        assert not state.above_middle

    def test_rising_price_above_middle_high_pct_b(self):
        prices = [100.0 + i for i in range(25)]
        state = bollinger(prices, window=20, num_std=2.0)
        assert state.above_middle
        assert state.pct_b > 0.7

    def test_bandwidth_widening_on_breakout(self):
        prices = [100.0] * 20 + [100.0, 104.0, 109.0]
        assert bandwidth_widening(prices, window=20, num_std=2.0)
        assert not bandwidth_widening([100.0] * 25, window=20, num_std=2.0)


class TestVolumePrice:
    def test_pattern3_healthy_accumulation(self):
        s = _series([100.0] * 15 + [104.0], volumes=[30] * 15 + [60])
        assert volume_price_state(s, **VP_KW).pattern == 3

    def test_pattern4_weak_rally(self):
        s = _series([100.0] * 15 + [104.0], volumes=[30] * 15 + [10])
        assert volume_price_state(s, **VP_KW).pattern == 4

    def test_pattern1_exit_and_pattern2_shakeout(self):
        exiting = _series(
            [100.0] * 15 + [95.0],
            volumes=[30] * 15 + [80],
            listings=[100] * 15 + [140],
        )
        assert volume_price_state(exiting, **VP_KW).pattern == 1
        shakeout = _series([100.0] * 15 + [95.0], volumes=[30] * 15 + [10])
        assert volume_price_state(shakeout, **VP_KW).pattern == 2

    def test_pattern5_sideways(self):
        s = _series([100.0] * 16)
        assert volume_price_state(s, **VP_KW).pattern == 5


class TestRegime:
    KW = dict(
        breadth_window=20, bull_breadth_min=0.6,
        bear_breadth_max=0.35, weak_volume_ratio_max=0.5,
    )

    def _history(self, trend_pct, volume_last=30):
        spec = ItemSpec("P", 1000.0, daily_vol=0.0)
        series = generate_series([spec], days=25, seed=3)
        items = []
        for day, snap in enumerate(series):
            base = snap[0]
            price = 1000.0 * (1 + trend_pct * day)
            items.append(
                Item(
                    market_hash_name="P", buff_lowest_sell_cny=price,
                    buff_highest_buy_cny=price * 0.97, buff_listing_count=100,
                    buff_buy_order_count=5,
                    buff_volume_24h=volume_last if day == len(series) - 1 else 30,
                    ts=base.ts,
                )
            )
        return items

    def test_bull_bear_sideways_weak(self):
        rising = {f"p{i}": self._history(+0.01) for i in range(4)}
        assert classify_regime(rising, **self.KW) == Regime.BULL
        falling = {f"p{i}": self._history(-0.01) for i in range(4)}
        assert classify_regime(falling, **self.KW) == Regime.BEAR
        mixed = {"a": self._history(+0.01), "b": self._history(-0.01)}
        assert classify_regime(mixed, **self.KW) == Regime.SIDEWAYS
        dried_up = {f"p{i}": self._history(+0.01, volume_last=5) for i in range(4)}
        assert classify_regime(dried_up, **self.KW) == Regime.WEAK

    def test_no_history_defaults_weak(self):
        assert classify_regime({}, **self.KW) == Regime.WEAK
        assert classify_regime({"p": self._history(0.0)[:5]}, **self.KW) == Regime.WEAK
