"""Out-of-sample parameter search for a high-win-rate strategy.

The factor study showed why buy-everything loses: with a ~5.9% round-trip cost
floor, a "+12% take profit / -10% cut" bracket is not symmetric in REALIZED
terms. At the median 3.4% spread a +12% winner nets about +5.5% while a -10%
loser realizes about -15.2%, so break-even needs a ~73% win rate. Nothing gets
there.

The dominant lever is therefore the SPREAD, because it sets the cost floor per
item. At a 1.5% spread with a +15% target and a -8% cut, break-even falls to
about 53% - which factors can plausibly reach.

Selection discipline (this is the point of the file):
  * the grid is split TRAIN / TEST by time; the winner is chosen on TRAIN and
    reported on TEST. The tuned number is never the headline.
  * a candidate must be positive in BOTH halves.
  * a candidate must survive a JACKKNIFE that drops its 5 best trades - the
    check that caught an earlier "+18.7%" that was one item in one event.

    PYTHONPATH=src:../system_a/src python research/strategy_search.py
"""

from __future__ import annotations

import glob
import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from factor_study import indicators  # noqa: E402

PANEL = sys.argv[1] if len(sys.argv) > 1 else "data/panel_iflow97_clean"
SLIP, FEE = 0.005, 0.015
LOCK, MAX_HOLD = 7, 90

TPS = [0.10, 0.15, 0.20, 0.30]
CUTS = [-0.06, -0.08, -0.12, -0.20]


def load(panel_dir):
    frames = {}
    for p in sorted(glob.glob(os.path.join(panel_dir, "*.csv"))):
        nm = os.path.basename(p)[:-4]
        if nm in ("meta", "coverage"):
            continue
        df = pd.read_csv(p, parse_dates=["day"]).set_index("day").sort_index()
        if len(df) < 120:
            continue
        frames[nm] = df.reindex(pd.date_range(df.index[0], df.index[-1], freq="D"))
    return frames


def simulate(frames, tp, cut):
    """Forward-walk every item-day under one exit bracket."""
    liq = min(cut * 2, -0.18)
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
                if r <= liq:
                    exited = (bk,); break
                if r <= cut:
                    exited = (bk,); break
                if r >= tp:
                    exited = (bk,); break
            if exited is None:
                tail = bid[i + 1: i + min(MAX_HOLD, n - i)]
                tail = tail[np.isfinite(tail)]
                if not len(tail):
                    continue
                exited = (tail[-1],)
            net = (exited[0] * (1 - SLIP) * (1 - FEE)) / entry - 1.0
            rec = {"day": df.index[i], "net": net, "price": a0}
            for c in ("spread", "pct_b", "above_mid", "vol_z", "bw_slope",
                      "vbo", "range10", "ret7"):
                rec[c] = cols[c][i]
            rows.append(rec)
    return pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan)


FACTORS = {
    "any": lambda d: pd.Series(True, index=d.index),
    "above_mid": lambda d: d.above_mid > 0.5,
    "above_mid+depth": lambda d: (d.above_mid > 0.5) & (d.vbo >= 3),
    "lowband+vol": lambda d: (d.pct_b < 0.15) & (d.vol_z > 0.5),
    "above_mid+narrowing": lambda d: (d.above_mid > 0.5) & (d.bw_slope < 0),
    "above_mid+sideways": lambda d: (d.above_mid > 0.5) & (d.range10 < 0.10),
    "above_mid+depth+narrow": lambda d: (d.above_mid > 0.5) & (d.vbo >= 3) & (d.bw_slope < 0),
}
SPREADS = [0.010, 0.015, 0.020, 0.99]


def evaluate(d, mask, split):
    sub = d[mask].dropna(subset=["net"])
    if len(sub) < 200:
        return None
    tr, te = sub[sub.day < split], sub[sub.day >= split]
    if len(tr) < 100 or len(te) < 100:
        return None
    jack = sub.sort_values("net", ascending=False).iloc[5:]
    return {
        "n": len(sub),
        "win": (sub.net > 0).mean(),
        "mean": sub.net.mean(),
        "median": sub.net.median(),
        "train_mean": tr.net.mean(),
        "test_mean": te.net.mean(),
        "test_win": (te.net > 0).mean(),
        "jack_mean": jack.net.mean(),
        "both_pos": bool(tr.net.mean() > 0 and te.net.mean() > 0),
        "jack_ok": bool(jack.net.mean() > 0),
    }


def main():
    frames = load(PANEL)
    print(f"panel {PANEL}: {len(frames)} items")
    probe = simulate(frames, 0.15, -0.08)
    split = probe.day.min() + (probe.day.max() - probe.day.min()) / 2
    print(f"TRAIN {probe.day.min().date()} -> {split.date()}   "
          f"TEST {split.date()} -> {probe.day.max().date()}\n")

    results = []
    for tp, cut in itertools.product(TPS, CUTS):
        d = probe if (tp, cut) == (0.15, -0.08) else simulate(frames, tp, cut)
        print(f"  simulated tp={tp:.0%} cut={cut:.0%}", flush=True)
        for sname, smax in [(f"spread<={s:.1%}" if s < 0.9 else "spread any", s)
                            for s in SPREADS]:
            base = d.spread <= smax
            for fname, fn in FACTORS.items():
                r = evaluate(d, base & fn(d), split)
                if r:
                    results.append({"tp": tp, "cut": cut, "spread": sname,
                                    "factor": fname, **r})
    res = pd.DataFrame(results)
    res.to_pickle("/tmp/strategy_search.pkl")

    print("\n" + "=" * 108)
    print("SELECTED ON *TRAIN* ONLY, RANKED BY TRAIN WIN RATE — test columns are held out")
    print("=" * 108)
    ok = res[res.both_pos & res.jack_ok].copy()
    if ok.empty:
        print("NOTHING passed both-halves-positive AND the jackknife.")
        print("Closest by test_mean:")
        ok = res.nlargest(12, "test_mean")
    else:
        ok = ok.nlargest(15, "win")
    show = ok[["tp", "cut", "spread", "factor", "n", "win", "test_win",
               "mean", "test_mean", "jack_mean", "both_pos", "jack_ok"]]
    for c in ("win", "test_win"):
        show[c] = show[c].map("{:.1%}".format)
    for c in ("mean", "test_mean", "jack_mean"):
        show[c] = show[c].map("{:+.2%}".format)
    print(show.to_string(index=False))
    print(f"\ncandidates evaluated: {len(res):,}   passing both gates: "
          f"{int((res.both_pos & res.jack_ok).sum()):,}")
    print("cached -> /tmp/strategy_search.pkl")


if __name__ == "__main__":
    main()
