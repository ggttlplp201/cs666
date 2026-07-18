"""Indicator library: constructed fixtures with known answers."""

import numpy as np
import pandas as pd
import pytest

from shared_b import indicators as ind


def _frame(price, listings=None, volume=None, bids=None):
    n = len(price)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    price = np.asarray(price, dtype=float)
    return pd.DataFrame(
        {
            "sell_price": price,
            "buy_price": price * 0.98,
            "listing_count": listings if listings is not None else np.full(n, 100),
            "buy_order_count": bids if bids is not None else np.full(n, 10),
            "volume": volume if volume is not None else np.full(n, 20),
            "valid_buy_orders": np.full(n, 5),
        },
        index=idx,
    )


def test_bollinger_pct_b_bounds():
    rng = np.random.default_rng(0)
    px = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 100))))
    bb = ind.bollinger(px)
    tail = bb.dropna()
    # price inside bands most of the time -> pct_b mostly within [0,1]
    assert ((tail["pct_b"] > -0.5) & (tail["pct_b"] < 1.5)).all()
    assert (tail["bandwidth"] >= 0).all()


def test_accumulation_s1_fires_on_flat_price_shrinking_listings():
    n = 60
    rng = np.random.default_rng(1)
    price = 100 + rng.normal(0, 0.2, n)               # flat
    listings = np.linspace(150, 90, n).astype(int)    # shrinking ~-0.8%/day
    df = _frame(price, listings=listings)
    sig = ind.accumulation_signals(df, market_return_5d=0.0)
    assert sig.sideways_shrinking_float


def test_accumulation_s1_no_fire_on_trending_price():
    n = 60
    price = np.linspace(100, 140, n)                  # +40%: not flat
    listings = np.linspace(150, 90, n).astype(int)
    df = _frame(price, listings=listings)
    sig = ind.accumulation_signals(df, market_return_5d=0.0)
    assert not sig.sideways_shrinking_float


def test_accumulation_s2_volume_spike_flat_price():
    n = 60
    rng = np.random.default_rng(2)
    price = 100 + rng.normal(0, 0.1, n)
    volume = np.full(n, 20)
    volume[-2] = 200                                  # 10x spike, price flat
    df = _frame(price, volume=volume)
    sig = ind.accumulation_signals(df, market_return_5d=0.0)
    assert sig.volume_no_price


def test_accumulation_s3_resilient_when_market_down():
    n = 60
    price = np.concatenate([np.full(30, 100.0), np.linspace(100, 102, 30)])  # holds up
    df = _frame(price)
    mr5 = pd.Series(-0.05, index=df.index)            # market down 5% on 5d
    sig = ind.accumulation_signals(df, market_return_5d=mr5)
    assert sig.resilient
    sig2 = ind.accumulation_signals(df, market_return_5d=pd.Series(0.01, index=df.index))
    assert not sig2.resilient


def test_pump_shape_detects_parabolic_with_collapsing_listings():
    n = 40
    price = np.full(n, 100.0)
    # accelerating blow-off: +40% in 10 days, last 3 days fastest
    price[-10:] = 100 * np.array([1.02, 1.04, 1.07, 1.10, 1.13, 1.17, 1.22, 1.28, 1.35, 1.44])
    listings = np.full(n, 100)
    listings[-10:] = np.linspace(100, 40, 10).astype(int)
    df = _frame(price, listings=listings)
    assert ind.pump_shape(df)


def test_pump_shape_ignores_healthy_rise():
    n = 40
    price = 100 * np.exp(np.linspace(0, 0.15, n))     # steady +16%, no acceleration
    df = _frame(price)
    assert not ind.pump_shape(df)


def test_volume_price_patterns():
    n = 40
    rng = np.random.default_rng(3)
    flat = 100 + rng.normal(0, 0.05, n)
    assert ind.volume_price_pattern(_frame(flat)) == ind.VP_FLAT

    up = np.concatenate([np.full(n - 5, 100.0), np.linspace(100, 108, 5)])
    vol_up = np.full(n, 20); vol_up[-5:] = 60
    assert ind.volume_price_pattern(_frame(up, volume=vol_up)) == ind.VP_HEALTHY_RISE

    vol_dn = np.full(n, 60); vol_dn[-5:] = 5
    assert ind.volume_price_pattern(_frame(up, volume=vol_dn)) == ind.VP_WEAK_RALLY


def test_listings_volume_rule():
    n = 30
    price = np.full(n, 100.0)
    good = _frame(price, listings=np.linspace(150, 100, n).astype(int),
                  volume=np.linspace(10, 40, n).astype(int))
    bad = _frame(price, listings=np.linspace(100, 150, n).astype(int),
                 volume=np.linspace(40, 10, n).astype(int))
    assert ind.listings_volume_rule(good) == 1
    assert ind.listings_volume_rule(bad) == -1


def test_volume_profile_support_vs_resistance():
    n = 80
    rng = np.random.default_rng(4)
    # long consolidation near 100 (dense zone), then price above it
    price = np.concatenate([100 + rng.normal(0, 1, 80 - 10), np.linspace(101, 112, 10)])
    df = _frame(price, volume=np.full(n, 30))
    vp = ind.volume_profile(df)
    assert vp is not None
    assert vp.price_vs_zone > 0          # dense zone below price = support
    assert vp.concentration > 0.25


def test_steam_sale_calendar():
    assert ind.in_steam_sale_window(pd.Timestamp("2026-06-30"))   # Summer 2026
    assert ind.in_steam_sale_window(pd.Timestamp("2025-12-25"))   # Winter 2025
    assert ind.in_steam_sale_window(pd.Timestamp("2025-10-02"))   # Autumn 2025 (moved)
    assert not ind.in_steam_sale_window(pd.Timestamp("2026-05-15"))
    # generic fallback outside the verified table
    assert ind.in_steam_sale_window(pd.Timestamp("2022-06-25"))


def test_ma_deviation_sign():
    px = pd.Series(np.linspace(100, 110, 20))
    assert ind.ma_deviation(px, 7).iloc[-1] > 0       # above its MA in an uptrend
