"""What entry conditions make a GREEDY round trip profitable?

The greedy ratchet's exits are not the main problem — its ENTRY is. It currently
buys whatever is most liquid, which is close to random with respect to whether
the price will rise. With no entry edge, an exit rule can only lose to the
round-trip cost floor (~5.9% median: spread 3.4% + fee 1.5% + slippage 1%).

So: open a notional position on EVERY item-day, run the ratchet forward to its
exit, and then group the realized net return by features known AT ENTRY. Any
feature whose top bucket clears the cost floor is a candidate entry condition.

Everything here is strictly backward-looking at the entry day. The forward walk
models the T+7 lock, fee and slippage exactly as the strategy does.

    PYTHONPATH=src:../system_a/src python research/greedy_entry_edge.py [PANEL]
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

PANEL = sys.argv[1] if len(sys.argv) > 1 else "data/panel_iflow97_clean"
SLIP, FEE = 0.005, 0.015
LOCK, MAX_HOLD = 7, 120
ARM, GIVEBACK, STOP = 0.10, 0.01, 0.05


def load(panel_dir: str) -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted(glob.glob(os.path.join(panel_dir, "*.csv"))):
        name = os.path.basename(p)[:-4]
        if name in ("meta", "coverage"):
            continue
        df = pd.read_csv(p, parse_dates=["day"]).set_index("day").sort_index()
        if len(df) < 120:
            continue
        out[name] = df.reindex(pd.date_range(df.index[0], df.index[-1], freq="D"))
    return out


def simulate(frames):
    """One row per (item, entry-day): entry features + realized net return."""
    rows = []
    for name, df in frames.items():
        ask = df["sell_price"].to_numpy(float)
        bid = df["buy_price"].to_numpy(float)
        vol = df["volume"].to_numpy(float)
        vbo = df["valid_buy_orders"].to_numpy(float)
        n = len(ask)
        lr = np.full(n, np.nan)
        with np.errstate(all="ignore"):
            lr[1:] = np.log(ask[1:] / ask[:-1])

        for i in range(60, n - LOCK - 2):
            a0, b0 = ask[i], bid[i]
            if not (np.isfinite(a0) and np.isfinite(b0)) or a0 <= 0:
                continue
            win60 = ask[max(0, i - 60): i + 1]
            win60 = win60[np.isfinite(win60)]
            win20 = ask[max(0, i - 20): i + 1]
            win20 = win20[np.isfinite(win20)]
            if len(win60) < 30 or len(win20) < 10:
                continue
            r20 = lr[max(0, i - 20): i + 1]
            r20 = r20[np.isfinite(r20)]
            if len(r20) < 10:
                continue
            v20 = vol[max(0, i - 20): i + 1]
            v20 = v20[np.isfinite(v20)]
            v60 = vol[max(0, i - 60): i + 1]
            v60 = v60[np.isfinite(v60)]

            spread = (a0 - b0) / a0
            entry = a0 * (1 + SLIP)
            # --- forward walk: the greedy ratchet, T+7 lock modeled ---
            armed, hw, exited = False, 0.0, None
            for k in range(1, min(MAX_HOLD, n - i)):
                ak, bk = ask[i + k], bid[i + k]
                if not (np.isfinite(ak) and np.isfinite(bk)):
                    continue
                ret = ak / entry - 1.0
                hw = max(hw, ret)
                if not armed and ret >= ARM:
                    armed = True
                if k < LOCK:
                    continue
                if armed and ret <= hw - GIVEBACK:
                    exited = ("trail", bk, k)
                    break
                if not armed and ret <= -STOP:
                    exited = ("stop", bk, k)
                    break
            if exited is None:
                tail = bid[i + 1: i + min(MAX_HOLD, n - i)]
                tail = tail[np.isfinite(tail)]
                if not len(tail):
                    continue
                exited = ("horizon", tail[-1], MAX_HOLD)
            reason, bx, held = exited
            net = (bx * (1 - SLIP) * (1 - FEE)) / entry - 1.0

            rows.append({
                "item": name, "day": df.index[i], "net": net, "reason": reason,
                "held": held,
                "spread": spread,
                "mom20": a0 / win20[0] - 1.0,
                "mom60": a0 / win60[0] - 1.0,
                "vol20": float(np.std(r20)),
                "dd60": a0 / np.max(win60) - 1.0,          # 0 = at 60d high
                "volexp": (np.mean(v20) / np.mean(v60)) if len(v60) and np.mean(v60) > 0 else np.nan,
                "price": a0,
                "vbo": vbo[i] if np.isfinite(vbo[i]) else np.nan,
            })
    return pd.DataFrame(rows)


def by_bucket(d: pd.DataFrame, col: str, q: int = 5) -> pd.DataFrame:
    x = d[col].replace([np.inf, -np.inf], np.nan)
    if x.notna().sum() < 100:
        return pd.DataFrame()
    try:
        b = pd.qcut(x, q, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    g = d.groupby(b, observed=True)["net"]
    out = pd.DataFrame({
        "n": g.size(),
        "win": g.apply(lambda s: (s > 0).mean()),
        "mean": g.mean(),
        "median": g.median(),
    })
    return out


def main() -> None:
    frames = load(PANEL)
    print(f"panel {PANEL}: {len(frames)} items")
    d = simulate(frames)
    print(f"simulated round trips: {len(d):,}")
    span = (d.day.min().date(), d.day.max().date())
    print(f"entry days: {span[0]} -> {span[1]}")
    print(f"\nBASELINE (buy everything): win {100*(d.net>0).mean():.1f}%  "
          f"mean {d.net.mean()*100:+.2f}%  median {d.net.median()*100:+.2f}%")
    print(f"cost floor at median spread: "
          f"{(d.spread.median()+FEE+2*SLIP)*100:.2f}%\n")

    for col, why in [
        ("spread", "the cost floor is PER ITEM - a tight book needs a smaller move"),
        ("mom20", "20d momentum at entry"),
        ("mom60", "60d momentum at entry"),
        ("dd60", "distance below the 60d high (0 = at the high)"),
        ("vol20", "20d realized volatility"),
        ("volexp", "volume expansion (20d mean / 60d mean)"),
        ("vbo", "valid bid-ladder depth"),
        ("price", "price level"),
    ]:
        t = by_bucket(d, col)
        if t.empty:
            continue
        print("=" * 78)
        print(f"{col}  —  {why}")
        print("=" * 78)
        t2 = t.copy()
        t2["win"] = t2["win"].map("{:.1%}".format)
        t2["mean"] = t2["mean"].map("{:+.2%}".format)
        t2["median"] = t2["median"].map("{:+.2%}".format)
        print(t2.to_string())
        print()

    # ---- candidate composite rule, with an honest out-of-sample split -------
    print("=" * 78)
    print("CANDIDATE ENTRY RULES (split at the midpoint of the entry range)")
    print("=" * 78)
    mid = d.day.min() + (d.day.max() - d.day.min()) / 2
    cheap = d.price <= 200
    tight = d.spread <= 0.02
    rules = {
        "baseline (buy everything)": pd.Series(True, index=d.index),
        "tight book (spread <= 2%)": tight,
        "cheap only (price <= 200)": cheap,
        "mid band (price 120-210)": (d.price >= 120) & (d.price <= 210),
        "cheap + tight": cheap & tight,
        "cheap + tight + mom20>0": cheap & tight & (d.mom20 > 0),
        "cheap + tight + vol20 high": cheap & tight & (d.vol20 > d.vol20.median()),
        "cheap + tight + depth>=5": cheap & tight & (d.vbo >= 5),
        "mid band + tight": (d.price >= 120) & (d.price <= 210) & tight,
        "mid band + tight + mom20>0": (d.price >= 120) & (d.price <= 210) & tight & (d.mom20 > 0),
        "EXPENSIVE (price > 350) - control": d.price > 350,
    }
    print(f"{'rule':<34}{'n':>7}{'win':>8}{'mean':>9}{'median':>9}"
          f"{'pre':>9}{'post':>9}  both>0")
    for label, m in rules.items():
        sub = d[m]
        if len(sub) < 100:
            print(f"{label:<34}{len(sub):>7}   (too few)")
            continue
        pre, post = sub[sub.day < mid].net, sub[sub.day >= mid].net
        ok = "YES" if (len(pre) and len(post) and pre.mean() > 0 and post.mean() > 0) else "no"
        print(f"{label:<34}{len(sub):>7}{(sub.net>0).mean():>8.1%}"
              f"{sub.net.mean():>+9.2%}{sub.net.median():>+9.2%}"
              f"{pre.mean():>+9.2%}{post.mean():>+9.2%}  {ok}")
    print("\nA rule that is positive overall but negative in one half is fitted.")


if __name__ == "__main__":
    main()
