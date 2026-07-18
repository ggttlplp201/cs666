"""Market-regime classifier (Shared §2) — gates everything.

Quantifies the regime from (a) breadth: share of tracked primaries above
their Bollinger middle band, and (b) aggregate executed volume vs baseline.
Fill-speed input from the docs is unavailable until the live feed lands, so
the classifier uses the two measurable legs; thresholds sit in config
`regime:`. Emits Regime consumed by both systems' risk gates.
"""

from __future__ import annotations

from shared.indicators import bollinger
from shared.schema import Item, Regime


def classify_regime(
    primaries_history: dict[str, list[Item]],
    breadth_window: int,
    bull_breadth_min: float,
    bear_breadth_max: float,
    weak_volume_ratio_max: float,
    bollinger_num_std: float = 2.0,
) -> Regime:
    usable = {
        name: hist
        for name, hist in primaries_history.items()
        if len(hist) >= breadth_window
    }
    if not usable:
        # No history yet — treat as weak (small-size / caution), never bull.
        return Regime.WEAK

    above = 0
    volume_ratios: list[float] = []
    for hist in usable.values():
        prices = [i.buff_lowest_sell_cny for i in hist]
        if bollinger(prices, breadth_window, bollinger_num_std).above_middle:
            above += 1
        # Volume leg degrades to neutral when executed volume is unavailable
        # (cs2.sh Developer tier) — breadth alone then drives the regime.
        volumes = [i.buff_volume_24h for i in hist[-(breadth_window + 1):-1]]
        latest_volume = hist[-1].buff_volume_24h
        if latest_volume is not None and all(v is not None for v in volumes):
            baseline_volume = sum(volumes) / len(volumes)
            if baseline_volume:
                volume_ratios.append(latest_volume / baseline_volume)

    breadth = above / len(usable)
    aggregate_volume_ratio = (
        sum(volume_ratios) / len(volume_ratios) if volume_ratios else 1.0
    )

    if aggregate_volume_ratio <= weak_volume_ratio_max:
        return Regime.WEAK
    if breadth >= bull_breadth_min:
        return Regime.BULL
    if breadth <= bear_breadth_max:
        return Regime.BEAR
    return Regime.SIDEWAYS
