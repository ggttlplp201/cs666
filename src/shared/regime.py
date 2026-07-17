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
        if bollinger(prices, breadth_window, 2.0).above_middle:
            above += 1
        baseline = hist[-(breadth_window + 1):-1]
        baseline_volume = sum(i.buff_volume_24h for i in baseline) / len(baseline)
        if baseline_volume:
            volume_ratios.append(hist[-1].buff_volume_24h / baseline_volume)

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
