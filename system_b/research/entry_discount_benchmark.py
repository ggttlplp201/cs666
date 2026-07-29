"""Probe Ivan's entry/exit tuning on the real archive panel — corrected.

v1 used positional row offsets, but archive coverage flickers, so "20 rows
ahead" was 30-40 calendar days ahead and the numbers were meaningless.
This version reindexes every item onto a calendar-day grid first.

Adds the decisive control: dip-buying returns MINUS the equal-weight panel
return over the identical window. If the excess is ~0, the tuned edge is
just long exposure to a rising market, not selection skill.
"""
import glob
import os
import numpy as np
import pandas as pd

PANEL = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Development/"
    "csQuantSystemA/system_b/var/panel_real"
)

frames = {}
for p in sorted(glob.glob(os.path.join(PANEL, "*.csv"))):
    name = os.path.basename(p).replace(".csv", "")
    if name == "meta":
        continue
    df = pd.read_csv(p, parse_dates=["day"]).set_index("day").sort_index()
    if len(df) < 40:
        continue
    # calendar grid: keep gaps as NaN so a k-day horizon is really k days
    full = pd.date_range(df.index[0], df.index[-1], freq="D")
    frames[name] = df.reindex(full)

span0 = min(d.index[0] for d in frames.values())
span1 = max(d.index[-1] for d in frames.values())
print(f"panel: {len(frames)} items, span {span0.date()} -> {span1.date()} "
      f"({(span1-span0).days} calendar days)")

# ---- panel benchmark: equal-weight ask index, calendar-aligned -------------
grid = pd.date_range(span0, span1, freq="D")
norm = pd.DataFrame(index=grid)
for name, df in frames.items():
    a = df["sell_price"].reindex(grid).ffill(limit=3)
    norm[name] = a / a.dropna().iloc[0]
bench = norm.mean(axis=1)
bench_tot = bench.dropna().iloc[-1] / bench.dropna().iloc[0] - 1
print(f"equal-weight panel ask return over the window: {bench_tot*100:+.1f}%")
print("^^ this window is a strong BULL market; every number below must be")
print("   read against it, not in isolation.")
print()

SLIP, FEE = 0.005, 0.015
HZ = [5, 10, 20]

print("=" * 82)
print("A. Below-ask entry: raw vs BENCHMARK-RELATIVE forward return (medians)")
print("=" * 82)
print(f"{'disc':>5} {'fills':>6} {'fill%':>6} " + "  ".join(
    f"{'H'+str(h)+' raw':>9} {'H'+str(h)+' vs bm':>10}" for h in HZ))

for d in [0.005, 0.010, 0.015, 0.020]:
    raw = {h: [] for h in HZ}
    exc = {h: [] for h in HZ}
    n_fill = n_opp = 0
    for name, df in frames.items():
        ask = df["sell_price"]
        for t in df.index[:-max(HZ) - 1]:
            a0, a1 = ask.get(t), ask.get(t + pd.Timedelta(days=1))
            if not (np.isfinite(a0) and np.isfinite(a1)):
                continue
            n_opp += 1
            if a1 > a0 * (1 - d):
                continue                       # his order would expire
            n_fill += 1
            px = min(a1 * (1 + SLIP), a0 * (1 - d))
            for h in HZ:
                ah = ask.get(t + pd.Timedelta(days=1 + h))
                b0 = bench.get(t + pd.Timedelta(days=1))
                bh = bench.get(t + pd.Timedelta(days=1 + h))
                if not (np.isfinite(ah) and np.isfinite(b0) and np.isfinite(bh)):
                    continue
                r = ah / px - 1.0
                raw[h].append(r)
                exc[h].append(r - (bh / b0 - 1.0))
    cells = []
    for h in HZ:
        cells.append(f"{np.median(raw[h])*100:>8.2f}% "
                     f"{np.median(exc[h])*100:>+9.2f}%")
    print(f"{d*100:>4.1f}% {n_fill:>6} {100*n_fill/max(n_opp,1):>5.1f}% "
          + "  ".join(cells))
print()
print("'vs bm' = median forward return MINUS the equal-weight panel's return")
print("over the same calendar window. That column is the actual selection edge.")
print()

print("=" * 82)
print("B. Exit asymmetry: half-lot trims at +10% vs whole-lot dumps at -2%")
print("=" * 82)
print()
print("Ivan's exits: take_profit_trim sells lot.qty//2 at +10%;")
print("bear_regime_cut and stops sell lot.qty (the DEFAULT sell_qty).")
print("Leon's removed `soft_exit_qty_pct` had scaled the soft exits too.")
print()
tp_lo, bear = 0.10, -0.02
for label, cut in [("Ivan  bear_cut_ret=-0.02", -0.02),
                   ("prior bear_cut_ret=-0.05", -0.05)]:
    wins = losses = 0
    wpnl = lpnl = 0.0
    for name, df in frames.items():
        ask = df["sell_price"]
        bid = df["buy_price"]
        for t in df.index[:-40]:
            a0 = ask.get(t)
            if not np.isfinite(a0):
                continue
            entry = a0 * (1 + SLIP)
            # walk forward to whichever bracket hits first
            for k in range(1, 40):
                tk = t + pd.Timedelta(days=k)
                ak, bk = ask.get(tk), bid.get(tk)
                if not (np.isfinite(ak) and np.isfinite(bk)):
                    continue
                ret = ak / entry - 1.0
                if ret >= tp_lo:
                    # trigger ASK-side, FILL bid-side, half the lot, pay fee
                    net = (bk * (1 - SLIP) * (1 - FEE)) / entry - 1.0
                    wins += 1
                    wpnl += 0.5 * net
                    break
                if ret <= cut:
                    net = (bk * (1 - 0.05) * (1 - FEE)) / entry - 1.0
                    losses += 1
                    lpnl += 1.0 * net          # WHOLE lot
                    break
    n = wins + losses
    if n:
        print(f"{label}:  {wins} wins / {losses} losses "
              f"({100*wins/n:.0f}% win rate)")
        print(f"    sum qty-weighted win pnl  {wpnl*100:>+8.1f}%  "
              f"(avg {wpnl/max(wins,1)*100:+.2f}% x 0.5 lot)")
        print(f"    sum qty-weighted loss pnl {lpnl*100:>+8.1f}%  "
              f"(avg {lpnl/max(losses,1)*100:+.2f}% x 1.0 lot)")
        print(f"    NET per round trip        {(wpnl+lpnl)/n*100:>+8.2f}%")
        print()

print("=" * 82)
print("C. The trigger-vs-fill side gap that the +10%/-2% brackets ignore")
print("=" * 82)
sp = []
for name, df in frames.items():
    a = df["sell_price"].to_numpy(float)
    b = df["buy_price"].to_numpy(float)
    m = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    sp.extend(((a[m] - b[m]) / a[m]).tolist())
sp = np.array(sp)
print(f"median ask->bid spread {np.median(sp)*100:>6.2f}%   "
      f"mean {np.mean(sp)*100:.2f}%   p75 {np.percentile(sp,75)*100:.2f}%")
print(f"round-trip floor = spread {np.median(sp)*100:.2f}% + fee {FEE*100:.2f}%"
      f" + 2x slip {2*SLIP*100:.2f}% = {(np.median(sp)+FEE+2*SLIP)*100:.2f}%")
print()
print(f"A +10% ASK-side take-profit nets roughly "
      f"{(0.10 - np.median(sp) - FEE - 2*SLIP)*100:.2f}% after crossing to the bid.")
print(f"A -2% ASK-side bear cut realizes roughly "
      f"{(-0.02 - np.median(sp) - FEE - 0.05 - SLIP)*100:.2f}% "
      "(urgency prices stops 5% under the bid).")
