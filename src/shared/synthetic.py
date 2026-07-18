"""Synthetic BUFF-like market generator with ground truth.

Until real cs2.sh / history keys are wired in (HANDOFF §0), the engine is
developed and validated against a simulator that embeds the structures the
strategy is supposed to exploit or avoid:

- regime process (bull/sideways/bear/weak) driving common drift + liquidity,
- whale ACCUMULATION -> MARKUP -> DISTRIBUTION episodes on structurally good
  items (flat price + shrinking listings + volume-without-price + resilience,
  then the pump the accumulation preceded),
- one-wave PUMP-AND-DUMP traps (parabolic + collapsing listings, then crash),
- fat-tailed idiosyncratic noise (student-t, vol clustering),
- Steam-sale seasonal dips,
- bid-ask spread, listing/bid depth, executed volume tied to supply/liquidity.

Ground truth (episode calendars) is returned so tests can assert the detectors
fire when they should.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .data import MarketPanel
from .indicators import in_steam_sale_window
from .schema import ItemMeta, SourceStatus

WEAPONS_PRIMARY = ["AK-47", "M4A4", "M4A1-S", "USP-S", "Glock-18", "AWP"]
WEAPONS_SECONDARY = ["Galil AR", "FAMAS", "P250", "MAC-10", "MP9", "UMP-45"]


@dataclass
class Episode:
    kind: str          # accumulation | markup | distribution | pump | crash
    start: int         # day index
    end: int


@dataclass
class SyntheticMarket:
    panel: MarketPanel
    episodes: dict[str, list[Episode]] = field(default_factory=dict)
    regimes: pd.Series | None = None   # ground-truth regime per day


def _regime_path(n_days: int, rng: np.random.Generator) -> np.ndarray:
    """Markov-ish regime sequence with realistic dwell times."""
    regimes = []
    cur = rng.choice(["sideways", "bull"])
    while len(regimes) < n_days:
        dwell = int(rng.integers(30, 90))
        regimes.extend([cur] * dwell)
        nxt = {
            "bull": [("sideways", 0.5), ("bear", 0.3), ("bull", 0.2)],
            "bear": [("weak", 0.35), ("sideways", 0.45), ("bear", 0.2)],
            "sideways": [("bull", 0.35), ("bear", 0.25), ("weak", 0.15), ("sideways", 0.25)],
            "weak": [("sideways", 0.55), ("bull", 0.20), ("weak", 0.25)],
        }[cur]
        names, probs = zip(*nxt)
        cur = rng.choice(names, p=np.array(probs) / sum(probs))
    return np.array(regimes[:n_days])


REGIME_DRIFT = {"bull": 0.0012, "sideways": 0.0000, "bear": -0.0015, "weak": -0.0004}
REGIME_VOL_MULT = {"bull": 1.1, "sideways": 0.9, "bear": 1.3, "weak": 0.7}
REGIME_VOLUME_MULT = {"bull": 1.4, "sideways": 1.0, "bear": 0.8, "weak": 0.45}
REGIME_BID_MULT = {"bull": 1.3, "sideways": 1.0, "bear": 0.6, "weak": 0.5}


def _item_meta(i: int, rng: np.random.Generator) -> ItemMeta:
    r = rng.random()
    if r < 0.35:
        category, weapon = "mid_tier_primary", rng.choice(WEAPONS_PRIMARY)
        base_price = float(rng.uniform(1000, 3000))
    elif r < 0.55:
        category, weapon = "small_item", rng.choice(WEAPONS_SECONDARY + WEAPONS_PRIMARY)
        base_price = float(rng.uniform(50, 500))
    elif r < 0.75:
        category, weapon = "collection", rng.choice(WEAPONS_PRIMARY + WEAPONS_SECONDARY)
        base_price = float(rng.uniform(400, 2500))
    elif r < 0.9:
        category, weapon = "material", "Sticker"
        base_price = float(rng.uniform(80, 800))
    else:
        category, weapon = "glove", "Gloves"
        base_price = float(rng.uniform(2000, 8000))

    supply = int(rng.choice(
        [rng.integers(800, 2000), rng.integers(2000, 10000),
         rng.integers(10000, 30000), rng.integers(30000, 80000)],
        p=[0.15, 0.35, 0.3, 0.2],
    ))
    statuses = [SourceStatus.ACTIVE, SourceStatus.RETIRED, SourceStatus.DISCONTINUED]
    status = statuses[int(rng.choice(3, p=[0.45, 0.3, 0.25]))]
    name = f"SYN | {weapon} Item-{i:03d}"
    meta = ItemMeta(
        market_hash_name=name,
        weapon=str(weapon),
        category=str(category),
        collection=f"Collection-{i % 7}",
        source_status=status,
        rerelease_risk=float(rng.uniform(0, 0.5)) if status != SourceStatus.ACTIVE else 0.1,
        case_price_cny=float(rng.choice([rng.uniform(20, 79), rng.uniform(80, 400)], p=[0.3, 0.7])),
        supply=supply,
        supply_fn=int(supply * rng.uniform(0.1, 0.4)),
        aesthetics=float(np.clip(rng.beta(2.5, 2.5), 0, 1)),
        is_primary=weapon in WEAPONS_PRIMARY,
        is_secondary_primary=weapon in WEAPONS_SECONDARY[:3],
    )
    meta.notes = f"base_price={base_price:.0f}"
    return meta


def generate(
    n_items: int = 60,
    n_days: int = 720,
    start: date = date(2024, 1, 1),
    seed: int = 7,
    accumulation_share: float = 0.3,
    pump_share: float = 0.12,
) -> SyntheticMarket:
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, periods=n_days, freq="D")
    regime = _regime_path(n_days, rng)

    metas = [_item_meta(i, rng) for i in range(n_items)]
    frames: dict[str, pd.DataFrame] = {}
    episodes: dict[str, list[Episode]] = {}

    # which items get whale treatment: structurally good ones preferentially
    def quality(m: ItemMeta) -> float:
        q = 0.0
        if 2000 <= m.supply <= 30000:
            q += 1.0
        if m.case_price_cny >= 80:
            q += 0.5
        q += m.aesthetics
        if m.is_primary or m.is_secondary_primary:
            q += 0.5
        return q

    ranked = sorted(range(n_items), key=lambda i: -quality(metas[i]))
    accum_items = set(ranked[: int(n_items * accumulation_share)])
    pump_items = set(rng.choice(
        [i for i in range(n_items) if i not in accum_items],
        size=max(1, int(n_items * pump_share)), replace=False,
    ).tolist())

    market_shock = rng.standard_t(4, n_days) * 0.004  # common fat-tailed factor

    for i, meta in enumerate(metas):
        base_price = float(meta.notes.split("=")[1])
        eps: list[Episode] = []

        # idiosyncratic vol with clustering
        sigma = 0.018 * np.ones(n_days)
        for t in range(1, n_days):
            sigma[t] = np.sqrt(0.00002 + 0.08 * (sigma[t - 1] * rng.standard_normal()) ** 2 + 0.88 * sigma[t - 1] ** 2)
        idio = rng.standard_t(4, n_days) * sigma

        drift = np.array([REGIME_DRIFT[r] for r in regime])
        vol_mult = np.array([REGIME_VOL_MULT[r] for r in regime])
        log_ret = drift + market_shock * vol_mult + idio * vol_mult

        # Steam sale dips (liquidation pressure)
        sale_mask = np.array([in_steam_sale_window(d) for d in days])
        log_ret[sale_mask] -= 0.0025

        # baseline microstructure levels
        liquidity = np.clip(30000 / max(meta.supply, 500), 0.2, 6.0)
        base_volume = max(3, int(8 * liquidity * rng.uniform(0.5, 2.0)))
        base_listings = max(20, int(meta.supply * rng.uniform(0.004, 0.012)))
        base_bids = max(2, int(base_volume * rng.uniform(0.5, 1.5)))

        listing_mult = np.ones(n_days)
        volume_mult = np.ones(n_days)
        price_add = np.zeros(n_days)

        # ---------------- whale accumulation -> markup -> distribution -------
        n_episodes = max(1, (n_days - 140) // 300) if i in accum_items and n_days >= 260 else 0
        starts: list[int] = []
        if n_episodes:
            seg = (n_days - 130 - 60) // n_episodes
            starts = [60 + k * seg + int(rng.integers(0, max(seg - 110, 1)))
                      for k in range(n_episodes)]
        for t0 in starts:
            acc_len = int(rng.integers(25, 45))
            mark_len = int(rng.integers(15, 35))
            dist_len = int(rng.integers(15, 30))
            a0, a1 = t0, t0 + acc_len
            m0, m1 = a1, a1 + mark_len
            d0, d1 = m1, min(m1 + dist_len, n_days)
            eps += [Episode("accumulation", a0, a1), Episode("markup", m0, m1),
                    Episode("distribution", d0, d1)]
            # accumulation: cancel drift (flat), listings shrink, volume spikes some days
            log_ret[a0:a1] = idio[a0:a1] * 0.4  # suppressed vol, flat, resilient to market
            shrink = np.linspace(1.0, rng.uniform(0.55, 0.75), acc_len)
            listing_mult[a0:a1] *= shrink
            spike_days = rng.choice(np.arange(a0, a1), size=max(2, acc_len // 8), replace=False)
            volume_mult[spike_days] *= rng.uniform(3.0, 5.0)
            # markup: strong up-trend with volume
            log_ret[m0:m1] += rng.uniform(0.010, 0.022)
            volume_mult[m0:m1] *= rng.uniform(1.8, 3.0)
            listing_mult[m0:m1] *= np.linspace(0.75, 0.9, mark_len)
            # distribution: flat/slightly down, listings rebuild, volume high then fading
            log_ret[d0:d1] -= rng.uniform(0.001, 0.004)
            listing_mult[d0:d1] *= np.linspace(0.9, 1.5, d1 - d0)
            volume_mult[d0:d1] *= np.linspace(2.0, 0.8, d1 - d0)

        # ---------------- one-wave pump-and-dump ----------------------------
        if i in pump_items and n_days >= 200:
            t0 = int(rng.integers(100, n_days - 50))
            pump_len = int(rng.integers(5, 10))
            crash_len = int(rng.integers(10, 25))
            p0, p1 = t0, t0 + pump_len
            c0, c1 = p1, min(p1 + crash_len, n_days)
            eps += [Episode("pump", p0, p1), Episode("crash", c0, c1)]
            per_day = rng.uniform(0.5, 0.9) / pump_len
            ramp = np.linspace(0.5 * per_day, 2.0 * per_day, pump_len)  # accelerating
            log_ret[p0:p1] += ramp
            listing_mult[p0:p1] *= np.linspace(1.0, 0.4, pump_len)  # collapsing listings
            volume_mult[p0:p1] *= np.linspace(1.5, 0.7, pump_len)   # price up, volume fading
            log_ret[c0:c1] -= rng.uniform(0.6, 0.9) / crash_len
            listing_mult[c0:c1] *= np.linspace(1.5, 2.2, c1 - c0)

        price = base_price * np.exp(np.cumsum(log_ret)) + price_add
        price = np.maximum(price, 1.0)

        reg_vol = np.array([REGIME_VOLUME_MULT[r] for r in regime])
        reg_bid = np.array([REGIME_BID_MULT[r] for r in regime])
        volume = np.maximum(
            rng.poisson(np.maximum(base_volume * volume_mult * reg_vol, 0.1)), 0
        )
        listings = np.maximum(
            (base_listings * listing_mult * rng.uniform(0.9, 1.1, n_days)).astype(int), 1
        )
        bids = np.maximum(
            (base_bids * reg_bid * rng.uniform(0.7, 1.3, n_days)).astype(int), 0
        )
        spread = np.clip(rng.normal(0.02, 0.005, n_days), 0.008, 0.05)
        valid_bids = np.maximum((bids * np.clip(rng.normal(0.6, 0.15, n_days), 0.1, 1.0)).astype(int), 0)

        frames[meta.market_hash_name] = pd.DataFrame(
            {
                "sell_price": price,
                "buy_price": price * (1 - spread),
                "listing_count": listings,
                "buy_order_count": bids,
                "volume": volume,
                "valid_buy_orders": valid_bids,
            },
            index=days,
        )
        episodes[meta.market_hash_name] = eps

    panel = MarketPanel(frames=frames, meta={m.market_hash_name: m for m in metas})
    return SyntheticMarket(
        panel=panel,
        episodes=episodes,
        regimes=pd.Series(regime, index=days),
    )
