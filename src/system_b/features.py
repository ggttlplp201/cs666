"""Cross-sectional feature builder (System B §3 + Pettersson feature spec).

`compute_row(view, item, ...)` builds one item's features strictly from a
no-lookahead PanelView — the same function feeds live decisions, the model's
training matrix, and the backtest, so there is exactly one causal code path.

Feature blocks:
- structural factors (Shared §4): supply band, source outlook, circulation,
  meta status, aesthetics, category priority
- TA/timing (Shared §3): Bollinger pct_b/bandwidth/side + touch signal,
  MA-deviation momentum (the empirically dominant predictor), 7d reversal,
  volume z, listing slope, volume-price pattern, volume-profile shape/drift
- whale-accumulation signals S1-S3 + count (the entry timing)
- conditional volatility (GARCH/EWMA, Shared §11)
- Tier-3 attention/sentiment from the shared bus (leading entry feature)
- calendar: Steam-sale window flag
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shared_b import indicators as ind
from shared_b.data import PanelView
from shared_b.garch import ewma_sigma, fit_garch
from shared_b.regime import RegimeReading, market_index_returns
from shared_b.schema import ItemMeta, SourceStatus
from shared_b.signal_bus import BusSignal, attention_for_item

VP_SHAPE_SCORE = {
    ind.SHAPE_LOW_PEAK: 1.0,
    ind.SHAPE_DOUBLE: 0.3,
    ind.SHAPE_HIGH_PEAK: -0.7,
    ind.SHAPE_SCATTERED: -1.0,
    None: 0.0,
}

# columns fed to the ML ranker (structural + timing; excludes ids/prices)
MODEL_FEATURES = [
    "supply_band_score",
    "supply_log",
    "source_score",
    "case_price_log",
    "meta_score",
    "weapon_priority",
    "category_priority",
    "aesthetics",
    "circulation_score",
    "volume_avg_20",
    "ma_dev_7",
    "ret_1d",
    "ret_7d",
    "ret_21d",
    "pct_b",
    "bandwidth",
    "middle_band_side",
    "bb_touch",
    "vol_z_20",
    "listing_slope_10",
    "spread_pct",
    "vp_pattern",
    "listings_volume_rule",
    "vp_shape_score",
    "vp_price_vs_zone",
    "vp_drift",
    "vp_concentration",
    "accum_s1",
    "accum_s2",
    "accum_s3",
    "accum_count",
    "pump_flag",
    "garch_vol",
    "attention",
    "sentiment",
    "attention_early",
    "steam_sale",
    "market_ret_5d",
    "market_breadth",
]


@dataclass
class VolCache:
    """Weekly GARCH refits per item + cheap recursion forward (Shared §11).

    Refitting GARCH per item per day is wasteful; parameters move slowly.
    We refit every `refit_days` and roll the variance recursion forward daily
    with cached (omega, alpha, beta). EWMA fallback for short histories.
    """

    refit_days: int = 7
    _params: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    _fit_day: dict[str, pd.Timestamp] = field(default_factory=dict)

    def daily_vol(self, item: str, view: PanelView) -> float:
        px = view.history(item, window=200)["sell_price"]
        rets = np.log(px / px.shift(1)).dropna().to_numpy()
        if len(rets) < 30:
            return 0.03
        last_fit = self._fit_day.get(item)
        if last_fit is None or (view.day - last_fit).days >= self.refit_days:
            fit = fit_garch(rets)
            if fit is not None and 0.3 <= fit.persistence <= 0.9999:
                self._params[item] = (fit.omega, fit.alpha, fit.beta)
            else:
                self._params.pop(item, None)
            self._fit_day[item] = view.day
        params = self._params.get(item)
        if params is None:
            return ewma_sigma(rets)
        omega, alpha, beta = params
        r = rets - rets.mean()
        s2 = float(np.var(r)) if np.var(r) > 1e-12 else 1e-8
        for x in r[1:]:
            s2 = omega + alpha * x * x + beta * s2
        # one-step-ahead forecast
        s2 = omega + alpha * r[-1] ** 2 + beta * s2
        return float(np.sqrt(max(s2, 1e-12)))


def _structural(meta: ItemMeta | None, cfg_sel: dict, priority_weapons: list[str],
                tilt_secondary: bool) -> dict:
    if meta is None:
        meta = ItemMeta(market_hash_name="?")
    lo, hi = cfg_sel.get("total_supply_sweet_spot", [2000, 10000])
    blo, bhi = cfg_sel.get("total_supply_broad", [10000, 30000])
    s = meta.supply
    if lo <= s <= hi:
        supply_band = 1.0
    elif blo <= s <= bhi:
        supply_band = 0.6
    elif 0 < s < lo:
        supply_band = 0.25   # too scarce -> illiquid (Shared §4.1)
    else:
        supply_band = 0.0

    source = {
        SourceStatus.DISCONTINUED: 1.0,
        SourceStatus.RETIRED: 0.7,
        SourceStatus.ACTIVE: 0.3,
    }[meta.source_status]
    source -= 0.6 * meta.rerelease_risk  # re-release risk = bearish supply outlook

    if meta.is_secondary_primary:
        meta_score = 1.0 if tilt_secondary else 0.6
    elif meta.is_primary:
        meta_score = 0.75 if tilt_secondary else 1.0  # traditional meta saturated this cycle
    else:
        meta_score = 0.2

    wp = 0.3
    if meta.weapon in priority_weapons:
        wp = 1.0 - 0.1 * priority_weapons.index(meta.weapon)

    cat_priority = {
        "glove": 0.9, "material": 0.8, "knife": 0.7,
        "mid_tier_primary": 0.65, "collection": 0.6, "small_item": 0.5,
    }.get(meta.category, 0.3)

    return {
        "supply_band_score": supply_band,
        "supply_log": float(np.log10(max(s, 1))),
        "source_score": float(np.clip(source, 0, 1)),
        "case_price_log": float(np.log10(max(meta.case_price_cny, 1))),
        "meta_score": meta_score,
        "weapon_priority": wp,
        "category_priority": cat_priority,
        "aesthetics": float(meta.aesthetics),
    }


def compute_row(
    view: PanelView,
    item: str,
    regime: RegimeReading,
    bus_signals: list[BusSignal],
    vol_cache: VolCache,
    cfg_sel: dict,
    priority_weapons: list[str],
    tilt_secondary: bool = True,
    accum_params: dict | None = None,
    market_r5: pd.Series | None = None,
) -> dict | None:
    """One item's feature dict at the view's decision day; None if no fresh data."""
    row = view.today(item)
    if row is None:
        return None
    df = view.history(item, window=150)
    if len(df) < 30:
        return None
    px = df["sell_price"]
    meta = view.meta.get(item)

    out: dict = {"item": item, "day": view.day, "price": float(row["sell_price"]),
                 "bid": float(row["buy_price"])}
    out.update(_structural(meta, cfg_sel, priority_weapons, tilt_secondary))

    # circulation
    vol20 = float(df["volume"].tail(20).mean())
    min_tr = float(cfg_sel.get("min_daily_trades", 10))
    out["volume_avg_20"] = vol20
    out["circulation_score"] = float(np.clip((vol20 - min_tr) / min_tr, 0, 1)) if vol20 >= min_tr else 0.0

    # TA block
    bb = ind.bollinger(px)
    last = bb.iloc[-1]
    out["ma_dev_7"] = float(ind.ma_deviation(px, 7).iloc[-1])
    out["ret_1d"] = float(px.iloc[-1] / px.iloc[-2] - 1) if len(px) >= 2 else 0.0
    out["ret_7d"] = float(px.iloc[-1] / px.iloc[-8] - 1) if len(px) >= 8 else 0.0
    out["ret_21d"] = float(px.iloc[-1] / px.iloc[-22] - 1) if len(px) >= 22 else 0.0
    out["pct_b"] = float(np.clip(last["pct_b"], -0.5, 1.5)) if np.isfinite(last["pct_b"]) else 0.5
    out["bandwidth"] = float(last["bandwidth"]) if np.isfinite(last["bandwidth"]) else 0.0
    out["middle_band_side"] = float(last["middle_band_side"]) if np.isfinite(last["middle_band_side"]) else 0.0
    out["bb_touch"] = int(ind.bollinger_touch_signals(df).iloc[-1])
    vz = ind.zscore(df["volume"], 20).iloc[-1]
    out["vol_z_20"] = float(vz) if np.isfinite(vz) else 0.0
    out["listing_slope_10"] = ind.slope_per_day(df["listing_count"], 10)
    out["spread_pct"] = float((row["sell_price"] - row["buy_price"]) / max(row["sell_price"], 1e-9))
    out["vp_pattern"] = ind.volume_price_pattern(df)
    out["listings_volume_rule"] = ind.listings_volume_rule(df)

    vp = ind.volume_profile(df)
    out["vp_shape_score"] = VP_SHAPE_SCORE[vp.shape if vp else None]
    out["vp_price_vs_zone"] = float(np.clip(vp.price_vs_zone, -1, 1)) if vp else 0.0
    out["vp_drift"] = float(np.clip(vp.drift * 100, -5, 5)) if vp else 0.0
    out["vp_concentration"] = vp.concentration if vp else 0.0

    # whale-accumulation signals (sticky-window S2/S3; see indicators §3.4)
    ap = accum_params or {}
    mr5 = market_r5 if market_r5 is not None else regime.market_return_5d
    sig = ind.accumulation_signals(df, market_return_5d=mr5, **ap)
    out["accum_s1"] = int(sig.sideways_shrinking_float)
    out["accum_s2"] = int(sig.volume_no_price)
    out["accum_s3"] = int(sig.resilient)
    out["accum_count"] = sig.count

    out["pump_flag"] = int(ind.pump_shape(df))
    out["garch_vol"] = vol_cache.daily_vol(item, view)

    # Tier-3 attention (leading feature; guard against LATE attention)
    att, sent = attention_for_item(bus_signals, item)
    out["attention"] = att
    out["sentiment"] = sent
    ret_10d = float(px.iloc[-1] / px.iloc[-11] - 1) if len(px) >= 11 else 0.0
    out["attention_early"] = float(att) if (att > 0 and abs(ret_10d) < 0.05) else 0.0
    out["attention_late"] = float(att) if (att > 1.5 and ret_10d > 0.15) else 0.0

    out["steam_sale"] = int(ind.in_steam_sale_window(view.day))
    out["market_ret_5d"] = regime.market_return_5d
    out["market_breadth"] = regime.breadth

    # liquidity/depth raw values the filters + risk gate need
    out["listing_count"] = int(row["listing_count"])
    out["buy_order_count"] = int(row["buy_order_count"])
    out["valid_buy_orders"] = int(row["valid_buy_orders"])
    out["volume_today"] = int(row["volume"])
    return out


def composite_score(features: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Config-weighted structural composite (System B §3.1). Cross-sectionally
    rank-normalizes each factor to [0,1] then applies factor_weights."""
    if features.empty:
        return pd.Series(dtype=float)

    def ranked(col: pd.Series) -> pd.Series:
        if col.nunique() <= 1:
            return pd.Series(0.5, index=col.index)
        return col.rank(pct=True)

    blocks = {
        "supply_outlook": ranked(features["source_score"]),
        "existing_quantity": ranked(features["supply_band_score"]),
        "circulation": ranked(features["circulation_score"]),
        "meta_status": ranked(0.6 * features["meta_score"] + 0.4 * features["weapon_priority"]),
        "aesthetics": ranked(features["aesthetics"]),
        "ma_deviation_momentum": ranked(features["ma_dev_7"]),
    }
    total_w = sum(weights.get(k, 0.0) for k in blocks) or 1.0
    score = sum(weights.get(k, 0.0) * v for k, v in blocks.items()) / total_w
    return score


def build_feature_frame(
    view: PanelView,
    regime: RegimeReading,
    bus_signals: list[BusSignal],
    vol_cache: VolCache,
    cfg: dict,
) -> pd.DataFrame:
    """All items' features at one decision day."""
    sel = cfg.get("selection_filters", {})
    prio = cfg.get("priority", {})
    weapons = list(prio.get("weapons", []))
    tilt = bool(prio.get("tilt_to_secondary_primaries", True))
    accum_params = cfg.get("accumulation_params", {}) or {}
    idx_rets = market_index_returns(view)
    market_r5 = idx_rets.rolling(5).sum() if len(idx_rets) else None
    rows = []
    for item in view.items:
        r = compute_row(
            view, item, regime, bus_signals, vol_cache, sel, weapons, tilt,
            accum_params=accum_params, market_r5=market_r5,
        )
        if r is not None:
            rows.append(r)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("item")
    weights = cfg.get("factor_weights", {})
    df["composite"] = composite_score(df, weights)
    # Entry gating uses the STRUCTURAL composite (momentum zeroed): the entry
    # timing wants flat-price accumulation phases, and momentum-in-the-gate
    # would systematically exclude exactly those. Momentum still feeds the ML
    # ranker (Pettersson's dominant predictor) and the full composite.
    structural_w = {k: (0.0 if k == "ma_deviation_momentum" else v) for k, v in weights.items()}
    df["structural_composite"] = composite_score(df, structural_w)
    return df
