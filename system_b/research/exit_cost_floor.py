"""Corrected exit-mechanics probe + benchmark sanity check.

Fix vs v2: sell fills follow shared_b/execution.py exactly --
    px = max(bid * (1 - slippage), limit_price)
so the aggressive `urgency` limit on stops is a FLOOR that guarantees the
fill, it does not cost 5%. v2 overstated stop losses.
"""
import glob
import os
import numpy as np
import pandas as pd

PANEL = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Development/"
    "csQuantSystemA/system_b/var/panel_real"
)
SLIP, FEE = 0.005, 0.015

frames = {}
for p in sorted(glob.glob(os.path.join(PANEL, "*.csv"))):
    name = os.path.basename(p).replace(".csv", "")
    if name == "meta":
        continue
    df = pd.read_csv(p, parse_dates=["day"]).set_index("day").sort_index()
    if len(df) < 40:
        continue
    frames[name] = df.reindex(pd.date_range(df.index[0], df.index[-1], freq="D"))

print("=" * 82)
print("0. Benchmark sanity check — is +379% real or a data artifact?")
print("=" * 82)
rows = []
for name, df in frames.items():
    a = df["sell_price"].dropna()
    rows.append((name[:40], a.iloc[0], a.iloc[-1], a.iloc[-1] / a.iloc[0] - 1,
                 a.max() / a.min() - 1, len(a)))
rows.sort(key=lambda r: -r[3])
print(f"{'item':<42}{'first':>9}{'last':>9}{'tot ret':>10}{'max/min':>10}{'obs':>5}")
for r in rows:
    print(f"{r[0]:<42}{r[1]:>9.1f}{r[2]:>9.1f}{r[3]*100:>9.1f}%{r[4]*100:>9.1f}%{r[5]:>5}")
med = np.median([r[3] for r in rows])
print(f"\nMEDIAN item total return: {med*100:+.1f}%   "
      f"MEAN: {np.mean([r[3] for r in rows])*100:+.1f}%")
print("-> the mean is dragged by a few extreme movers; the median is the honest")
print("   read on 'what a typical item did' in this window.")
print()

print("=" * 82)
print("1. Exit mechanics: what an ASK-side trigger actually REALIZES")
print("=" * 82)
sp = []
for name, df in frames.items():
    a, b = df["sell_price"].to_numpy(float), df["buy_price"].to_numpy(float)
    m = np.isfinite(a) & np.isfinite(b) & (a > 0)
    sp.extend(((a[m] - b[m]) / a[m]).tolist())
s = float(np.median(sp))
print(f"median ask->bid spread: {s*100:.2f}%   fee {FEE*100:.2f}%   slip {SLIP*100:.2f}%")
print()
print(f"{'ASK-side trigger':>18} {'-> realized net':>16}   note")
for trig, note in [(0.15, "take_profit_full"), (0.10, "take_profit_trim (half lot)"),
                   (-0.02, "bear_cut_ret  IVAN"), (-0.05, "bear_cut_ret  prior"),
                   (-0.10, "stop_loss_cut"), (-0.18, "stop_liquidation")]:
    # entry paid ask*(1+slip); exit fills at bid*(1-slip) net of fee
    realized = ((1 + trig) * (1 - s) * (1 - SLIP) * (1 - FEE)) / (1 + SLIP) - 1
    print(f"{trig*100:>17.0f}% {realized*100:>15.2f}%   {note}")
print()
print("The spread+fee wedge is ~5.4%. A -2% trigger therefore realizes ~-7%:")
print("the ask ticking down 2% is INSIDE the 3.4% spread, i.e. noise, but the")
print("exit crystallizes a real ~7% loss on the WHOLE lot.")
print()

print("=" * 82)
print("2. bear_cut_ret -0.05 -> -0.02, with correct fills, whole-lot dumps")
print("=" * 82)
print()
for label, cut in [("IVAN  -0.02", -0.02), ("prior -0.05", -0.05),
                   ("        -0.10", -0.10)]:
    wins = losses = 0
    wpnl = lpnl = 0.0
    for name, df in frames.items():
        ask, bid = df["sell_price"], df["buy_price"]
        for t in df.index[:-40]:
            a0 = ask.get(t)
            if not np.isfinite(a0):
                continue
            entry = a0 * (1 + SLIP)
            for k in range(1, 40):
                tk = t + pd.Timedelta(days=k)
                ak, bk = ask.get(tk), bid.get(tk)
                if not (np.isfinite(ak) and np.isfinite(bk)):
                    continue
                ret = ak / entry - 1.0
                if ret >= 0.10:
                    net = (bk * (1 - SLIP) * (1 - FEE)) / entry - 1.0
                    wins += 1
                    wpnl += 0.5 * net            # HALF lot on a trim
                    break
                if ret <= cut:
                    net = (bk * (1 - SLIP) * (1 - FEE)) / entry - 1.0
                    losses += 1
                    lpnl += 1.0 * net            # WHOLE lot
                    break
    n = wins + losses
    print(f"{label}: {wins:>5} wins / {losses:>5} losses  "
          f"({100*wins/max(n,1):>2.0f}% win)   "
          f"avg win {wpnl/max(wins,1)*100:>+6.2f}%(x.5)  "
          f"avg loss {lpnl/max(losses,1)*100:>+7.2f}%(x1)  "
          f"NET/trip {(wpnl+lpnl)/max(n,1)*100:>+6.2f}%")
print()
print("Same universe, same fills, only the bear cut moves. Tightening the cut")
print("raises the LOSS COUNT while the per-loss size barely shrinks, because")
print("the spread wedge dominates a 2% trigger.")
