"""Indicator library — Shared §3 encoded as testable features.

All functions take a per-item daily DataFrame with the canonical panel columns
(data.PANEL_COLUMNS) and use only rows already in the frame (the caller passes
a no-lookahead PanelView slice). "price" = sell_price (BUFF lowest listing —
what the crash-course charts show).

Convention for volume-bar color (crash course §1): red bar = selling-pressure
day (price down), green bar = buying-pressure day (price up).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

EPS = 1e-12


# ---------------------------------------------------------------------------
# Bollinger bands (Shared §3.1)
# ---------------------------------------------------------------------------

def bollinger(price: pd.Series, window: int = 20, n_std: float = 2.0) -> pd.DataFrame:
    mid = price.rolling(window, min_periods=window // 2).mean()
    sd = price.rolling(window, min_periods=window // 2).std()
    upper = mid + n_std * sd
    lower = mid - n_std * sd
    pct_b = (price - lower) / (upper - lower + EPS)
    bandwidth = (upper - lower) / (mid + EPS)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "pct_b": pct_b,
            "bandwidth": bandwidth,
            "middle_band_side": np.sign(price - mid),
        }
    )


def bollinger_touch_signals(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """+1 = buy-side touch (lower band + red bar + price stopped falling),
    -1 = sell-side touch (upper band + green bar), 0 = none.  Shared §3.1."""
    price = df["sell_price"]
    bb = bollinger(price, window)
    ret = price.pct_change()
    red_bar = ret < 0
    green_bar = ret > 0
    touched_lower = (price <= bb["bb_lower"] * 1.01).rolling(3, min_periods=1).max() > 0
    # "price stops falling": today's drop is small after the plunge
    stabilizing = ret >= -0.005
    buy = touched_lower & red_bar.shift(1).fillna(False) & stabilizing
    at_upper = price >= bb["bb_upper"] * 0.99
    vol_z = zscore(df["volume"], 20)
    sell = at_upper & green_bar & (vol_z > 0.5)
    out = pd.Series(0, index=df.index, dtype=int)
    out[buy] = 1
    out[sell] = -1
    return out


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def zscore(s: pd.Series, window: int) -> pd.Series:
    m = s.rolling(window, min_periods=max(3, window // 3)).mean()
    sd = s.rolling(window, min_periods=max(3, window // 3)).std()
    return (s - m) / (sd + EPS)


def slope_per_day(s: pd.Series, window: int) -> float:
    """OLS slope of the last `window` values, normalized by the mean level
    (fractional change per day). NaN-safe; 0 if not enough data."""
    tail = s.tail(window).dropna()
    if len(tail) < max(4, window // 2):
        return 0.0
    y = tail.to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    denom = ((x - x.mean()) ** 2).sum()
    if denom < EPS:
        return 0.0
    beta = ((x - x.mean()) * (y - y.mean())).sum() / denom
    level = abs(y.mean())
    return float(beta / (level + EPS))


def ma_deviation(price: pd.Series, window: int = 7) -> pd.Series:
    """Deviation from N-day MA — Pettersson's single dominant predictor."""
    ma = price.rolling(window, min_periods=max(2, window // 2)).mean()
    return price / (ma + EPS) - 1.0


def log_return(price: pd.Series, periods: int = 1) -> pd.Series:
    return np.log(price / price.shift(periods))


# ---------------------------------------------------------------------------
# Volume-price relationship classifier (Shared §3.3 — the 5 patterns)
# ---------------------------------------------------------------------------

VP_MAIN_FORCE_EXIT = 1     # listings up + price down + high volume  -> bearish
VP_SHAKEOUT = 2            # price down + low volume                 -> often a setup
VP_HEALTHY_RISE = 3        # volume up + price up together           -> confirmation
VP_WEAK_RALLY = 4          # price up + volume down                  -> sell / never enter
VP_FLAT = 5                # sideways                                -> conditional


def volume_price_pattern(
    df: pd.DataFrame,
    window: int = 5,
    flat_thresh: float = 0.01,
    vol_z_hi: float = 0.5,
    vol_z_lo: float = -0.5,
) -> int:
    """Classify the last `window` days into one of the 5 crash-course states."""
    if len(df) < window + 20:
        return VP_FLAT
    price_chg = df["sell_price"].iloc[-1] / df["sell_price"].iloc[-window - 1] - 1.0
    listing_chg = slope_per_day(df["listing_count"], window) * window
    vz = zscore(df["volume"], 20).tail(window).mean()
    if abs(price_chg) <= flat_thresh:
        return VP_FLAT
    if price_chg < 0:
        if vz >= vol_z_hi and listing_chg > 0:
            return VP_MAIN_FORCE_EXIT
        return VP_SHAKEOUT
    # price rising
    if vz >= vol_z_lo:
        return VP_HEALTHY_RISE
    return VP_WEAK_RALLY


def listings_volume_rule(df: pd.DataFrame, window: int = 7) -> int:
    """Compact rule (Shared §3.3): listings down + volume up = +1 (good);
    listings up + volume down = -1 (bad); else 0."""
    if len(df) < window + 5:
        return 0
    l_slope = slope_per_day(df["listing_count"], window)
    v_slope = slope_per_day(df["volume"], window)
    if l_slope < 0 and v_slope > 0:
        return 1
    if l_slope > 0 and v_slope < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Volume Profile / cost-distribution proxy (Shared §3.2)
# ---------------------------------------------------------------------------

SHAPE_LOW_PEAK = "low_single_peak"      # accumulation done -> bullish
SHAPE_HIGH_PEAK = "high_single_peak"    # top-heavy -> high risk
SHAPE_DOUBLE = "double_peak"            # range
SHAPE_SCATTERED = "scattered"           # illiquid/crashing -> forbidden


@dataclass
class VolumeProfile:
    dense_zone_price: float     # main peak of the traded-volume-at-price histogram
    price_vs_zone: float        # (price - zone)/zone; >0 => zone is SUPPORT below
    drift: float                # fractional/day drift of the dense zone (up = accumulation)
    shape: str
    concentration: float        # share of volume in the main peak's bin neighborhood


def volume_profile(
    df: pd.DataFrame,
    window: int = 60,
    bins: int = 24,
    decay: float = 0.99,
) -> VolumeProfile | None:
    """Approximate holders' cost distribution from traded volume at price with
    exponential decay (older cost bases progressively turned over)."""
    tail = df.tail(window).dropna(subset=["sell_price", "volume"])
    if len(tail) < max(15, window // 3):
        return None
    price = tail["sell_price"].to_numpy(dtype=float)
    vol = tail["volume"].to_numpy(dtype=float).clip(min=0.0)
    if vol.sum() < EPS or price.max() - price.min() < EPS:
        return None
    w = vol * (decay ** np.arange(len(vol) - 1, -1, -1))
    hist, edges = np.histogram(price, bins=bins, weights=w)
    centers = (edges[:-1] + edges[1:]) / 2
    peak_i = int(np.argmax(hist))
    zone = float(centers[peak_i])
    total = hist.sum()
    lo, hi = max(0, peak_i - 1), min(bins, peak_i + 2)
    concentration = float(hist[lo:hi].sum() / (total + EPS))

    # drift: dense zone now vs. dense zone computed `shift` days ago
    shift = max(5, window // 6)
    prev = df.iloc[:-shift] if len(df) > shift else df
    prev_tail = prev.tail(window).dropna(subset=["sell_price", "volume"])
    drift = 0.0
    if len(prev_tail) >= max(15, window // 3):
        p2 = prev_tail["sell_price"].to_numpy(dtype=float)
        v2 = prev_tail["volume"].to_numpy(dtype=float).clip(min=0.0)
        if v2.sum() > EPS and p2.max() - p2.min() > EPS:
            w2 = v2 * (decay ** np.arange(len(v2) - 1, -1, -1))
            h2, e2 = np.histogram(p2, bins=bins, weights=w2)
            j = int(np.argmax(h2))
            zone_prev = float((e2[j] + e2[j + 1]) / 2)
            if zone_prev > EPS:
                drift = (zone - zone_prev) / zone_prev / shift

    # shape: peaks above 60% of max, position of main peak in price range
    thresh = 0.6 * hist.max()
    peaks = [
        i
        for i in range(len(hist))
        if hist[i] >= thresh
        and (i == 0 or hist[i] >= hist[i - 1])
        and (i == len(hist) - 1 or hist[i] >= hist[i + 1])
    ]
    rel_pos = (zone - price.min()) / (price.max() - price.min() + EPS)
    if concentration < 0.25 or len(peaks) >= 4:
        shape = SHAPE_SCATTERED
    elif len(peaks) >= 2 and (centers[peaks[-1]] - centers[peaks[0]]) / (zone + EPS) > 0.08:
        shape = SHAPE_DOUBLE
    elif rel_pos <= 0.45:
        shape = SHAPE_LOW_PEAK
    else:
        shape = SHAPE_HIGH_PEAK

    cur = float(tail["sell_price"].iloc[-1])
    return VolumeProfile(
        dense_zone_price=zone,
        price_vs_zone=(cur - zone) / (zone + EPS),
        drift=float(drift),
        shape=shape,
        concentration=concentration,
    )


# ---------------------------------------------------------------------------
# Whale-accumulation signals (Shared §3.4 / System B §3.2) — the entry timing
# ---------------------------------------------------------------------------

@dataclass
class AccumulationSignals:
    sideways_shrinking_float: bool   # S1 横盘缩量
    volume_no_price: bool            # S2 放量不涨
    resilient: bool                  # S3 逆势抗跌

    @property
    def count(self) -> int:
        return int(self.sideways_shrinking_float) + int(self.volume_no_price) + int(self.resilient)


def accumulation_signals(
    df: pd.DataFrame,
    market_return_5d: float | pd.Series,
    window: int = 20,
    flat_thresh: float = 0.05,
    listing_slope_thresh: float = -0.002,
    vol_z_thresh: float = 2.0,
    small_move: float = 0.02,
    market_down_thresh: float = -0.02,
    sticky_days: int = 7,
) -> AccumulationSignals:
    """The three detectors. S1 is a state; S2/S3 are event-days, so they count
    as "firing" if seen within the trailing `sticky_days` window — the crash
    course's "multiple signals simultaneously" means the same accumulation
    phase, not the same tick.

    `market_return_5d`: either the current broad-market 5d return (scalar) or a
    date-indexed series of rolling 5d market returns (enables sticky S3).
    """
    price = df["sell_price"]
    n = len(df)
    if n < window + 5:
        return AccumulationSignals(False, False, False)

    # S1 (state): flat price over `window` AND listing count trending down
    total_move = abs(price.iloc[-1] / price.iloc[-window] - 1.0)
    band = (price.tail(window).max() - price.tail(window).min()) / (price.tail(window).mean() + EPS)
    l_slope = slope_per_day(df["listing_count"], window)
    s1 = bool(total_move < flat_thresh and band < 2 * flat_thresh and l_slope < listing_slope_thresh)

    # S2 (sticky event): executed-volume spike on a day the price barely moved
    # (volume, never listings). Any such day within the sticky window counts.
    vz = zscore(df["volume"], 30)
    day_move = price.pct_change().abs().rolling(3, min_periods=1).sum()
    spike_flat = (vz > vol_z_thresh) & (day_move < small_move * 1.5)
    s2 = bool(spike_flat.tail(sticky_days).fillna(False).any())

    # S3 (sticky event): market down while the item held or rose (5d windows)
    item_r5 = price.pct_change(5)
    if isinstance(market_return_5d, pd.Series) and len(market_return_5d):
        mr5 = market_return_5d.reindex(df.index).ffill()
        resilient_day = (mr5 < market_down_thresh) & (item_r5 >= 0.0)
        s3 = bool(resilient_day.tail(sticky_days).fillna(False).any())
    else:
        mr_now = float(market_return_5d) if not isinstance(market_return_5d, pd.Series) else 0.0
        item_now = float(item_r5.iloc[-1]) if np.isfinite(item_r5.iloc[-1]) else 0.0
        s3 = bool(mr_now < market_down_thresh and item_now >= 0.0)

    return AccumulationSignals(s1, s2, s3)


# ---------------------------------------------------------------------------
# Late-stage pump detector (System B §8.1 blocklist / System A §4.2)
# ---------------------------------------------------------------------------

def pump_shape(
    df: pd.DataFrame,
    run_window: int = 10,
    run_thresh: float = 0.25,
    accel_ratio: float = 1.5,
    listing_collapse: float = -0.25,
) -> bool:
    """Parabolic price + collapsing listings = late-stage pump (一波流).
    Never buy; block-list. Hype spike from the bus is layered on by the caller."""
    if len(df) < run_window + 10:
        return False
    price = df["sell_price"]
    run = price.iloc[-1] / price.iloc[-run_window - 1] - 1.0
    if run < run_thresh:
        return False
    recent = price.iloc[-1] / price.iloc[-4] - 1.0        # last 3 days
    earlier = price.iloc[-4] / price.iloc[-run_window - 1] - 1.0
    per_day_recent = recent / 3
    per_day_earlier = earlier / max(1, run_window - 3)
    accelerating = per_day_recent > accel_ratio * max(per_day_earlier, 0.002)
    listings_chg = df["listing_count"].iloc[-1] / max(df["listing_count"].iloc[-run_window - 1], 1) - 1.0
    return bool(accelerating and listings_chg < listing_collapse)


# ---------------------------------------------------------------------------
# Steam-sale seasonality (RESEARCH_INDEX directive 8 — secondary, [new])
# ---------------------------------------------------------------------------

# Verified dates (steamdb, checked 2026-07). Liquidation pressure rises into
# sales; Autumn moved from late-Nov to early-Oct starting 2025.
STEAM_SALE_DATES = [
    ("2024-03-14", "2024-03-21"),
    ("2024-06-27", "2024-07-11"),
    ("2024-11-27", "2024-12-04"),
    ("2024-12-19", "2025-01-02"),
    ("2025-03-13", "2025-03-20"),
    ("2025-06-26", "2025-07-10"),
    ("2025-09-29", "2025-10-06"),
    ("2025-12-18", "2026-01-05"),
    ("2026-03-19", "2026-03-26"),
    ("2026-06-25", "2026-07-09"),
    ("2026-10-01", "2026-10-08"),
    ("2026-12-17", "2027-01-04"),
]
_SALE_RANGES = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in STEAM_SALE_DATES]

# fallback for years outside the verified table (synthetic data, far history)
_GENERIC_WINDOWS = [(6, 20, 7, 11), (12, 15, 1, 5), (9, 28, 10, 8)]


def in_steam_sale_window(day: pd.Timestamp) -> bool:
    day = pd.Timestamp(day).normalize()
    if _SALE_RANGES[0][0] <= day <= _SALE_RANGES[-1][1]:
        return any(a <= day <= b for a, b in _SALE_RANGES)
    for sm, sd, em, ed in _GENERIC_WINDOWS:
        start_ok = (day.month, day.day) >= (sm, sd)
        end_ok = (day.month, day.day) <= (em, ed)
        if (sm, sd) <= (em, ed):
            if start_ok and end_ok:
                return True
        else:  # wraps year end
            if start_ok or end_ok:
                return True
    return False
