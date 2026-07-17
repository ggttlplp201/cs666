# Research Corpus — Index & Implications

> **What this is:** a manifest over the academic papers in `/research/papers/`, written so an agent (Claude Code) or a teammate can absorb the market's empirical properties *fast* without re-reading every PDF. Read this file first; open a PDF only when you need a specific number, table, or method.
>
> **PRECEDENCE (read this first).** Our own notes — the crash-course `.txt` and everything derived from them in `/docs` (Shared §§1–10, and the System-A/B strategy logic) — are the **PRIMARY source and take precedence in live trading.** They're BUFF practitioner knowledge on our actual venue. The papers are **SECONDARY**: use them to (a) *corroborate* the notes and (b) hold a *backlog of ideas to test later*. Rule when they meet:
> - **Paper confirms a note** → keep the note's framing (it's more venue-specific and actionable); treat the paper as validation, don't duplicate.
> - **Paper adds something the notes don't have** → keep it, but tagged as secondary/for-later, and defer to notes wherever they speak to the same decision.
> - **Paper conflicts with a note** → follow the note now; keep the paper's version recorded so we can A/B it later; log the conflict in the trade journal.
>
> Do **not** copy paper code or numbers blindly — different venues (Steam vs BUFF), horizons, and small samples. Use them for *direction*, then validate on our own BUFF data.

## Recommended repo layout (so the corpus is machine-readable later)
```
/research/
  papers/
    paper1.pdf   # Nikolaenko — ARMA-GARCH, stationarity, structural breaks
    paper2.pdf   # Pettersson — ML price prediction (RF/XGBoost/LSTM)
  RESEARCH_INDEX.md                          # this file — read first
/docs/
  Shared_Market-Fundamentals_Indicator-Library.md
  System-A_Event-Driven_Reactive.md
  System-B_Positional_Value-Trend.md
/config/                                     # hot-editable knobs (weights, thresholds, caps)
/src/                                        # pipelines, features, models, backtester
```
Keep everything as markdown + PDFs in one tree. An Obsidian vault is exactly this — a folder of `.md` files — so humans can browse/link it in Obsidian while Claude Code reads the same files. No separate knowledge system needed.

---

## Paper 1 — Nikolaenko (2025), "Time-Series Analysis of CS:GO/CS2 Skin Prices: Stationarity, Breaks, and Volatility Forecasting"
`/research/papers/paper1.pdf` (Nikolaenko)

**Setup.** Two *substitute* CT rifles on **BUFF163** (our venue) — M4A4 Desolate Space (FT) and M4A1-S Decimator (FT) — 826 daily obs, CNY, Jul 2021–Feb 2024. Classic econometrics: ADF/KPSS, ARMA, GARCH(1,1), Bai–Perron breaks, STL.

**Findings.**
- **Prices are non-stationary; returns are stationary** (ADF + KPSS agree). ⇒ model *returns*, not price levels.
- **Return level is barely predictable:** ARMA selects white-noise / weak AR(1). Returns have ~zero mean and **very heavy tails** (kurtosis > 100 for Desolate). Direction is close to a coin flip for a linear univariate model.
- **Volatility IS forecastable:** strong volatility clustering; GARCH(1,1) fits well with α+β ≈ 0.82–0.91 (persistent but mean-reverting vol). Out-of-sample conditional-variance forecasts are decent.
- **Structural breaks land on game updates:** Bai–Perron break dates coincide with weapon balance changes (M4A1-S buff Sep 2021, nerf Nov 2022, etc.). These are the big, durable repricings.
- **Substitute dynamics:** nerf one weapon → usage and price shift to its substitute (and vice versa). The two skins co-move seasonally but their medium-run trajectories diverge → basket / VAR modeling suggested.
- **Steam sale seasonality:** semi-annual dips during Steam Summer/Winter sales (players liquidate skins to buy discounted games).

**Design implications (mostly System B risk + System A events):**
1. Model returns, not prices. (Already our stance — now empirically backed.)
2. Don't expect to predict daily *direction* well; **do** forecast *volatility* (GARCH) and use it for **position sizing and stop/target placement** — size down when conditional vol is high. → new *volatility-targeting* rule.
3. **Fat tails ⇒ fat-tail risk management:** no Gaussian VaR; use robust/quantile risk measures, hard stops, and small per-item caps. Reinforces "survive, don't win big."
4. **Breaks = balance/meta updates ⇒ System A's core alpha is real** and lands on identifiable dates. Use break detection (Bai–Perron/CUSUM) live as a **regime-change alarm** that (a) pauses positional trading and (b) triggers model re-fit; don't train across a break.
5. **Substitute-pair rule:** a nerf to weapon A is bullish for substitute B. Concrete System A rules-table entry *and* a System B cross-sectional feature (substitute basket).
6. **Steam-sale calendar feature:** expect liquidation dips in sale windows → buying opportunity / don't panic-sell.

---

## Paper 2 — Pettersson (2025, Åbo Akademi), "Price Dynamics in the Counter-Strike 2 Skin Market: A Machine-Learning Analysis"
`/research/papers/paper2.pdf` (Pettersson)

**Setup.** **Steam Community Market**, 28 liquid rifle/pistol skins (7 meta weapons × 4), **640,145 daily obs**, 2013–2025, median daily price + quantity. Feature-engineered (price MAs/deviations/returns, volume, case-market, peak players, event & calendar dummies). Target = next-day log return, chronological 80/20 split. Models: Linear, Random Forest, XGBoost, LSTM.

**Findings.**
- **Model ranking: Random Forest (R² ≈ 0.49) > XGBoost (0.45) > Linear (0.42) ≫ LSTM (0.18).** Tree ensembles win; **deep sequence models underperform** — the market has little exploitable temporal memory.
- **The single dominant predictor is `price_deviation_ma7`** (deviation of today's price from its 7-day moving average): linear coef ≈ 0.82, RF importance ≈ 0.64. Positive ⇒ **short-term momentum** (above trend → next day up).
- **`price_logret_7d` is negative** ⇒ **medium-term mean reversion.** Net: short-horizon continuation + medium-horizon reversal.
- **Case price/activity matters (supply proxy):** higher case price/opening → downward pressure on skin returns.
- **Calendar/esports events barely move daily price** (Steam sales, Majors, S-tier, Operations ≈ negligible on returns) **but they move volume** (Operations +33%, case-release week +34%, case day +17%; Majors −8% volume). *Note: Pettersson deliberately excluded weapon-balance updates.*
- **R² ceiling ≈ 0.50** ⇒ roughly half of daily variation is noise. Overfitting gap (train 0.77 vs test 0.49 RF); tuning gave marginal gains → the ceiling is *noise*, not model choice.
- **Data limitation:** median price (no individual sales, float, pattern, or stickers) hides micro-structure. Richer transaction/volume/depth data would help.

**Design implications (mostly System B modeling):**
1. **Use gradient-boosted trees / Random Forest as the workhorse; deprioritize LSTM/deep sequence models.** Start with a transparent tree ranker (feature importances = interpretability).
2. **Core empirical signal = MA-deviation momentum (short) + 7-day mean reversion (medium).** This is exactly our Bollinger `pct_b` / distance-from-middle-band feature — validated as *the* key feature. Build it first.
3. **Calendar/esports events ⇒ volume signal, not price alpha.** Feed them to liquidity/accumulation detection, not to direction prediction. (Contrast with balance/meta updates — see synthesis.)
4. **Case-market supply features** (case price/quantity MAs) are worth including as bearish-supply indicators.
5. **Set expectations:** ~0.5 R² is the realistic ceiling on daily prediction; size for noise, don't over-leverage on model confidence.
6. **Prefer richer data than Steam median** → validates the `cs2.sh` choice (real volume, buy orders, listing depth, float) over median-only feeds.
7. Their Appendix B feature table is a ready-made **feature-engineering starter spec** — adapt it to BUFF fields.

---

## Papers vs. our notes — overlap / difference map (precedence per topic)

Quick reference for what defers to the notes vs. what the papers add. **"Notes"** = crash-course `.txt` + Shared §§1–10.

| Topic | Relationship | What we do |
|---|---|---|
| Momentum + mean reversion | **Overlap** — papers confirm the notes' Bollinger / "buy dips, not spikes" logic | Keep the **notes'** Bollinger framing (§3.1); papers = validation only |
| Accumulation via volume-vs-price | **Overlap** — Pettersson's volume features ≈ notes' Signal 2 | Keep **notes'** whale-signal version (§3.4); it's richer |
| Game updates move price | **Overlap** — Nikolaenko's breaks confirm the notes' update thesis | Keep **notes'** System-A thesis; paper = empirical backing |
| Supply/circulation drives value | **Overlap** — notes' selection thresholds are far more specific | Keep **notes'** criteria (§4); paper's case-activity proxy is a minor add |
| Conservatism / fat tails | **Overlap** — notes' "survive not win big" ≈ kurtosis>100 | Keep **notes'** layers framework (§5); paper = the statistical "why" |
| Calendar/esports events = volume, not price | **Difference** (new) — notes don't split event classes | **Keep both:** notes drive update-reactions; paper's volume-not-price rule refines System A. Defer to notes on balance/meta updates |
| Model class: trees ≫ LSTM | **New** — notes are discretionary, silent on ML | Adopt (paper-only); nothing in notes to override |
| Volatility forecastable (GARCH) | **New** — notes don't model vol explicitly | Adopt for sizing (paper-only) |
| Steam sale seasonal dips | **New** — notes are BUFF/China-centric, don't flag this | Keep as secondary/for-later (verify it shows on BUFF) |
| Substitute pairs (nerf A → buy B) | **Difference** — distinct from notes' tier-linkage | **Keep both** — complementary, not competing |
| ~0.5 R² predictability ceiling | **Mild tension** — notes are more optimistic on signals | Follow **notes'** signal approach; treat paper's ceiling as an expectations check |

Everything marked **New** or **Difference** is retained as a *candidate to test later*, not something that overrides the notes today.

---

## Consolidated modeling directives (what both systems + Claude Code should follow)

> These are ordered **notes-first**: where a directive restates the notes, the notes govern; items marked *(paper-only)* are secondary additions to validate on our data before trusting.

1. **Target = returns (log returns), never price levels.** Prices are non-stationary with update-driven breaks. *(paper-only, no conflict with notes)*
2. **Two things are predictable; direction is the hard one.**
   - *Predictable-ish:* short-term MA-deviation momentum + 7-day mean reversion (Pettersson); conditional **volatility** (Nikolaenko).
   - *Hard:* daily return direction (R² ceiling ≈ 0.5). Plan around a modest edge, not certainty.
3. **Model class:** gradient-boosted trees / Random Forest for the cross-sectional ranker; **skip LSTM/deep nets** until we have individual-transaction data. Keep linear as a baseline.
4. **Volatility-targeted sizing:** estimate conditional vol (GARCH-style); scale position size inversely to forecast vol; place stops/targets in vol units, not fixed %.
5. **Two event classes — treat them differently:**
   - **Balance/meta updates** (buffs, nerfs, trade-up pool changes) → *structural price breaks* → **System A's real alpha**; substitutes reprice (nerf A ⇒ buy B).
   - **Calendar/esports events** (Steam sales, Majors, tournaments, operations) → *volume/liquidity movers, not price* → use for liquidity timing and accumulation detection, not direction.
6. **Fat-tail risk always:** kurtosis >100 means Gaussian assumptions lie. Small per-item caps, hard stops, robust/quantile risk measures.
7. **Substitutes & baskets:** model related items jointly (substitute pairs co-move and swap on balance changes).
8. **Seasonality:** encode Steam Summer/Winter sale windows (liquidation dips = buy opportunity / don't panic-sell).
9. **Validation:** chronological / walk-forward only. Never random splits. Watch train-vs-test gap (regularize).
10. **Data quality caveat:** both papers were data-limited (2 skins; or Steam median only). Our edge partly comes from *better data* (BUFF volume + buy orders + listing depth + float via cs2.sh) — lean on it.

## Open questions to resolve with our own data
- Does the MA-deviation-momentum / 7-day-reversion pattern hold on **BUFF** (vs Steam) and on our item universe?
- Does adding **listing depth + buy-order** features (unavailable to both papers) push R² past ~0.5?
- Can Bai–Perron break detection be run **live** as a fast update-detector to complement the social monitor?
