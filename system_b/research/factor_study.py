"""Factor dig: which crash-course signals actually predict a winning trade?

Grounded in `research/crash course.txt` rather than mined blindly, because a
factor with a mechanism behind it is far likelier to survive out of sample than
one found by scanning. Each factor below is a claim the notes make; this
measures whether the panel agrees.

  middle band     "above = strong, can hold; below = weak, DO NOT touch"  (S1)
  lower band+red  "hits lower band + red volume bar + stops falling = buy"  (S1)
  upper band+green"hits upper band + green bar = sell"                      (S1)
  bandwidth       "widening = trend accelerating; narrowing = oscillation"  (S1)
  listings/volume "listings down + volume up = good; up + down = bad"       (S6)
  weak rally      "price rises + volume shrinks -> sell immediately"        (S6)
  small items     "50-500 yuan, listings <=150, sideways <10%/10d"          (S8)
  supply/case/bids the 3 mandatory conditions                               (S5)
  spread          not from the notes - the venue's own cost floor

Exit policy is the notes' own: take profit at +12% (they say 10-15%), cut at
-10%, unconditional liquidation at -18%, T+7 lock modeled. Reported per factor:
WIN RATE first (the stated objective), then net return, because a high win rate
with fat losses still loses money.

    PYTHONPATH=src:../system_a/src python research/factor_study.py [PANEL]
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

PANEL = sys.argv[1] if len(sys.argv) > 1 else "data/panel_iflow97_clean"
SLIP, FEE = 0.005, 0.015
LOCK, MAX_HOLD = 7, 90
TP, CUT, LIQ = 0.12, -0.10, -0.18


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Crash-course indicators, all backward-looking."""
    ask = df["sell_price"]
    out = pd.DataFrame(index=df.index)
    out["ask"] = ask
    out["bid"] = df["buy_price"]
    out["spread"] = (ask - df["buy_price"]) / ask
    ma = ask.rolling(20, min_periods=15).mean()
    sd = ask.rolling(20, min_periods=15).std()
    out["mid"] = ma
    out["upper"] = ma + 2 * sd
    out["lower"] = ma - 2 * sd
    out["pct_b"] = (ask - out.lower) / (out.upper - out.lower).replace(0, np.nan)
    out["bandwidth"] = (out.upper - out.lower) / ma.replace(0, np.nan)
    out["bw_slope"] = out["bandwidth"].diff(5)
    out["above_mid"] = (ask > ma).astype(float)
    v = df["volume"]
    out["vol"] = v
    out["vol_ma20"] = v.rolling(20, min_periods=10).mean()
    out["vol_z"] = (v - out.vol_ma20) / v.rolling(20, min_periods=10).std().replace(0, np.nan)
    lc = df["listing_count"]
    out["listings"] = lc
    out["listing_slope10"] = lc.diff(10) / lc.shift(10).replace(0, np.nan)
    out["ret1"] = ask.pct_change()
    out["ret7"] = ask.pct_change(7)
    out["ret21"] = ask.pct_change(21)
    out["range10"] = (ask.rolling(10, min_periods=8).max()
                      / ask.rolling(10, min_periods=8).min() - 1)
    out["vbo"] = df["valid_buy_orders"]
    return out


def simulate(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for name, df in frames.items():
        ind = indicators(df)
        ask = ind["ask"].to_numpy(float)
        bid = ind["bid"].to_numpy(float)
        n = len(ask)
        cols = {c: ind[c].to_numpy(float) for c in ind.columns}
        for i in range(25, n - LOCK - 2):
            a0 = ask[i]
            if not np.isfinite(a0) or a0 <= 0 or not np.isfinite(cols["mid"][i]):
                continue
            entry = a0 * (1 + SLIP)
            exited = None
            for k in range(1, min(MAX_HOLD, n - i)):
                ak, bk = ask[i + k], bid[i + k]
                if not (np.isfinite(ak) and np.isfinite(bk)):
                    continue
                r = ak / entry - 1.0
                if k < LOCK:
                    continue
                if r <= LIQ:
                    exited = ("liquidate", bk, k); break
                if r <= CUT:
                    exited = ("cut", bk, k); break
                if r >= TP:
                    exited = ("take_profit", bk, k); break
            if exited is None:
                tail = bid[i + 1: i + min(MAX_HOLD, n - i)]
                tail = tail[np.isfinite(tail)]
                if not len(tail):
                    continue
                exited = ("horizon", tail[-1], MAX_HOLD)
            reason, bx, held = exited
            net = (bx * (1 - SLIP) * (1 - FEE)) / entry - 1.0
            rec = {"item": name, "day": df.index[i], "net": net,
                   "reason": reason, "held": held}
            for c in ("spread", "pct_b", "bandwidth", "bw_slope", "above_mid",
                      "vol_z", "listing_slope10", "listings", "ret1", "ret7",
                      "ret21", "range10", "vbo", "vol_ma20"):
                rec[c] = cols[c][i]
            rec["price"] = a0
            rows.append(rec)
    return pd.DataFrame(rows)


def report(d: pd.DataFrame, label: str, mask=None) -> dict | None:
    sub = d if mask is None else d[mask]
    if len(sub) < 150:
        return None
    return {"rule": label, "n": len(sub), "win": (sub.net > 0).mean(),
            "mean": sub.net.mean(), "median": sub.net.median()}


def main() -> None:
    frames = {}
    for p in sorted(glob.glob(os.path.join(PANEL, "*.csv"))):
        nm = os.path.basename(p)[:-4]
        if nm in ("meta", "coverage"):
            continue
        df = pd.read_csv(p, parse_dates=["day"]).set_index("day").sort_index()
        if len(df) < 120:
            continue
        frames[nm] = df.reindex(pd.date_range(df.index[0], df.index[-1], freq="D"))
    print(f"panel {PANEL}: {len(frames)} items")
    d = simulate(frames)
    d = d.replace([np.inf, -np.inf], np.nan)
    print(f"simulated round trips: {len(d):,}  "
          f"({d.day.min().date()} -> {d.day.max().date()})")
    base = (d.net > 0).mean()
    print(f"\nBASELINE buy-everything: win {base:.1%}  mean {d.net.mean():+.2%}  "
          f"median {d.net.median():+.2%}")
    print(f"exit policy: TP +{TP:.0%} / cut {CUT:.0%} / liq {LIQ:.0%}, T+{LOCK} lock\n")
    print("exit mix:", d.reason.value_counts().to_dict())

    # ------------------------------------------------ single-factor claims
    print("\n" + "=" * 92)
    print("CRASH-COURSE FACTORS, ONE AT A TIME  (sorted by win rate)")
    print("=" * 92)
    tests = {
        "S1 above middle band": d.above_mid > 0.5,
        "S1 BELOW middle band (notes say avoid)": d.above_mid < 0.5,
        "S1 lower band + volume spike (buy sig)": (d.pct_b < 0.15) & (d.vol_z > 0.5),
        "S1 lower band only, no volume": (d.pct_b < 0.15) & (d.vol_z <= 0.5),
        "S1 upper band (notes say sell)": d.pct_b > 0.85,
        "S1 bands narrowing (oscillation)": d.bw_slope < 0,
        "S1 bands widening (trend)": d.bw_slope > 0,
        "S6 listings down + volume up": (d.listing_slope10 < 0) & (d.vol_z > 0),
        "S6 listings up + volume down": (d.listing_slope10 > 0) & (d.vol_z < 0),
        "S6 weak rally (price up, vol down)": (d.ret7 > 0.03) & (d.vol_z < 0),
        "S6 price+volume rise together": (d.ret7 > 0.03) & (d.vol_z > 0),
        "S8 small item 50-500y": (d.price >= 50) & (d.price <= 500),
        "S8 low listings (<=150)": d.listings <= 150,
        "S8 sideways <10% over 10d": d.range10 < 0.10,
        "S5 >=3 valid buy orders": d.vbo >= 3,
        "venue tight spread <=2%": d.spread <= 0.02,
    }
    rows = [r for lbl, m in tests.items() if (r := report(d, lbl, m))]
    rows.append(report(d, "-- baseline (all) --"))
    t = pd.DataFrame([r for r in rows if r]).sort_values("win", ascending=False)
    t["win"] = t["win"].map("{:.1%}".format)
    t["mean"] = t["mean"].map("{:+.2%}".format)
    t["median"] = t["median"].map("{:+.2%}".format)
    print(t.to_string(index=False))
    d.to_pickle("/tmp/factor_rows.pkl")
    print("\nrows cached -> /tmp/factor_rows.pkl")


if __name__ == "__main__":
    main()
