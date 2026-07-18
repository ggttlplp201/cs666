# Shared — CS2 Market Fundamentals & Indicator Library

> **Purpose:** the grounding knowledge layer for *both* systems. This is the "how the CS2 market actually works" reference used to design features, calibrate thresholds, and (where useful) prime the model. System A and System B both cite this doc rather than duplicating it — same pattern as the shared signal bus: shared knowledge, separate decision logic.
>
> **Source:** distilled from a Chinese-market trading crash course. Treat every rule here as a **testable hypothesis to validate in backtest net of fees**, not gospel. Most of this is discretionary human advice; §8 covers how to convert it into hard automation constraints.
>
> **Fee note:** the source quotes round-trip fees of ~3–5%. On BUFF the seller-side fee was ≈2.5% until **2026-04-14, when BUFF cut it to 1.5%** (buff.163.com/news/87397 — Builder 2's due diligence; `costs.buff_fee_pct` in config is the live number). Withdrawal adds 1% on cash-out, and since 2025-07-16 seller funds settle only after the 7-day Trade Protection window (proceeds are receivables, not cash). Model *actual* per-venue, round-trip fees in all EV math (System B §5 / System A §5). The principle "always deduct fees before calling something profitable" is non-negotiable.

---

## 1. Glossary (CN → EN, with model meaning)

| CN term | English | Meaning / how we use it |
|---|---|---|
| 左侧 / 右侧 | Left-side / Right-side | Left = buy as price falls (staged dip-buying); Right = buy as momentum rises. **Left ≈ System B's native style; Right ≈ System A's.** |
| 下轨 / 中轨 / 上轨 | Lower / Middle / Upper Bollinger Band | Support / trend-strength / resistance. See §3.1. |
| 出红柱 / 出绿柱 | Red / Green volume bar | Red = selling pressure; Green = buying pressure. Used as confirmation with band touches. |
| 存世量 | Circulating supply | Total units in existence. Core selection factor (§4). |
| 有效求购 | Valid buy orders | Bids *near* market price (not lowballs). Depth/demand proxy; a mandatory selection filter (§4). |
| CD | Cooldown | 7-day trade lock (T+7). Gates every add/exit decision. |
| 层 / 仓 | Layer / Position | 1 layer = 10% of allocated capital. "5层仓" = 50% deployed (§5). |
| 千百战 | Mid-tier primary weapons | ~1,000–3,000 CNY skins (M4/AK/A1). |
| 小件 / 大件 | Small / Large items | Small = 50–500 CNY; Large = expensive gloves/knives/collection skins. Regime-dependent (§7). |
| 庄 / 盘主 | Market maker / Whale | Large player manipulating price. |
| 一波流 | One-wave pump-and-dump | Sharp spike then crash. **Avoid entering — this is the parked pump pattern.** |
| 筹码图 / 密集区 | Volume Profile / Dense zone | Holder cost-basis distribution (§3.2). |
| 套牢盘 | Bagholders / Trapped holders | Bought high; will sell into any bounce → overhead resistance. |
| 正T / 负T / 做T | Swing (T) trading in a position | +T = buy low then sell high; −T = sell high from holdings then rebuy lower. |
| 接盘 | Being exit liquidity | Buying while whales sell to you. The thing to never be. |
| 割肉 / 止损 | Cut loss / Stop loss | Sell at a loss to preserve capital (§6). |
| 梭哈 | All-in | 100% into one item. **Forbidden** (§5). |

---

## 2. Market regime detection (gates everything)

Before any item-level decision, classify the broad market. Regime sets the position-sizing ceiling (§5) and biases both systems.

- **Bull:** most primary weapons rise steadily daily; pullbacks small and quickly recovered; buy orders plentiful, listings few, orders fill fast; even obscure items follow.
- **Bear:** primaries drift down; bounces short-lived and fade next day; listings pile up, bids thin and keep stepping lower; "even good items can't escape" — entering is catching a falling knife.
- **Sideways/range:** prices oscillate in a band; best regime for steady range-trading.

**Quantify it** from breadth (share of tracked primaries above their middle band / N-day MA), listing-vs-buy-order balance market-wide, and aggregate fill speed. Emit a `market_regime ∈ {bull, bear, sideways, weak}` signal both systems read.

**Per-regime action (core table):**

| Regime | Max deployment | Action |
|---|---|---|
| Bull | 70–80% | Hold strong items, scale up gradually. |
| Sideways | ~50% | Trade support/resistance; steady 10–15% profits. |
| Bear | ≤30% | Avoid buying; cut losses immediately; **never average down**. |
| Weak / low-liquidity | small items only | Spread across 5–10 low-value items; avoid large items (§7). |

---

## 2a. Data layer: verified API facts, tier gaps & the phased data strategy

> Added after live testing of the cs2.sh API (2026-07). This section is the
> authority on what data we actually have; System-A §2 / System-B §2 defer to
> it where they disagree. The full normalization schema stays in System-A §2.3.

### 2a.1 cs2.sh — verified facts (Developer tier, $75/mo)

- Base URL `https://api.cs2.sh`; auth `Authorization: Bearer <key>`; **`Accept-Encoding: gzip` is required on every /v1 request.**
- `POST /v1/prices/latest` — up to **100 items/request** (body ≤ 1 MiB). A bad item name returns an `errors[]` entry, not a failed batch. GET (no body) dumps ALL tracked items — avoid.
- Response shape `items[market_hash_name][source]`, sources `buff, youpin, csfloat, skinport, steam, c5game`. Verified BUFF fields: `ask` (lowest listing), `ask_volume` (**listing count**), `bid` (highest buy order), `bid_volume` (**buy-order count**).
- Freshness = `collected_at` (cs2.sh's snapshot time). `updated_at` is the marketplace's own timestamp — **not** a freshness signal.
- **⚠ Currency: everything is normalized to USD — including BUFF (which trades CNY).** The fee math, ledger, and capital are CNY; feed prices must be FX-converted (config `fx.usd_cny_rate`), never assumed CNY.
- **⚠ `ask_volume` is NOT total supply (存世量).** It is the current listing count — a small fraction of supply. The §4.2 supply thresholds must never be evaluated against it; true `total_supply` requires `/v1/archive/history` (Scale tier).

### 2a.2 Known gaps on the Developer tier

Developer exposes **only** `/v1/prices/latest`; `/v1/market/buff/latest` and all history/archive endpoints are Scale ($200/mo). Consequences (code degrades explicitly — flags the gap, never proxies from listings):

- **No executed volume for BUFF** → whale Signal 2 (§3.4, volume-up/price-flat) is *not computable*; the "≥10 genuine trades/day" mandatory filter (§4.3) is *not enforceable* (risk gate refuses buys by default — `selection_filters.allow_unknown_volume`); volume–price patterns (§3.3) are unavailable live.
- **No float/fade ranges, no total_supply.**

### 2a.3 Phased data strategy (experimental project — validate before spending)

**PHASE 1 (now, ~$0)** — validate the premise:
- **Backtest data = Steam Community Market price history (FREE).** `GET https://steamcommunity.com/market/pricehistory/?appid=730&market_hash_name=…` with a logged-in `steamLoginSecure` cookie. Daily **median price + quantity sold** (real executed volume) back to 2013 — the exact source of Pettersson's 640k-observation dataset (paper2, Appendix A2). Undocumented + rate-limited: polite polling, aggressive caching (`src/shared/steam_history.py`; rows stored `source="steam"`).
  **Caveat:** Steam, not BUFF — trades ~30–40% above BUFF, median hides intraday. Good enough to test *the strategy premise* (do updates cause tradeable repricings? does MA-deviation momentum predict net of fees?); never a live-trading substitute.
- **Live signals = cs2.sh Developer** via `/v1/prices/latest` (bid/ask + depth).
- **Snapshot poller runs from day one:** poll `/v1/prices/latest` every 5–15 min and persist every response, forever (`data.snapshot_poller`). This accumulates our *own* BUFF depth history — data not captured is lost permanently.

**PHASE 2 (only if Phase 1 shows an edge net of fees):**
- **cs2.sh Scale ($200/mo) for ONE month** → bulk-download `/v1/archive/history` (BUFF `total_supply` + hourly volume back to 2023) and `/v1/liquidity/items` for our universe, store locally, downgrade — archive data is static; don't rent it monthly. Price-check alternatives first: CSPriceAPI, csmarketapi, SteamAnalyst (free tier + built-in price-manipulation flags, useful for the pump blocklist).
- **BUFF execution API decision:** **Developer ¥300/mo (≈$42) is sufficient** — `POST /api/market/developer/purchase/orders` can take liquidity on standard items. Enterprise ¥1000 is only needed to snipe specific float-variant listings via `/api/market/enterprise/goods/sell_order`.

---

## 3. Indicator library (the feature set)

### 3.1 Bollinger Bands
Three bands: Upper (resistance), Middle (trend/strength), Lower (support).

- **Middle band = the waist.** Price above → strong, holdable, first touch often bounces. Price below → weak; do **not** bottom-fish; usually slow downtrend.
- **Upper band = resistance / sell.** Touch + **green** volume bar → standard sell (reduce/take profit; capital is exiting, follow it out). Sticks to upper band with **no** green bar → strong item riding the band, **hold**.
- **Lower band = support / buy.** Touch + **red** bar + price *stops falling* → buy signal (place low resting orders). Touch with no red bar and still falling → **no signal**, can keep dropping. Rule: don't buy until it has fully plunged *and* stabilised.
- **Band width = trend speed.** Widening → trend accelerating (don't fight it — hold or exit decisively). Narrowing → entering oscillation; range-trade.

**As features:** `pct_b` (position within bands), `bandwidth`, `middle_band_side`, and touch-events joined with the volume-bar sign.

### 3.2 Volume Profile / cost distribution (筹码图)
Distribution of holders' cost basis. Tall bars = cost-concentration (dense) zone.

- **Support/resistance:** price *above* the dense zone → that zone is **support** (holders defend cost). Price *below* → dense zone is **resistance** (bagholders sell into bounces).
- **Dense-zone drift (the key read):** drifting **up** → main force accumulating/pushing → follow with small size. Drifting **down** → distribution/selling → avoid buying. No direction → chaotic, stay away.
- **Four shapes:**
  - *Low single peak* → accumulation complete; potential uptrend; hold if price stays above the peak.
  - *High single peak* → profit-taking done, retail bought the top; **high risk**, break below peak = run.
  - *Double peak* → range; trade between; volume break above upper peak opens new upside.
  - *Scattered peaks* → illiquid/crashing; forbidden zone.
- **Never use alone** — combine with supply, case price, volume-price. It shows trend, not the exact bottom.

### 3.3 Volume–Price relationships (5 patterns)
**Critical distinction the model must encode:** *Volume (成交额, executed trades)* ≠ *Listings (在售量, open sell orders).* Volume up = more buyers (bullish); listings up = more sellers (bearish).

1. Listings ↑ + price ↓ + high volume → main force **exiting** (or rare costly shakeout). Bearish.
2. Price ↓ + low volume → **shakeout** to scare weak hands, then re-accumulate. Often a setup, not a real breakdown.
3. Volume ↑ + price ↑ together → **healthiest**; real accumulation, steady push. Confirmation to follow.
4. Price ↑ + volume ↓ → **weak rally, sell**; classic one-wave pump — never enter the pump.
5. Sideways/flat → conditional entry only with conviction/information; else wait for the first move to confirm.

Compact rule from the selection section: **listings ↓ + volume ↑ = good; listings ↑ + volume ↓ = bad.**

### 3.4 Whale-accumulation signals (entry timing)
Three signals that a market maker is accumulating *before* a pump — buy *ahead*:

1. **Sideways + shrinking volume (横盘缩量):** can't drop further, sellers exhausted, listings/float slowly decreasing → light float, low resistance to a pump.
2. **Volume up, price flat (放量不涨):** executed volume spikes several× baseline but price barely moves → whale absorbing all sell orders while pacing to avoid tipping the price. *(Data gap: not computable on the cs2.sh Developer tier — no executed volume; see §2a.2.)*
3. **Against-the-trend resilience (逆势抗跌):** refuses to fall on bad news / broad-market drop → strong hidden bid absorbing supply; whoever holds most inventory defends the line.

Multiple signals firing at once → high-priority watchlist. Not a guarantee; a win-rate improver.

---

## 4. Item selection criteria

### 4.1 The five core factors
1. **Production/source (产出):** active drop, retired, or discontinued-collection. Retired/capped supply is bullish; a likely Armory/Arsenal re-release is bearish (supply risk).
2. **Circulating supply (存世量):** determines the price *ceiling*; capital injection determines how high it actually goes. Not "lower is always better" — too low kills liquidity.
3. **Liquidity / daily volume (流通量):** ≥ ~10 genuine trades/day is the minimum to be "liquid"; 20–40/day is healthy.
4. **Primary weapon? (主战与否):** AK-47, M4A1-S, M4A4, USP-S, Glock, AWP = primaries. Secondary (e.g. Galil, force-buy guns) rank lower — but traditional primaries in the mid-tier band are *saturated* after this year's rally, so secondary-primaries are the fresh-edge tier.
5. **Aesthetics (颜值):** rank within weapon category; classic art styles (Howl / Poseidon / Boom lineage) carry a premium.

### 4.2 Supply tiers (reconciling the two ranges in the source)

> ⚠ These are **total existing quantity (存世量)** thresholds — never evaluate
> them against listing counts (`ask_volume`), which are a small fraction of
> supply. True total supply needs the Scale-tier archive (§2a.2); until then
> supply filters are a Phase-2 capability (config keys renamed `total_supply_*`).

- **High-conviction "institutional-grade" pick (esp. Factory New):** ~**2,000–10,000** existing units is the sweet spot capital prefers (Hellhound ≈7,000).
- **Broader safer-liquidity selection / quick-start:** **10,000–30,000** circulation.
- **Hard exclude:** supply **> 50,000** (upside capped, too heavy to move).

### 4.3 Mandatory conditions (all three required)
1. Supply in target band (§4.2).
2. **Case/source price ≥ 80 CNY** (expensive opening cost → supply won't inflate easily).
3. **≥ 3 valid buy orders** near market (e.g. item at 1000 → bids at 950–980 with 4+ qty). Valid = close to market; invalid = far-below lowballs. To buy, place *between* the valid and invalid zones.

### 4.4 Priority ordering
- **Category priority:** 1st-gen high-end gloves > 1st-gen glove materials > knife "three gods" materials ≥ mid-tier primaries (1,000–3,000) and 2nd/3rd-gen gloves.
- **Weapon priority:** M4A4 > AK-47 ≈ M4A1-S ≈ Glock > others (M4A4 = highest liquidity, easiest to recover).
- **Upper/lower tier link:** if the top-tier Gold item has an active market maker, its lower-tier Red skins usually follow — but don't blindly sweep a whole tier; watch for manipulation.

---

## 5. Position sizing & money management (layers)

**Golden rule:** stickers/pins never full position; in a range keep ~5 layers (50%); chasing momentum never exceed 2 layers (20%); never casually push to high deployment.

- Divide capital into **10 layers** (1 layer = 10%).
- **Baseline: 50% deployed**, 50% dry powder for averaging down / sudden dips.
- **Regime scaling:** bear 30% (short swings only); strong bull 70–80%.
- **Per-category cap: ≤ 60% (6 layers)** of that category's budget in one sub-category.
- **Single item: ≤ 6 layers (60%)** of its allocation; momentum-chase entries ≤ 2 layers.
- **Never all-in (梭哈); never borrow; idle funds only.**
- **Core logic:** this market rewards *longevity, not one big win* — a single Valve update can wipe out the reckless.

**Example allocation (100k CNY):** mid-tier collection 30k · mid-tier primaries 20k · materials 10k · gloves 20k · emergency-dip reserve 20k.

---

## 6. Entry / add / take-profit / stop-loss

### 6.1 Left-side vs right-side
- **Left-side (dip-buying, staged):** exact bottom unknown → enter in stages, keep dry powder, **wait a full CD before adding**. Rises → break even; drops → add lower. Needs good info + selection understanding. *(System B default.)*
- **Right-side (momentum):** buy only on a confirmed breakout/uptrend, when the main force's signal is obvious and the trade completes within one CD. Easier but riskier. *(System A default.)*

### 6.2 Staged build example (5,000 CNY, one item)
Buy 2 @600 → 2 @560 → 2 @540 → wait CD; if still ~540, 2 more (≈70–80% deployed) → sell 540-batch @550, 560-batch @570. Add **only at support** (≈ −10% from first buy) and **only after the prior batch's CD clears**; never exceed 6 layers.

### 6.3 Take-profit / stop-loss (hard rules)
- **Take profit at +10–15%**; sell partial at resistance. Consistency beats greed.
- **Stop loss −10% → cut immediately** (no hoping). **−15% to −20% → unconditional liquidation.**
- **Never average down in a bear market** — the bottom is unknowable.
- **+T / −T** swing-trading allowed within a held position for extra yield.

---

## 7. Small vs large items & rotation (regime-dependent)

- **Weak/low-liquidity market:** large items (gloves, high knives) are a minefield — need huge capital to move, full of bagholders, every bounce gets sold. **Small items (50–500 CNY) are the safe haven:** low listings (≤150), low entry, spread across **5–10** items, place low resting orders and wait. In a weak market, *not losing is winning*.
- **Rotation logic:** after small items run up, capital rotates to **mid-tier (1,000–3,000)** items with real fundamentals and depth. Don't chase late-stage small-item hype — pre-place low resting orders on mid-tier candidates (low listings, not yet hyped) and wait for the rotation.

---

## 8. Translating discretionary advice → automation constraints

The source is written for a human ("check once a day, close the app, don't obsess"). For an autonomous agent, those become **hard-coded rules**:

- "Wait for CD before adding/selling" → the scheduler **cannot** act on a lot before `unlock_time`; adds to an item are blocked until the prior batch clears CD.
- "Don't obsess / check once a day" → System B runs on a **scheduled decision cadence** (e.g. daily), not a hot loop.
- "Don't revenge-trade after a loss" → a **cooldown/rate-limit** on re-entering an item after a stop-loss; a tripped daily-loss limit halts new buys.
- "Never all-in / never full position" → **position-size caps enforced in the risk gate**, not left to judgment.
- "Take profit 10–15%, stop 10%" → **bracket orders / automated TP+SL** attached at entry.
- "Don't follow screenshots/tips" → social is **untrusted input** feeding scoring only, never a direct trigger (already in both systems).
- "1–2 trades/month, don't overtrade" → a turnover cap per item; longevity over churn.

---

## 9. Trade journaling → automated logging & attribution

The human "trading journal" becomes the system's **structured trade log + performance attribution**. Record per lot: entry/exit dates, item + capital + layer %, buy/sell price *including fees*, net P&L, the 3 selection reasons (which factors/signals fired), and outcome vs. thesis. Feed this back to:
- re-fit factor weights (walk-forward),
- measure which signals actually carry edge net of fees,
- flag recurring mistakes as new risk rules.

"10 tracked trades > 30 untracked." No log = wasted experience.

---

## 10. Which system uses what

| Knowledge block | System A (reactive) | System B (positional) |
|---|---|---|
| Regime detection (§2) | gate: pause reactive buys in bear unless durable | primary: sets deployment ceiling |
| Bollinger (§3.1) | band-width = trend acceleration confirmation | core entry/exit timing |
| Volume Profile (§3.2) | secondary | **core** factor/timing feature |
| Volume–Price (§3.3) | pattern 3 = confirm reactive entry; pattern 4 = avoid | core |
| Whale signals (§3.4) | secondary | **core** entry timing |
| Selection criteria (§4) | liquidity/exit-ability check on candidates | **core** factor model |
| Position sizing (§5) | momentum-chase ≤2 layers; caps in risk gate | full framework |
| Left/right-side (§6.1) | **right-side** default | **left-side** default |
| TP/SL (§6.3) | yes (hard brackets) | yes (hard brackets) |
| Small/large & rotation (§7) | minor | regime-dependent strategy |
| Journaling (§9) | yes | yes |

---

## 11. Empirical grounding (research corpus) — *secondary to the notes above*

> **Precedence:** §§1–10 (derived from our crash-course notes, BUFF practitioner knowledge) are **primary and govern live trading.** This section is **secondary** — the two academic studies (`/research/RESEARCH_INDEX.md` + local PDFs) are used to *corroborate* the notes and to hold a *backlog of additions to test later*. Where a finding just confirms a note, the note's framing wins. Findings tagged **[new]** aren't in the notes and are retained to validate on our own BUFF data before we trust them — they don't override §§1–10 today.

- **[confirms §4/System A] Update-driven structural breaks & model returns not prices.** Prices are non-stationary with breaks on balance updates; returns are stationary. Backs the notes' "updates move the market" thesis and the returns-based modeling. (Nikolaenko)
- **[confirms §3.1] Momentum + mean reversion.** The dominant predictor is deviation-from-7-day-MA (short-term momentum) with 7-day reversion behind it — i.e. exactly the notes' Bollinger `pct_b` logic. Use the **notes'** Bollinger framing; this is validation, not a replacement. Caveat: daily *direction* ceiling ≈0.5 R² — treat as an expectations check on the notes' more optimistic signal claims. (Pettersson)
- **[new] Model class:** gradient-boosted trees / Random Forest beat linear and *beat LSTM* (weak temporal memory → deep nets underperform). Notes are discretionary and silent on ML, so adopt this with nothing to override. (Pettersson)
- **[refines § events] Two event classes.** Balance/meta updates cause real price breaks (defer to the **notes'** System-A update logic); calendar/esports events (Steam sales, Majors, tournaments, operations) move *volume, not daily price* — the paper's added distinction, used for liquidity timing only. (Nikolaenko + Pettersson)
- **[confirms §5] Fat tails (kurtosis >100).** The statistical "why" behind the notes' "survive, don't win big." Keep the **notes'** layers framework; this just justifies it. (Nikolaenko)
- **[new] Steam sale seasonality.** Semi-annual liquidation dips in Steam sale windows. Notes (BUFF/China-centric) don't flag this — keep as secondary and verify it shows on BUFF before acting. (Nikolaenko)
- **[new] Volatility is forecastable** via GARCH even when direction isn't → enables the sizing rule below. (Nikolaenko)
- **Our edge is partly better data:** both papers were limited to 2 skins or Steam median-only; our BUFF volume + buy-order + listing-depth + float feed (cs2.sh) is richer than what either had. *(Tier caveat, §2a: on the Developer tier only bid/ask + depth are live today — the volume/float/supply edge arrives with Scale in Phase 2, plus whatever our own snapshot poller has accumulated by then.)*

**Volatility-targeted sizing [new — secondary]:** estimate conditional volatility per item (GARCH-style); scale position size *inversely* to forecast vol and place stops/targets in vol units. A paper-derived addition — layer it on top of (not instead of) the notes' layers framework (§5).

## 12. Model iteration, observability & governance (how to change the model fast)

A fully-automated system *will* make flawed decisions; the goal is to make them **diagnosable and correctable in minutes, not redeploys**.

**Layered design so a bad decision is traceable.** Every trade decision passes through: (1) data → (2) features/signals → (3) model/score → (4) decision & risk rules → (5) execution. When something goes wrong, triage *which layer*: bad data (feed stale/divergent), bad feature (signal miscomputed), bad model (weights/drift), or bad rule (a threshold/cap). Fix at the lowest responsible layer.

**Config-driven knobs (the fast path).** Factor weights, signal thresholds, position caps, TP/SL levels, regime ceilings, and the blocklist/allowlist all live in **versioned config (YAML), not code.** Adjusting behavior = editing config + reload, no logic redeploy. This is the answer to "the model is doing X, make it stop": tighten the rule or reweight the factor in config.

**Decision provenance logging (the debugging substrate).** Every order logs its inputs, which signals fired, the score, the rule that triggered it, and the regime — so any bad trade is *replayable* ("why did it buy that?"). This is the trade journal (§9) elevated to an observability tool.

**Override & rollback.** The risk gate can veto/shrink model output live; blocklist is hot-editable; models + configs are **versioned so you can revert to last-good instantly**; a single kill switch halts all buying.

**Safe change promotion.** Test any model/config change in **shadow / paper mode alongside live (champion-challenger)** before promoting. Never hot-swap an untested model into live capital.

**Retraining & drift.** Schedule walk-forward re-fits; trigger an off-cycle re-fit on a **structural-break/drift alarm** (Bai–Perron/CUSUM — the same break detection that flags balance updates) or a factor-decay alarm from the journal. Factor weights decay (e.g. the noted mid-tier-meta saturation) — re-fit, don't trust stale weights.

**What's live-editable vs. needs a redeploy:** *live* = config knobs, blocklist/allowlist, kill switch, regime ceilings. *Redeploy* = retraining the model, adding features, changing execution logic.

### 12a. Flawed-decision runbook (the fast-adjust procedure)

When the system makes a bad trade, follow this loop — most fixes are a config edit, not a redeploy:

1. **Pull the decision's provenance log** (§12 logging) — inputs, signals that fired, score, rule, regime. This tells you *why* it acted.
2. **Localize to a layer** using the symptom table below.
3. **Apply the lowest-layer fix**, test in shadow, then promote. Use the kill switch first if capital is actively at risk.

| Symptom | Most likely layer | Fast fix (live) | If it recurs (redeploy) |
|---|---|---|---|
| Bought an obvious pumped/parabolic item | decision rule | tighten pump-detector threshold; add item to blocklist | add/retrain a manipulation classifier feature |
| Over-trading / churning one item | decision rule | lower turnover cap; raise re-entry cooldown | revisit signal that keeps re-firing |
| Sizing too large / too concentrated | risk config | lower per-item/per-category caps; tighten vol-target | — |
| Won't sell a loser / ignores stop | risk config | verify TP/SL brackets are attached & firing | fix bracket-attachment logic |
| Good signal, bad entries in a falling market | regime gate | tighten bear-regime suppression / deployment ceiling | improve regime classifier |
| Same feature dominates every bad call | feature/model | down-weight that factor in config | re-fit weights (walk-forward); feature review |
| Model good in backtest, bad live | data or overfit | check feed freshness/divergence; pause on stale data | regularize / re-fit; check look-ahead leakage |
| Systematically wrong since a date | drift / structural break | — | trigger off-cycle re-fit (break alarm, §12) |

**Rule of thumb:** if the fix is "change a number" (a weight, threshold, cap, cooldown, or blocklist entry) it's a live config edit and takes minutes. If the fix is "change what the model *learns from*" (features, training data, model class) it's a retrain/redeploy. Design so the common cases are the former.

### Tooling notes
- **Claude Code** is the right tool to *build and iterate* the whole stack — data pipeline, feature engineering (Pettersson's feature table is a ready starter spec), the RF/XGBoost ranker, the GARCH vol module, Bai–Perron break detection, the backtester, and the paper-trading harness. It's an agentic coding tool (terminal / IDE / desktop). Key distinction: **Claude Code is the *builder/maintainer*, not the deployed model** — it produces the trained-model artifact + inference code, which then runs on your own scheduler/service 24/7. In the iteration loop, a dev describes the flaw → Claude Code inspects the provenance logs, adjusts config or retrains, runs the backtest/shadow, and proposes the change. (Docs: https://docs.claude.com/en/docs/claude-code/overview)
- **Obsidian** is a human knowledge-management layer, not a modeling or runtime tool. Since an Obsidian vault is just a folder of markdown files, point it at `/docs` + `/research` and the team browses/links the exact same files Claude Code reads. Complementary, not competing. Not needed for the model itself.
- **"Other agents like Hermes":** you don't need a separate coding agent to build the model — Claude Code covers building/iterating. If "Hermes" means a self-hostable open LLM (e.g. a Nous Hermes-class model), the sensible place for it is the **social-monitor's classification layer** (cheap, high-volume Chinese-text tagging), not the price model — that's a cost/hosting choice for one component, not a replacement for Claude Code. (Flagging ambiguity: confirm which "Hermes" is meant before committing.)
