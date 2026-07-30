"""Greedy ratchet parameter sweep with real statistical power.

A full backtest of the greedy strategy closes only ~25-30 trades on a 6-month
panel, because position caps, per-item allocation and capital limits throttle
it. Twenty-five trades cannot distinguish a real effect from noise — that is
exactly the trap the n=18 iflow-97 tuning fell into (docs/EXIT_COST_FLOOR.md §4).

So this probe isolates the ratchet MECHANISM instead: it opens a notional
position on EVERY item-day and runs the ratchet forward to its exit, which
yields thousands of round trips per parameter set. It deliberately ignores
sizing, capital and concurrency — it answers "do these thresholds extract money
from this price series", not "what would the book have returned".

Models honestly:
  - entry at ask * (1 + slippage)
  - the T+7 trade lock (no exit can fire before unlock_day)
  - exit at bid * (1 - slippage) * (1 - fee)
  - ask-side triggers, matching the strategy
  - a max holding horizon, after which the position is marked out at the bid

    PYTHONPATH=src:../system_a/src python research/greedy_sweep.py [PANEL_DIR]
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

SLIP, FEE = 0.005, 0.015
LOCK_DAYS = 7
MAX_HOLD = 120

PANEL = sys.argv[1] if len(sys.argv) > 1 else "var/panel_real"


def load(panel_dir: str) -> dict[str, pd.DataFrame]:
    frames = {}
    for p in sorted(glob.glob(os.path.join(panel_dir, "*.csv"))):
        name = os.path.basename(p)[:-4]
        if name in ("meta", "coverage"):
            continue
        df = pd.read_csv(p, parse_dates=["day"]).set_index("day").sort_index()
        if len(df) < 40:
            continue
        # calendar grid so "7 days locked" really means 7 days
        frames[name] = df.reindex(pd.date_range(df.index[0], df.index[-1], freq="D"))
    return frames


def run_one(frames, arm, giveback, stop, spread_aware, split_at=None):
    """Every item-day is an entry. Returns per-trade net returns."""
    out, out_pre, out_post = [], [], []
    reasons = {"trail": 0, "stop": 0, "horizon": 0}
    for name, df in frames.items():
        ask = df["sell_price"].to_numpy(float)
        bid = df["buy_price"].to_numpy(float)
        idx = df.index
        n = len(ask)
        for i in range(n - LOCK_DAYS - 2):
            a0 = ask[i]
            if not np.isfinite(a0) or a0 <= 0:
                continue
            b0 = bid[i]
            spread = (a0 - b0) / a0 if np.isfinite(b0) and a0 > 0 else 0.0
            if spread_aware:
                gb = max(giveback, spread)
                st = max(stop, spread + FEE + 2 * SLIP)
            else:
                gb, st = giveback, stop
            entry = a0 * (1 + SLIP)
            armed, hw, exited = False, 0.0, None
            for k in range(1, min(MAX_HOLD, n - i)):
                ak, bk = ask[i + k], bid[i + k]
                if not (np.isfinite(ak) and np.isfinite(bk)):
                    continue
                ret = ak / entry - 1.0
                hw = max(hw, ret)
                if not armed and ret >= arm:
                    armed = True
                if k < LOCK_DAYS:
                    continue                      # T+7: cannot exit yet
                if armed and ret <= hw - gb:
                    exited = ("trail", bk)
                    break
                if not armed and ret <= -st:
                    exited = ("stop", bk)
                    break
            if exited is None:
                # mark out at the last observable bid inside the horizon
                tail = bid[i + 1: i + min(MAX_HOLD, n - i)]
                tail = tail[np.isfinite(tail)]
                if not len(tail):
                    continue
                exited = ("horizon", tail[-1])
            reason, bx = exited
            reasons[reason] += 1
            net = (bx * (1 - SLIP) * (1 - FEE)) / entry - 1.0
            out.append(net)
            if split_at is not None:
                (out_pre if idx[i] < split_at else out_post).append(net)
    return np.array(out), np.array(out_pre), np.array(out_post), reasons


def summarize(label, r, reasons=None):
    if not len(r):
        print(f"{label:<34} (no trades)")
        return
    wins = (r > 0).sum()
    print(f"{label:<34} n={len(r):>6}  win={100*wins/len(r):>5.1f}%  "
          f"mean={r.mean()*100:>+7.2f}%  median={np.median(r)*100:>+7.2f}%  "
          f"tot={r.sum()*100:>+9.1f}%", end="")
    if reasons:
        print(f"  [trail {reasons['trail']} / stop {reasons['stop']} "
              f"/ horizon {reasons['horizon']}]")
    else:
        print()


def main():
    frames = load(PANEL)
    if not frames:
        raise SystemExit(f"no item CSVs in {PANEL}")
    span0 = min(d.index[0] for d in frames.values())
    span1 = max(d.index[-1] for d in frames.values())
    split = span0 + (span1 - span0) / 2
    print(f"panel {PANEL}: {len(frames)} items, {span0.date()} -> {span1.date()}")
    print(f"T+7 lock modeled, max hold {MAX_HOLD}d, "
          f"fee {FEE:.1%}, slippage {SLIP:.1%} each way")
    print(f"sample split at {split.date()} (pre / post halves shown for the finalists)")
    print()

    # ---- 1. the spec, literal vs spread-aware ----------------------------
    print("=" * 100)
    print("1. Leon's spec: arm +10%, trail 1 point, stop -5%")
    print("=" * 100)
    for aware in (False, True):
        r, pre, post, reasons = run_one(frames, 0.10, 0.01, 0.05, aware, split)
        summarize("literal" if not aware else "spread-aware", r, reasons)
        summarize("    pre-split", pre)
        summarize("    post-split", post)
        print()

    # ---- 2. sweep the giveback ------------------------------------------
    print("=" * 100)
    print("2. Trail giveback (arm +10%, stop -5%, literal thresholds)")
    print("=" * 100)
    for gb in (0.01, 0.02, 0.03, 0.05, 0.08):
        r, _, _, reasons = run_one(frames, 0.10, gb, 0.05, False)
        summarize(f"giveback {gb:.0%}", r, reasons)
    print()

    # ---- 3. sweep the stop ----------------------------------------------
    print("=" * 100)
    print("3. Hard stop (arm +10%, trail 1 point, literal thresholds)")
    print("=" * 100)
    for st in (0.03, 0.05, 0.08, 0.10, 0.15, 0.20):
        r, _, _, reasons = run_one(frames, 0.10, 0.01, st, False)
        summarize(f"stop -{st:.0%}", r, reasons)
    print()

    # ---- 4. sweep the arm threshold -------------------------------------
    print("=" * 100)
    print("4. Arm threshold (trail 1 point, stop -5%, literal thresholds)")
    print("=" * 100)
    for arm in (0.05, 0.10, 0.15, 0.20, 0.30):
        r, _, _, reasons = run_one(frames, arm, 0.01, 0.05, False)
        summarize(f"arm +{arm:.0%}", r, reasons)
    print()

    # ---- 5. best-of grid, with the split as the honesty check -----------
    print("=" * 100)
    print("5. Grid — ranked by mean net/trade, both halves shown")
    print("=" * 100)
    rows = []
    for arm in (0.05, 0.10, 0.15, 0.20):
        for gb in (0.01, 0.02, 0.03, 0.05):
            for st in (0.05, 0.10, 0.15):
                r, pre, post, _ = run_one(frames, arm, gb, st, False, split)
                if len(r) < 50:
                    continue
                rows.append((r.mean(), arm, gb, st, len(r), (r > 0).mean(),
                             pre.mean() if len(pre) else float("nan"),
                             post.mean() if len(post) else float("nan")))
    rows.sort(reverse=True)
    print(f"{'arm':>5} {'trail':>6} {'stop':>6} {'n':>6} {'win':>7} "
          f"{'mean':>8} {'pre':>8} {'post':>8}  both>0")
    for mean, arm, gb, st, n, win, pre, post in rows[:12]:
        ok = "YES" if (pre > 0 and post > 0) else "no"
        print(f"{arm:>5.0%} {gb:>6.0%} {st:>6.0%} {n:>6} {win:>6.1%} "
              f"{mean*100:>+7.2f}% {pre*100:>+7.2f}% {post*100:>+7.2f}%  {ok}")
    print()
    print("A parameter set that is positive overall but negative in one half is")
    print("fitted to the other half. Both>0 is the minimum bar, and even that is")
    print("weak evidence when the halves are not independent regimes.")


if __name__ == "__main__":
    main()
