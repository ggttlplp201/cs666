# System B — Positional Value / Trend Trading Agent (CS2 · BUFF)

> **Owner:** [Builder 2]
> **Scope:** Identify undervalued / accumulation-phase items via structural factors (supply, float, circulation, meta, aesthetics) and hold them over weeks–months. Alpha source = *factor edge + patience*, not information speed.
> **Sibling system:** System A (Event-Driven/Reactive). Shares the data layer and normalization schema; runs independently.

---

## 0. One-paragraph thesis

Most items are mispriced slowly, not instantly. A "good" item — right supply outlook, right existing quantity (~2,000–10,000 FN for a "meaningful" item), healthy circulation (≥~10 daily trades), a meta or secondary-meta weapon, and decent aesthetics — tends to be accumulated by market makers *before* it moves. This system scores the whole catalogue on those factors, watches for the accumulation signatures (shrinking float on flat price, volume without price rise, resilience to bad news), positions **ahead** of the pump, and holds through the T+7 lock with room to spare. It does **not** try to win latency races and does **not** need a real-time leak monitor as a core input.

---

## 1. Market/platform assumptions

- **Primary venue:** BUFF163. Fees ≈ 2.5%, CNY, Chinese account required.
- **Cooldown:** ~7-day (T+7) lock per purchase. For a multi-week holding horizon this is minor friction rather than a binding constraint — one of the reasons the positional approach is more tractable than the reactive one.
- **Long-only**, thin per-item books — same as System A.

---

## 2. Data layer (shared with System A)

### 2.1 Data API ≠ execution API
The pricing providers give you *what to buy*; they do **not** place orders. Automated execution is a separate layer (§6). See System A §2 for the full write-up — same conclusion applies.

### 2.2 Provider recommendation

> ⚠ **Tier caveat (Shared §2a, verified 2026-07):** the bullets below describe
> cs2.sh's *Scale* tier. Our current Developer tier has bid/ask + depth counts
> only, USD-normalized — no executed volume, float, or archive. System B's
> volume-dependent factors (circulation, Signal 2) are Phase-2 capabilities;
> Phase-1 backtesting uses the free Steam pricehistory source instead.

Same stack as System A:
- **Primary live feed: cs2.sh (SELECTED)** — native BUFF listing prices, buy orders, volume every few minutes, float/fade ranges, OHLC (5m–1d), 3+ yr archive. Critical here because *every* System B factor needs volume/depth/float, not just price.
- **History/backtest: csmarketapi** (11+ yr sales history, 10+ markets, item metadata, no Steam rate limits) — *verify it exposes BUFF per-item volume + listing counts + buy orders before relying on it for signals*; otherwise use it purely for the long historical series. **pricempire** (5 yr, 56 markets) is an equally good history source.
- **Redundancy:** cspriceapi / steamwebapi for cross-market divergence.

Because this system's holding horizon is long, **history depth matters more than update frequency** — prioritize the provider with the cleanest multi-year OHLC + volume.

### 2.3 Normalization schema
Identical to System A §2.3 (import the shared module). You additionally lean on `float_range`, `buff_listing_count`, `buff_buy_order_count`, and `buff_volume_24h` for the factor model.

---

## 3. Factor model — quantifying the selection criteria

> **Grounding:** the concrete thresholds, indicators, and selection logic below come from the **Shared Market-Fundamentals & Indicator Library** (the "crash course" knowledge layer) — see that doc for full definitions of Bollinger bands, Volume Profile, volume-price patterns, supply tiers, and the layers framework. This section wires the System-B-relevant pieces into the model; the shared doc is the source of truth.

Turn the five selection criteria into numeric features, scored cross-sectionally across the catalogue.

### 3.1 Structural factors (the "what to consider" list)

| Factor | Feature(s) | Direction |
|---|---|---|
| **Supply outlook** | discontinued-collection flag; probability of re-release via Armory pass; drop-source active/retired | retired/scarce = bullish, but re-release risk = bearish |
| **Existing quantity** | estimated total float / listing count for the wear tier | sweet spot ~2,000–10,000 FN; too low hurts liquidity, too high caps upside |
| **Circulation** | `buff_volume_24h`, rolling avg daily trades | need ≥ ~10/day for tradability; 20–40/day = healthy |
| **Meta status** | weapon class ∈ {AK-47, M4A1-S, M4A4, USP-S, Glock, AWP, …}; secondary-meta (Galil, etc.) | meta bullish; but traditional-meta may be *saturated* → secondary-meta is where fresh edge is |
| **Aesthetics** | rank within its weapon category; art-style proxy | subjective; encode as a curated score, refine over time |

Each feeds a **composite item score**. Weight the factors, then let the backtest tune the weights (don't hand-fix them on folklore).

### 3.1a Hard selection filters (all mandatory — a gate before scoring)

An item must pass **all** of these to be scoreable (Shared §4.3):
- **Supply band:** high-conviction/FN sweet spot ~**2,000–10,000**; broader safe-liquidity **10,000–30,000**; **hard-exclude supply > 50,000** (upside capped).
- **Case/source price ≥ 80 CNY** (expensive opening cost → supply won't inflate easily).
- **≥ 3 valid buy orders** near market (bids close to price, not lowballs) — demand-depth proof and your exit floor.
- **Liquidity ≥ ~10 genuine trades/day** (20–40 = healthy).

**Priority tilts** (Shared §4.4): weapon M4A4 > AK-47 ≈ M4A1-S ≈ Glock > others; category 1st-gen gloves > glove materials > knife-god materials ≥ mid-tier primaries. Note traditional mid-tier primaries are *saturated* this cycle → tilt toward secondary-primaries.

### 3.1b Technical-analysis features (timing layer)

Add these as features alongside the structural factors (Shared §3.1–3.3):
- **Bollinger:** `pct_b` (position in bands), `bandwidth` (widening = trend accelerating), `middle_band_side` (above = strong/holdable, below = weak/don't-bottom-fish). Lower-band touch **+ red bar + price stops falling** = buy; upper-band touch **+ green bar** = sell/trim.
- **Volume Profile / cost distribution:** dense-zone location vs. price (support if price above, resistance if below), and **dense-zone drift** (up = accumulation/bullish, down = distribution/avoid). Shape classifier: low-single-peak = accumulation done (bullish); high-single-peak = top-heavy (high risk).
- **Volume–Price pattern** (encode the 5 states): the buy-worthy one is **volume ↑ + price ↑ together** (healthy accumulation) and the compact rule **listings ↓ + volume ↑ = good**; the avoid states are price ↑ + volume ↓ (weak rally / one-wave pump) and listings ↑ + volume ↓.

### 3.2 Accumulation signals (the market-maker detectors — the entry timing)

These decide *when* to buy a well-scored item. Operationalize the three signals as testable features:

- **Signal 1 — Sideways + shrinking float.** Price flat/ranging while `listing_count` (and estimated available float) trends **down** over N days. → supply being quietly absorbed.
  `flat_price(window) AND slope(listing_count) < 0`
- **Signal 2 — Volume without price rise.** `buff_volume_24h` spikes to several× its baseline while price barely moves. → accumulation. (Use *executed volume*, not listings.)
  `volume_z > threshold AND abs(price_change) < small`
- **Signal 3 — Resilient to bad news.** Item holds or rises when the broad market (or the item's own news) is bearish. → strong hidden bid.
  `market_return < 0 AND item_return >= 0` over the event window.

**Entry rule:** a high composite score (§3.1) **plus** ≥2 accumulation signals firing simultaneously → add to high-priority watchlist → buy within size caps. This is not a guarantee; it's a win-rate improver. Treat every signal as a hypothesis the backtest must validate net of fees.

### 3.3 A note on the folklore
The "market rhymes" (small consecutive rises real / large ones exit; buy dips not vertical spikes; sell into euphoria; etc.) are candidate features, not laws. Encode the falsifiable ones as momentum/mean-reversion features and let the backtest keep only what survives the 2.5% fee. Discard the unfalsifiable ones.

---

## 3c. Market-regime gate (runs before item scoring)

Classify the broad market each cycle into `{bull, bear, sideways, weak}` (Shared §2) — quantified from breadth (share of tracked primaries above their middle band), market-wide listings-vs-buy-orders balance, and fill speed. **Regime sets the deployment ceiling and biases entries:**

| Regime | Max deployment | System B behavior |
|---|---|---|
| Bull | 70–80% | scale into strong items gradually |
| Sideways | ~50% | range-trade support/resistance for steady 10–15% |
| Bear | ≤30% | **stop new buys / cut losses; never average down** |
| Weak / low-liquidity | small items only | spread across 5–10 small items (§ Shared 7); avoid large items |

In bear/weak regimes the factor model still *ranks*, but the risk gate (§8) refuses or shrinks buys. "In a weak market, not losing is winning."

---

## 4. Modeling approach

> **Empirically grounded** by the research corpus (`/research/RESEARCH_INDEX.md`, Shared §11). The choices below aren't guesses — they're what the two CS2-specific studies actually found.

- **Target = returns, not prices** (prices are non-stationary with update-driven breaks; returns are stationary). Predict *N-day forward log return* or rank on it.
- **Cross-sectional, not per-item.** Per-item history is short and noisy. Score *many* items each timestep and rank; trade the top-ranked.
- **Model class: gradient-boosted trees / Random Forest as the workhorse; keep linear as an interpretable baseline; do NOT reach for LSTM/deep sequence models.** In Pettersson's 640k-obs study, RF (R²≈0.49) and XGBoost (0.45) beat linear (0.42) and *crushed* LSTM (0.18) — the market has weak temporal memory, so deep nets underperform. Trees also give feature-importance interpretability, which we need for the debugging loop (Shared §12).
- **Lead with the empirically dominant signal:** deviation-from-7-day-MA (short-term momentum) was the single strongest predictor in Pettersson (RF importance ≈0.64); 7-day return was negative (medium-term mean reversion). So the core feature pair is **short-horizon continuation + medium-horizon reversal** — which is our Bollinger `pct_b` (§3.1b). Build and validate this before anything fancy.
- **Add a conditional-volatility estimate** (GARCH-style; Nikolaenko showed vol is forecastable even when direction isn't) — feed it as a feature *and* use it for volatility-targeted sizing (Shared §11).
- **Validation:** strictly **time-ordered / walk-forward** splits — never random. Mind the train-vs-test gap (RF overfit to 0.77 train / 0.49 test in the study); regularize (min_samples_leaf, subsample).
- **Expectation setting:** ~0.5 R² on daily returns is the realistic ceiling both papers hit — roughly half of daily movement is noise. Size for a modest edge, not certainty; the money is made on discipline and risk control, not on a magic forecast.

---

## 5. Holding, staged entry & the 7-day cooldown

System B trades **left-side** (staged dip-buying) by default (Shared §6.1) — the opposite of System A's momentum style.

- **Staged build, CD-gated (Shared §6.2):** split each item's allocation into batches; buy the first batch, add **only at support** (≈ −10% from first entry), and **only after the prior batch's CD clears**. Keep dry powder. Never exceed 6 layers (60%) in one item.
- Track `unlock_time = buy_time + 7d` per lot; the exit scheduler ignores locked lots. (Horizon is weeks–months, so the lock rarely binds, but adds are still CD-gated.)
- **Hard take-profit / stop-loss (Shared §6.3) — attach at entry as brackets:**
  - Take profit **+10–15%**; trim partial at resistance. Consistency over greed.
  - Stop loss **−10% → cut**; **−15% to −20% → unconditional liquidation.**
  - **Never average down in a bear regime.**
- **Exit logic:** sell into strength once the thesis plays out (target hit / euphoria / accumulation reversing into distribution per the cost-distribution drift), or cut on thesis-break (e.g. confirmed re-release killing a scarcity thesis). Scale out — don't dump into a thin book.
- **Position sizing vs. exit liquidity** (same discipline as System A §5.2):
  ```
  max_units(item) = min(capital_cap_per_item / entry_price, k * avg_daily_volume)   // k ≈ 0.2–0.5
  ```

---

## 6. Execution / automation layer

Same reality as System A §6 — **fully automated on BUFF requires a real execution path the data API doesn't give you:**
- **Option A (recommended):** official BUFF/NetEase API (~$150/mo, Chinese account) — most stable for autonomous order placement.
- **Option B:** unofficial session-based automation — works, but violates ToS, fights anti-bot/Cloudflare, and risks account/inventory bans; isolate capital if used.

System B is *gentler* on the execution layer than System A: orders are patient (limit/buy-orders rather than urgent market grabs), so you can throttle hard, avoid burst patterns that trip anti-bot systems, and place buy orders at your target price rather than chasing. This materially lowers ban risk vs. the reactive system.

Build behind the same interface (`place_buy`, `place_sell`, `get_inventory`, `get_wallet`) with idempotent order IDs and per-cycle reconciliation.

---

## 7. Consuming the shared signal bus (social is BOTH offense and defense)

> **Does System B need to run the full monitor agent? No — it doesn't *own* it.** The monitor is **shared infrastructure** (built once — see System A §7 — and jointly owned) that emits a **tiered signal bus**. System B *subscribes* to it rather than building its own. But make no mistake: slow trend movements in this market are substantially driven by social narrative, so System B is a first-class consumer of that feed, not a system that merely guards against it.

System B reads two of the three tiers (see §7 of System A for tier definitions):

- **Tier 3 — attention/sentiment (a LEADING ENTRY FEATURE).** Per-item mention volume and sentiment, trended over time. **Rising attention while price is still flat is a narrative-formation signal that precedes accumulation** — it's the same phenomenon Signal 2 (§3.2) detects in volume, seen one layer earlier in social. Fold it directly into the factor/timing model as a feature: a high composite score + accumulation signals + *rising-but-early* social attention is a stronger entry than the structural factors alone. (Guard against buying *late* attention — parabolic mentions on an already-pumped item is the §8.1 block-list case, not an entry.)
- **Tier 2 — confirmed events (a RISK OVERLAY).** On a confirmed update / Armory re-release affecting a held item, **pause new buys** in that item/collection and flag the lot for thesis review (hold / scale out / exit). Never *chase* the event — that's System A's job.

- **Graceful degradation:** if the shared bus is down, System B must keep running on structural factors alone (attention feature simply goes null). It should never hard-depend on social the way System A does.
- **What it does NOT do:** System B does not consume Tier 1 (raw leaks/rumors) and does not race. It ingests the *slower, higher-confidence* slices.

---

## 8. Risk management

### 8.0 Position sizing — the "layers" framework (Shared §5)
- Divide each category's budget into **10 layers** (1 layer = 10%).
- **Baseline 50% deployed**, 50% dry powder for averaging down / dip-buying.
- **Regime-scaled ceiling:** bull 70–80% · sideways ~50% · bear ≤30% · weak = small items only.
- **Per sub-category ≤ 60% (6 layers); per item ≤ 6 layers; never all-in (梭哈); idle funds only, no borrowing.**
- Example split (100k CNY): mid-tier collection 30k · mid-tier primaries 20k · materials 10k · gloves 20k · dip reserve 20k.
- Core logic: **survive longer, not win bigger** — one Valve update can wipe out the over-deployed.

### 8.1 Per-item
- Capital cap per item; volume-relative size cap for exit-ability (§5).
- **Block list** for items showing late-stage-pump shape (parabolic price + collapsing listings + hype spike) — never buy these, even if the factor score looks tempting. Getting trapped in a distribution phase is the classic retail loss.

### 8.2 Portfolio
- Max total exposure; concentration caps per collection/weapon class (a single update can hit a whole class).
- Diversify across uncorrelated theses so no one Valve decision sinks the book.

### 8.3 Thesis / model risk
- Every position carries a written thesis and an invalidation condition. If the invalidation fires (e.g. confirmed re-release for a scarcity bet), exit — don't rationalize.
- Watch for **regime change**: the doc itself notes traditional-meta selections are near-saturated after this year's meta hype. Factor weights decay; re-fit periodically on walk-forward data.

### 8.4 Operational
- Kill switch; capital isolation across accounts; data-feed sanity checks (pause on stale/divergent feeds); per-cycle reconciliation.
- **Discretionary rules → hard constraints (Shared §8):** CD-gate all adds; scheduled (e.g. daily) decision cadence, not a hot loop; a re-entry cooldown after any stop-loss (no revenge-trading); per-item turnover cap (aim low churn — "1–2 trades/month" spirit); tripped daily/weekly loss limit halts new buys.

### 8.5 Drawdown & accounting
- Track marked (mark-to-market on inventory) vs. realized P&L separately; realized only lands after locks clear and items actually sell.
- Weekly loss limit trips the kill switch and forces a factor-model review.

---

## 9. Backtest & mock plan

- **Historical backtest** on multi-year data. Must model: 2.5% BUFF fee, T+7 lock, realistic fills capped at a fraction of book depth/volume, and slippage. Ignoring fills/fees/lock produces a beautiful, fake equity curve.
- **Pitfalls to control:** survivorship (items added/discontinued via Armory passes — include delisted items), look-ahead bias in features (only use data available at decision time), and thin-book fill fantasy.
- **Cross-validate over time** (walk-forward), never random splits.
- **Forward paper trading** in shadow mode at real observed prices before real capital.
- **Go-live gate:** deploy real money only after paper results clear a pre-agreed net-of-cost edge threshold.
- **Automated journaling & attribution (Shared §9):** log every lot with entry/exit dates, item, layer %, buy/sell price *including fees*, net P&L, the specific factors/signals that fired, and outcome vs. thesis. Feed this back to re-fit factor weights (walk-forward), measure which signals actually carry edge net of fees, and promote recurring mistakes into new risk rules. This is the live-trading analogue of the crash course's trading journal — "no log = wasted experience."

---

## 10. Architecture summary

```
[cs2.sh / csmarketapi feed] → Normalizer (§2.3) ──────────────┐
                                                              ▼
        Shared Signal Bus ── Tier 3 (attention) ───→ Factor Model (§3) → Ranker (§4)
        (built in Sys A §7,                                   │
         jointly owned)   ── Tier 2 (confirmed) ─→ Risk Overlay (§7) ────┤
                                                                          ▼
                                                          Watchlist → Risk Gate (§8) → Execution (§6) → BUFF
                                                                          │
                                                                          └→ Ledger (T+7 aware, §5)
```

---

## Appendix P — Parked: "ride the early pump" (NOT for initial build)

Kept per request for future consideration; **excluded from the active strategy.** Same reasoning as System A Appendix P: buying into a manipulated item early and dumping before the crash is participating in a pump-and-dump — the profit comes from the retail buyers who get trapped, and the T+7 lock makes misjudging the top ruinous. For System B, manipulation detection is used **only** to keep flagged items off the buy list (§8.1), never as an entry.
