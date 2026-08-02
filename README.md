# CS2 Quant — skin trading agents (BUFF163)

Paper-only trading research on the CS2 skin market. Two strategies run against
real BUFF price history and are compared in a local dashboard.

Nothing here is cleared to trade live. `execution.paper_mode` is `true` and the
go-live gate decides when that can change.

---

## Running the dashboard

```bash
make b-dashboard        # -> http://localhost:5173/
```

That installs deps (bun if present, otherwise npm) and starts Vite. Or by hand:

```bash
cd system_b/dashboard
npm install
npm run dev
```

No API keys, no `.env`, no database. The two runs it displays are **bundled as
JSON in the repo**, so a fresh clone works offline.

Two tabs in the top bar switch strategies: **positional** and **greedy**. Each
shows the equity curve, market regime and deployment, P&L by exit rule, and a
**trade blotter** — every closed lot with item, quantity, buy and sell dates,
buy and sell prices, and two deltas. `Δ price` is the gross move a chart would
show; `Δ net` is what the book actually received after the 1.5% fee and
slippage. The gap between those columns is the round-trip cost floor, and it is
why a positive price move can still be a losing trade.

To inspect a different backtest, drag a `runs/<stamp>/` folder's files onto the
page, or bundle it permanently:

```bash
cd system_b/dashboard
node scripts/make-sample.mjs ../runs/<stamp> "label" sample_greedy_tuned.json
```

---

## The two strategies

Both consume the same panel and the same execution model: decisions at day *t*
fill at *t+1* against the observed book, capped by depth and by a share of that
day's volume, with the 1.5% BUFF sell fee, slippage, and the **T+7 trade lock**
all modeled.

### Positional — structural selection, patient holds

Buys items that look structurally cheap and waits. Built by Ivan.

**Entry** is a funnel: hard liquidity/safety filters → a structural composite
score → **at least 2 of 3 accumulation signals** (sideways price with a
shrinking float, volume without price, resilience) → a walk-forward XGBoost
ranker orders the survivors → staged entry in 4 batches, adding only at −10%
support steps.

**Exit** uses brackets: take profit at +10% (half the lot) and +15% (all of it),
cut at −10%, liquidate at −18%, plus regime and thesis-break exits.

**Result** on the 97-item panel: 18 trades, **77.8% win, +6.56% average net per
trade**, +0.48% total, gate PASS.

**Its problem is that it almost never trades.** The `≥2 accumulation signals`
gate removes **96.2%** of everything reaching it (12,413 item-days → 475), and
the risk gate vetoed only 18 orders in 761 days — so selection, not sizing, is
the throttle. The book sits in cash 79% of days and deploys ~0.6% on average.
A genuinely good per-trade edge is being applied to almost no capital.

### Greedy — cheap-book momentum with a trailing ratchet

Buys broadly and manages the exit mechanically. Entry conditions were selected
by an out-of-sample search (below).

**Entry:** spread ≤ 1.0% · price above the 20-day middle Bollinger band · ≥3
valid bids near market. No structural scoring, no model.

**Exit:** the ratchet arms once the position is +20% (ask-side) and then tracks
a high-water return; giving back the trail from that high sells the whole
position. A wide −20% stop applies only before the ratchet arms. After either
exit the item goes on a watch list at the exit price and is re-bought if price
dips below and returns to it, bounded by a re-entry cap and cooldown.

Because of the T+7 lock it cannot "sell immediately" — the high-water mark
tracks *through* the lock and the exit fires at the first unlocked cycle where
it still holds. `lock_blocked_exits` counts the deferrals.

**Result** on the clean 97-item panel: 78 trades, **60.3% win, median trade
+8.60% net** (+10.25% gross, before the fee), +4.86% total, Sharpe 0.61, max drawdown −5.73%, profit factor 1.77,
deployment 15–20% continuous. Positive in both sample halves (pre 60.8% win,
post 59.3%).

### Why the spread cap is the whole strategy

A "+12% take profit / −10% cut" bracket looks symmetric and is not, once it
meets the venue. Triggers are measured ask-side but exits fill bid-side and pay
the fee:

| spread | winner nets | loser nets | break-even win rate |
|---|---|---|---|
| 3.4% (panel median) | +5.5% | −15.2% | **73%** — unreachable |
| **1.0% (tight)** | **+10.5%** | **−11.6%** | **~53%** — reachable |

Refusing to trade wide books is what makes every other factor start working.
This is arithmetic, not a fitted pattern, which is why it is trusted more than
anything else in this repo. Full write-up: `system_b/docs/EXIT_COST_FLOOR.md`.

---

## Where the data comes from

**Price history: the iflow.work Datadump Priority Archive** — a free, keyless
archive of twice-daily BUFF163 snapshots. `shared_b/vendors/iflow_archive.py`
merges snapshots into daily bars: best ask, best bid, 10-level ladders, listing
and bid counts.

```bash
make b-panel-archive    # builds data/panel_iflow97 from the archive
```

Verified properties, each of which has bitten us:

1. **Prices are CNY, not USD.** An older docstring says otherwise and is wrong.
2. **BUFF bids only exist from 2024-02-13.** Earlier bars would need fabricated
   bids, which would corrupt every exit fill and mark.
3. **`volume` is a Steam 24h-sold proxy.** The archive has no BUFF executed
   volume at all, so volume thresholds calibrated for BUFF read tight.
4. **Coverage flickers** — roughly 2.5–3.5k items tracked per snapshot. Missing
   days stay missing; anything over 3 days old is treated as stale.
5. **~18% of item-days had a crossed book** (bid above ask), because the
   archive's bid field goes stale for weeks while the ask keeps updating. Raw
   example, AK-47 Redline FT in May 2024: ask ladder `[128.00, 128.30, 129.00]`
   against a frozen bid ladder `[248.00, 247.00, 246.00]`. A backtest could
   sell higher than it bought. Median spreads stay positive (~2%), so a
   median-based screen cannot see it. `--max-crossed-bid 0.0` drops those rows;
   it is opt-in so existing panels reproduce exactly.

**Universe:** `config/universe_b_draft.yaml`, 97 items screened from 826 archive
snapshots for coverage, price, spread, volume and bid depth. Its
`supply`, `case_price_cny` and `aesthetics` fields are still **placeholders** —
human inputs per `HANDOFF.md` §B — so structural gates on those items are not
yet meaningful.

---

## How the market knowledge base was built

Three layers, with an explicit precedence order.

**1. Practitioner notes (PRIMARY).** `system_b/research/crash course.txt` — a
Chinese BUFF trading course covering Bollinger band rules, volume-profile
(筹码) cost distribution, left- vs right-side entry, layered position sizing,
item selection (supply 10k–30k, case price ≥¥80, ≥3 valid bids), volume-price
relationships, and bull/bear/sideways identification. These are venue-specific
and take precedence in live trading.

**2. Academic papers (SECONDARY).** Corroboration and a backlog to test, indexed
in `docs/RESEARCH_INDEX.md`:

- **Nikolaenko (2025)** — ARMA-GARCH on two BUFF163 rifles. Returns are
  stationary but barely predictable with very heavy tails; **volatility is
  forecastable** (GARCH α+β ≈ 0.82–0.91); structural breaks land on game
  updates. Drove volatility targeting, fat-tail risk caps, and the CUSUM break
  alarm.
- **Pettersson (2025)** — ML on 640k Steam observations. **Trees beat LSTM**
  (RF R² ≈ 0.49 vs LSTM 0.18), and the dominant predictor is deviation from the
  7-day moving average. Drove the XGBoost ranker and the momentum feature.

**3. Measured on our own data.** Every claim above was re-tested on the panel
rather than assumed, in `system_b/research/`. What held and what didn't:

| claim from the notes | measured | verdict |
|---|---|---|
| Above middle band = strong; below = don't touch | 48.0% vs 43.9% win | **confirmed** |
| Lower band **+ volume spike**, not lower band alone | 48.4% vs 43.9% win | **confirmed**, worth 4.5 points |
| Upper band is a sell, not a buy | 45.1% win | **confirmed** |
| Bands narrowing = safer than widening | 47.9% vs 46.4% | confirmed, weak |
| "Listings down + volume up = good" | 34.5% win, −8.73% | **contradicted** — worst factor tested (small n) |
| "Price up on shrinking volume = sell" | 48.3% win | **contradicted** — among the better entries |

The precedence rule when a source conflicts with the data: the notes win in live
trading, the conflict gets logged, and we A/B it. Both contradictions above are
flags, not settled refutations — the samples are small.

---

## How a strategy is validated

A capital-constrained backtest closes only tens of trades, which cannot separate
signal from noise — that is exactly how an earlier exit tuning went wrong on 18
trades. So parameters are tested two ways:

- **`research/strategy_search.py`** opens a notional position on every item-day,
  giving tens of thousands of round trips per parameter set. Candidates are
  selected on a **train** half and reported on a held-out **test** half.
- **`run_backtest`** then answers the different question of what the book would
  actually have returned under real sizing, caps, fills and the T+7 lock.

Three gates, all mandatory:

1. **Out-of-sample selection** — the tuned number is never the headline.
2. **Positive in both sample halves.**
3. **Jackknife** — drop the 5 best trades. This is the check that caught a
   `price <= 200` filter showing +18.68% which was **83% one item** (AK-47 Neon
   Revolution) riding the October 2025 trade-up repricing. Remove its 5 best
   trades and it was −41 CNY.

Beware the 2024-02 → 2026-05 window: it contains the **2025-10-22 trade-up
announcement**, which repriced cheap trade-up fuel 10–25× in days. It is large
enough to carry an entire backtest, so a split that puts it wholly in one half
proves nothing.

---

## Future direction

**Unblock positional's capital — the lever is already in the codebase.** It has
the better per-trade edge (+5.56% net at 78% win on clean data) and cannot
deploy it. `research/entry_gate_variants.py` changes one thing at a time:

| variant | trades | win | avg net | total | idle days |
|---|---|---|---|---|---|
| baseline | 23 | 78.3% | +5.56% | +0.21% | 65% |
| `min_accumulation_signals` 2 → 1 | 165 | 68.5% | +3.25% | **−0.51%** | 17% |
| **`model_signal_substitution` ON** | 48 | **79.2%** | **+6.99%** | **+1.29%** | 39% |
| `batches_per_item` 4 → 2 | 23 | 78.3% | +5.56% | +0.21% | 65% |
| `max_new_positions_per_cycle` 3 → 8 | 23 | 78.3% | +5.56% | +0.21% | 65% |
| all three loosened together | 172 | 66.9% | +2.98% | **−0.66%** | 14% |

Ivan's own designed lever wins and is still off by default: letting a top-decile
forecast stand in for one of the two required signals **doubles the trades while
raising both the win rate and the per-trade edge**, and it is the only variant
that improves everything at once. It also finally puts the ranker's measured
cross-sectional edge (rank IC 0.137, p=0.0003) to work, which today never
reaches an order.

Two things this rules out. Simply loosening the gate to one signal **turns the
strategy negative** — more trades at a worse win rate is not progress. And the
sizing knobs change nothing at all (23 trades either way), confirming selection
is the throttle. Note the ceiling is still low: +1.29% total. Turning it on is
step one, not the answer.

**Fill in the human fields.** `supply`, `case_price_cny` and `aesthetics` for
the 97-item universe (HANDOFF §B). Until then the structural gates are inert and
the universe is effectively liquidity-screened only.

**Default the crossed-book guard on.** It is opt-in today so existing panels
reproduce; otherwise every future panel inherits ~18% unusable rows.

**Widen the greedy sample.** `spread ≤ 1%` is only ~2.9% of item-days, so 78
trades is thin. More history, or a second venue, would tell us whether 60.3% is
stable. The −20% stop is effectively "no stop" and has never been tested through
a sustained bear.

**Merge System A into System B.** The event layer is currently separate, yet the
single largest price move in System B's own backtest window is a System-A-class
event that B is blind to. One real coupling already exists:
`shared_b/vendors/iflow_archive.py` imports System A's `shared.iflow_history`,
so B is not standalone today.

**Buy real BUFF volume.** Every volume threshold currently reads against a Steam
proxy from a different venue.
