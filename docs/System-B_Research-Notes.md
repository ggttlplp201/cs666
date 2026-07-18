# System B — Research Notes & Build Decisions (Builder 2)

> **What this is:** the research pass behind the System B engine build, run 2026-07-17.
> Sources: deep-extraction of both papers in `/research/papers/`, vendor due diligence
> (cs2.sh, csmarketapi, Pricempire), BUFF/CS2 market-mechanics validation as of mid-2026,
> and resolution of the crash-course item nicknames. Everything here either **corrects a
> stale assumption in the docs**, **adds a number the docs lacked**, or **records a build
> decision** and where it's encoded. Precedence rules (RESEARCH_INDEX) were followed:
> notes stay primary for strategy; *venue facts* (fees, locks, Armory mechanics) follow
> the primary-source record below.

---

## 1. Market mechanics — three doc assumptions were stale

| Docs said | Verified reality (mid-2026) | Encoded in |
|---|---|---|
| BUFF fee ≈ 2.5% single-side | **Sell fee cut 2.5% → 1.5% on 2026-04-14** (buff.163.com/news/87397), buyer 0%, withdrawal 1% (cap raised ¥10k→¥50k). English aggregators still show the stale 2.5%. | `config/shared.yaml costs` |
| T+7 = item lock only | Item lock is Valve-side (2018 cooldown + Jul-2025 Trade Protection). **BUFF additionally releases *seller funds* only after the 7-day protection window** → sale proceeds are receivables, not cash, for ~7 days. Also: since ~mid-June 2026 unlocks roll exactly 168h per item (the synchronized daily unlock wave is gone). | `Ledger.settlement_days` + `pending_settlements`; backtest matures receivables daily |
| Armory "re-releases collections" (supply risk) | **The Armory has never re-released an old collection.** It mints *new* collections, then permanently retires them (retired = hard-capped supply). The real risk is (a) Valve never ruled out a return, and (b) *new* collections for old maps (the §15 Cache scenario — note **Cache returned to Active Duty 2026-07-08**). Rotation: Oct-2024 launch → Mar-2025 (Train 2025 in) → Jan-2026 (Harlequin/Achroma) → **Jul-2026 S5 (Arabesque/Spy Tech in; Train 2025 + Sport & Field OUT → fresh post-discontinuation candidates)**. | `ItemMeta.rerelease_risk` semantics; universe notes |

Other verified context: the **Oct 22, 2025 knife/glove trade-up update** crashed market cap ~$5.9B→$3.0B in 24h (recovered >$5B by early 2026) — the crash course (dated 2025-12-05) is *post*-crash; its "mid-tier saturated" framing described the pre-crash bubble. Chinese-source snapshot (Apr 2026): mid-tier gun skins flat/stable, "reasonable entry", high-end knives/gloves −50-75% from peak. `tilt_to_secondary_primaries` stays on but is a config knob, not gospel. Steam sale calendar verified through 2026 (Autumn moved to early Oct starting 2025) → exact dates in `shared/indicators.py STEAM_SALE_DATES`.

## 2. Paper extraction — the numbers that shaped the model

**Pettersson 2025** (640,145 obs, 28 skins, Steam, 2013-2025; full feature table extracted):
- Test R²: **RF 0.494 > XGB 0.448 > Linear 0.421 ≫ LSTM 0.176**. Winning RF config: `n_estimators=200, max_depth=None, min_samples_leaf=5, max_features='sqrt'` (its own tuned variant scored *worse*: 0.484). XGB baseline `500/6/0.05, subsample .8/.8`. → hyperparameter seeds in `system_b/model.py` (leaf sizes raised for our smaller cross-sections; the paper's test-set early stopping — mild leakage — is *not* repeated).
- Dominant predictor: `price_deviation_ma7` (RF importance **0.639**, linear +0.822) = short-horizon momentum; `price_logret_7d` **−0.046** = weekly mean reversion; `Case_price_LogMA7` **−0.157** (rising case prices depress skin returns). → features `ma_dev_7`, `ret_7d`, case-price factor.
- Events on **price ≈ nil**; on **volume**: Operations +33.5%, case-release week +34%, case day +16.6%, S-tier +8.1%, Steam sale +2.5%, **Majors −7.9%**. → calendar features feed liquidity/timing only, never direction.
- Target: next-day log return of daily *median* price; chronological 80/20; no winsorizing (we do winsorize targets at 0.5/99.5% — kurtosis 200 says the paper got lucky).

**Nikolaenko 2025** (M4A4 Desolate Space FT + M4A1-S Decimator FT, BUFF, 826 daily obs):
- Prices non-stationary, returns stationary (ADF −22/−21); returns ≈ white noise (best ARMA(0,0)/AR(1) with −0.146 coeff). Desolate kurtosis **202.6** driven by one +0.87 daily log return (the 18-Nov-2022 M4A1-S nerf).
- GARCH(1,1): α+β = **0.815** (Decimator, vol half-life ~3.4d) and **0.906** (Desolate, ~7.0d) — vol is forecastable; sizing scales inversely (`shared/garch.py`, `volatility_targeting`). (Paper's Decimator SEs are numerically degenerate — point estimates only.)
- **Two break morphologies need two detectors**: nerf-type = instant jump (38σ day — jump filter `|r| > k·σ`) vs buff-type = gradual drift dated *months* late by Bai-Perron (needs CUSUM). → `shared/breaks.py` implements both; System B uses them as regime/retrain alarms, not entries.
- Steam-sale dips confirmed qualitatively (magnitude never quantified; ~0.05-0.15 log by chart read).

## 3. Data vendors — the decisive finding: **nobody sells BUFF executed-volume history**

- **cs2.sh** (docs fully open, `cs2.sh/llms-full.txt`): BUFF `ask/bid/ask_volume/bid_volume` at ~5-min cadence, per-float-range pricing, archive since 2023 with `total_supply`. **`ask_volume`/`bid_volume` are order-book state, never executed trades**; the only sales figure is `aggregate.hourly_volume` (cross-platform aggregate, 1-2×/day updates). All prices **USD** (CNY not exposed). Auth Bearer + mandatory gzip; 10 rps; $75/mo (latest) / $200/mo (history). → adapter `shared/vendors/cs2sh.py` maps exactly these fields; `valid_buy_orders` is emitted as *unknown* (−1) and the hard filter falls back to `buy_order_count`.
- **csmarketapi: DISQUALIFIED** — its API enum has `BUFFMARKET` (buff.market, the thin international site), **no BUFF163 at all**, despite marketing copy. Removed as history source.
- **Pricempire**: the only real BUFF163 history backbone — `buff163` + `buff163_buy` daily series (Enterprise ~$192/mo), **price-only** (no volume/listings), 180d per request, page backwards.
- **Consequence:** accumulation signals (S1/S2 need listings + executed volume) can only be researched on data we collect ourselves. → `shared/collector.py` snapshots the universe daily; every day it runs buys us signal history. Until keys exist, the engine develops against `shared/synthetic.py` (regimes, whale episodes, pumps, fat tails, with ground truth for detector validation).

## 4. Build decisions of record

1. **Structural-composite entry gate.** The §3.2 rule ("high composite + ≥2 accumulation signals") is gated on a momentum-free composite: accumulation phases are flat by definition, so a momentum-weighted floor would systematically exclude exactly the setups the system exists to buy. Momentum (Pettersson's signal) stays in the ML ranker and the full composite. (`system_b/features.py`, `entry.composite_top_pct`)
2. **Sticky signal windows.** S1 is a state; S2/S3 are event-days. "Signals firing simultaneously" = within the same 7-day phase, not the same tick. Validated on synthetic ground truth: catches 4/6 accumulation episodes with multi-day entry windows at ~1.3% false-positive rate. (`shared/indicators.accumulation_signals`)
3. **Decide at t, fill at t+1.** Strategy sees a hard-truncated `PanelView` (look-ahead impossible structurally); orders fill against next-day prices with thin-book caps (≤25% of daily volume, ≤book depth), slippage, and sell-side fee. Walk-forward ranker trains only on target windows fully realized ≥ horizon+1 days before the refit day.
4. **T+7 twice.** Item lock gates exits (`Lot.unlock_day`); *settlement lock gates cash* — sale proceeds mature `settlement_days` later and cannot fund buys meanwhile.
5. **Go-live gate reads per-trade net edge + rank IC**, not just portfolio return: a correctly-cautious paper book (regime ceilings, layer caps) can carry real edge at low deployment. (`go_live_gate` in `config/system_b.yaml`)
6. **One-cycle order cooldown per item** — a resting order is exposure; without it the strategy stacked batch-1 buys on consecutive days while its first order was still in flight.

## 5. Universe resolution (crash-course nicknames → canonical names)

Resolved with high confidence into `config/universe_b.yaml`: 机械工业 = **M4A1-S | Mecha Industries**; 女火神 = **M4A1-S | Chantico's Fire** (Chantico = Aztec *female fire goddess* — not a Vulcan variant); 彼岸花 = **M4A4 | Spider Lily**; **AK-47 | Neon Revolution** (only its FN is still in the ¥1000-3000 band post-crash); 活色生香 = **M4A4 | In Living Color** (active case → fails the ≥¥80-case gate, kept for tracking); "Gamma Glock" = **Glock-18 | Wasteland Rebel**; 沙漠之狐 = **Desert Eagle | Fennec Fox**; **M4A1-S | Icarus Fell**; 地狱犬 "Hellhound" = **Galil AR | Cerberus** (Cache; course's "~7,000 FN" plausible but unverified — SteamDT: MW 10,064 / FT 20,522; its "20-40 trades/day" is an *all-wears* figure — FN alone is single digits and may rightly fail the liquidity gate).

**Excluded pending human confirmation:** 黑莓 "Blackberry" (unresolvable — possibly Glock-18 | Neo-Noir; ask the course author), 绿宝石 "Emerald" (ambiguous: Glock Gamma Doppler Emerald vs knife phases), AK-47 | Fire Serpent (resolved, but ¥5k+ and above our band; "(Hot Rod style)" in the notes is a mix-up — Hot Rod is an unrelated finish). Course-era prices ran ~30-50% above today's (pre-crash quotes) — the §9 tiered buy levels (1750/1550/1100 etc.) must NOT be used as live anchors.

## 5a. Adversarial review round (same day)

A multi-agent adversarial review (5 dimensions, findings independently verified)
plus self-verification confirmed **27 defects**, all fixed with regression tests.
The material ones: same-cycle orders double-spent cash and breached every cap
(no aggregate reservation); paper fills violated limit semantics; sale-side
book capacity was granted per order, not per item-day; bracket triggers were
measured bid-side while entries paid the ask (every lot started ~spread+slip
closer to its stop — stops fired on noise, TPs never); forward-return targets
were row-shifted so gapped panels leaked future returns past the calendar-day
embargo; bus reads had no as-of bound; the Tier-2 buy-pause, loss-limit latch,
stop-cooldown-on-adds, and break-alarm wiring were missing; the paper runner
didn't persist risk state, could double-run a day, and never trained the
ranker. `PanelView.meta` remains a documented static-metadata limitation (the
collector now snapshots meta history so a future as-of store can close it).

## 6. Open items / follow-ups

- **Keys** (HANDOFF §0): cs2.sh Scale key + Pricempire Enterprise trial → backfill the price backbone, verify buff163 depth actually reaches ~5y, start `shared/collector.py` on a daily cron immediately (volume/listings history only accrues forward).
- **CNY conversion**: cs2.sh is USD-only with undocumented FX methodology — decide a house FX source before CNY-denominated live signals.
- `aggregate.hourly_volume` semantics inside 1d buckets are undocumented (rate vs sum) — calibrate against a few days of BUFF page observations once keys exist.
- Aesthetics scores in the universe are provisional seeds — Builder 2 must re-score (HANDOFF §B).
- Whether Harlequin/Achroma remained mintable after the Jul-2026 rotation — affects their retirement-date theses.
- Tier-3 attention feed depends on the shared monitor (System A build); engine degrades gracefully (NullBus) until then.
