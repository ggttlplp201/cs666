"""Indicator library (Shared §3): Bollinger features + volume–price patterns.

Pure functions over price/Item series; every threshold comes in via the
`indicators:` block of config/shared.yaml.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shared.schema import Item


@dataclass(frozen=True)
class BollingerState:
    upper: float
    middle: float
    lower: float
    pct_b: float          # 0 = at lower band, 1 = at upper band
    bandwidth: float      # (upper-lower)/middle — trend speed (§3.1)
    above_middle: bool


def bollinger(prices: list[float], window: int, num_std: float) -> BollingerState:
    if len(prices) < window:
        raise ValueError(f"need >= {window} prices, got {len(prices)}")
    tail = prices[-window:]
    middle = sum(tail) / window
    variance = sum((p - middle) ** 2 for p in tail) / window
    std = math.sqrt(variance)
    upper, lower = middle + num_std * std, middle - num_std * std
    price = prices[-1]
    span = upper - lower
    pct_b = 0.5 if span == 0 else (price - lower) / span
    return BollingerState(
        upper=upper,
        middle=middle,
        lower=lower,
        pct_b=pct_b,
        bandwidth=span / middle if middle else 0.0,
        above_middle=price > middle,
    )


def bandwidth_widening(prices: list[float], window: int, num_std: float) -> bool:
    """True when the band is wider now than one step ago — trend accelerating."""
    if len(prices) < window + 1:
        return False
    now = bollinger(prices, window, num_std).bandwidth
    prev = bollinger(prices[:-1], window, num_std).bandwidth
    return now > prev


@dataclass(frozen=True)
class VolumePriceState:
    """One of the five §3.3 patterns for the latest step of a series."""
    pattern: int          # 1..5
    price_change_pct: float
    volume_ratio: float   # latest volume vs baseline mean
    listings_change: int


def volume_price_state(
    series: list[Item],
    baseline_window: int,
    flat_price_pct: float,
    volume_high_ratio: float,
    volume_low_ratio: float,
) -> VolumePriceState:
    if len(series) < 2:
        raise ValueError("need >= 2 snapshots")
    latest, prev = series[-1], series[-2]
    baseline = series[-(baseline_window + 1):-1]
    baseline_volume = sum(i.buff_volume_24h for i in baseline) / len(baseline)
    volume_ratio = (
        latest.buff_volume_24h / baseline_volume if baseline_volume else 1.0
    )
    price_change = (
        (latest.buff_lowest_sell_cny - prev.buff_lowest_sell_cny)
        / prev.buff_lowest_sell_cny
    )
    listings_change = latest.buff_listing_count - prev.buff_listing_count

    price_up = price_change > flat_price_pct
    price_down = price_change < -flat_price_pct
    volume_up = volume_ratio >= volume_high_ratio
    volume_down = volume_ratio <= volume_low_ratio

    if price_down and listings_change > 0 and volume_up:
        pattern = 1   # main force exiting — bearish
    elif price_down and volume_down:
        pattern = 2   # low-volume shakeout
    elif price_up and volume_up:
        pattern = 3   # healthy accumulation — the confirm-entry state
    elif price_up and volume_down:
        pattern = 4   # weak rally / one-wave pump — never enter
    else:
        pattern = 5   # sideways / no signal
    return VolumePriceState(pattern, price_change, volume_ratio, listings_change)
