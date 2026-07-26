"""Deterministic synthetic market data for tests, demos, and backtest fixtures.

Generates daily Item snapshots as a seeded geometric random walk, with
optional injected events (price jumps with volume surges) so event-study
mechanics can be exercised without live data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from shared.schema import Item

DAY = 86400.0


@dataclass
class ItemSpec:
    name: str
    start_price: float
    daily_vol: float = 0.02          # stdev of daily log-ish return
    volume_24h: int = 30
    listing_count: int = 120
    buy_order_count: int = 6
    # events: ts_day → (price_jump_pct, volume_multiplier), applied that day
    events: dict[int, tuple[float, float]] = field(default_factory=dict)


def generate_series(
    specs: list[ItemSpec],
    days: int,
    start_ts: float = 1_700_000_000.0,
    seed: int = 7,
    spread_pct: float = 0.03,
) -> list[list[Item]]:
    """Return one snapshot list per day for every spec."""
    rng = random.Random(seed)
    prices = {s.name: s.start_price for s in specs}
    snapshots: list[list[Item]] = []
    for day in range(days):
        ts = start_ts + day * DAY
        snapshot: list[Item] = []
        for spec in specs:
            drift = rng.gauss(0.0, spec.daily_vol)
            jump, vol_mult = spec.events.get(day, (0.0, 1.0))
            prices[spec.name] *= (1.0 + drift + jump)
            price = round(prices[spec.name], 2)
            volume = max(1, int(spec.volume_24h * vol_mult))
            snapshot.append(
                Item(
                    market_hash_name=spec.name,
                    buff_lowest_sell_cny=price,
                    buff_highest_buy_cny=round(price * (1 - spread_pct), 2),
                    buff_listing_count=spec.listing_count,
                    buff_buy_order_count=spec.buy_order_count,
                    buff_volume_24h=volume,
                    ts=ts,
                )
            )
        snapshots.append(snapshot)
    return snapshots
