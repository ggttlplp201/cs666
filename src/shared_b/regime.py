"""Market-regime classifier (Shared §2) — gates everything.

Quantified exactly as the doc prescribes: breadth (share of tracked primaries
above their middle band), market-wide listings-vs-buy-orders balance, and a
fill-speed proxy (aggregate executed volume vs. its baseline). Emits
`market_regime in {bull, bear, sideways, weak}` that both systems read.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import PanelView
from .indicators import EPS, zscore
from .schema import Regime


@dataclass
class RegimeReading:
    regime: Regime
    breadth: float               # share of tracked items above their middle band
    market_return_5d: float      # cap-ignored equal-weight index return, 5d
    market_return_20d: float
    listings_bids_balance: float # z of (buy orders - listings) balance, market-wide
    volume_z: float              # aggregate executed volume vs baseline (fill-speed proxy)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["regime"] = self.regime.value
        return d


def _tracked(view: PanelView, min_history: int) -> list[str]:
    out = []
    for item in view.items:
        m = view.meta.get(item)
        # regime is read off primaries/mid-tier — the market's bellwethers
        if m is not None and not (m.is_primary or m.is_secondary_primary or m.category == "mid_tier_primary"):
            continue
        if len(view.history(item)) >= min_history:
            out.append(item)
    # fall back to everything with history if metadata is sparse
    if len(out) < 5:
        out = [i for i in view.items if len(view.history(i)) >= min_history]
    return out


def market_index_returns(view: PanelView, lookback: int = 90) -> pd.Series:
    """Equal-weight index of daily log returns across tracked items."""
    rets = {}
    for item in _tracked(view, min_history=25):
        px = view.history(item, window=lookback + 1)["sell_price"]
        if len(px) > 5:
            rets[item] = np.log(px / px.shift(1))
    if not rets:
        return pd.Series(dtype=float)
    return pd.DataFrame(rets).mean(axis=1).dropna()


def classify_regime(
    view: PanelView,
    ma_window: int = 20,
    breadth_bull: float = 0.65,
    breadth_bear: float = 0.35,
    volume_weak_z: float = -1.0,
    trend_bull: float = 0.03,
    trend_bear: float = -0.03,
) -> RegimeReading:
    items = _tracked(view, min_history=ma_window + 5)

    above = []
    balances = []
    vol_series = []
    for item in items:
        h = view.history(item, window=120)
        px = h["sell_price"]
        ma = px.rolling(ma_window, min_periods=ma_window // 2).mean()
        if not np.isnan(ma.iloc[-1]):
            above.append(float(px.iloc[-1] > ma.iloc[-1]))
        lb = (h["buy_order_count"] - h["listing_count"]) / (
            h["buy_order_count"] + h["listing_count"] + EPS
        )
        if len(lb) > 10:
            balances.append(zscore(lb, 60).iloc[-1])
        vol_series.append(h["volume"])

    breadth = float(np.mean(above)) if above else 0.5
    balance = float(np.nanmean(balances)) if balances else 0.0

    idx = market_index_returns(view)
    r5 = float(idx.tail(5).sum()) if len(idx) >= 5 else 0.0
    r20 = float(idx.tail(20).sum()) if len(idx) >= 20 else 0.0

    total_vol = pd.concat(vol_series, axis=1).sum(axis=1) if vol_series else pd.Series(dtype=float)
    vol_z = float(zscore(total_vol, 60).iloc[-1]) if len(total_vol) > 20 else 0.0
    if np.isnan(vol_z):
        vol_z = 0.0

    # decision table (Shared §2): weak = dried-up liquidity regardless of drift;
    # bull/bear need breadth AND trend agreement; else sideways.
    if vol_z <= volume_weak_z and breadth < breadth_bull:
        regime = Regime.WEAK
    elif breadth >= breadth_bull and r20 >= trend_bull:
        regime = Regime.BULL
    elif breadth <= breadth_bear and r20 <= trend_bear:
        regime = Regime.BEAR
    else:
        regime = Regime.SIDEWAYS

    return RegimeReading(
        regime=regime,
        breadth=breadth,
        market_return_5d=r5,
        market_return_20d=r20,
        listings_bids_balance=balance,
        volume_z=vol_z,
    )
