# System B — Positional Value / Trend engine

Implementation of `system_b/docs/System-B_Positional_Value-Trend.md`, grounded by
`system_b/docs/System-B_Research-Notes.md` (research pass of 2026-07-17). Shared
infrastructure lives in `system_b/src/shared_b/` — B's own parallel data layer,
indicators, regime classifier, ledger, execution and backtester. (System A has a
separate `shared/` layer; the two are deliberately independent.)

## Quick start

```bash
# from the repo root
python3 -m venv .venv && .venv/bin/pip install -e ".[ml,dev]"

cd system_b

# walk-forward backtest on the synthetic market (no API keys needed)
PYTHONPATH=src ../.venv/bin/python -m system_b.run_backtest --synthetic --items 60 --days 720

# walk-forward backtest on REAL collected history
PYTHONPATH=src ../.venv/bin/python -m shared_b.real_panel                    # build var/panel_real
PYTHONPATH=src ../.venv/bin/python -m system_b.run_backtest --data-dir var/panel_real

# tests
../.venv/bin/pytest -q
```

Or from the repo root: `make b-panel && make b-backtest-real`.

## Data status — read before trusting a number

**The engine's original data path is dead.** It was built against cs2.sh
(`shared_b/vendors/cs2sh.py`, `shared_b/collector.py`); that key expired and the
vendor was dropped repo-wide on 2026-07-25. Nothing collects forward for B today.

Real backtests therefore run off history System A collected, via
`shared_b/real_panel.py`, which **joins two sources** because neither is
sufficient alone:

| Source | Days | Book (ask/bid/depth) | Executed volume |
|---|---|---|---|
| `buff_iflow` | 430 | yes — real BUFF | no — NULL throughout |
| `steam` | 4,595 | no — zeros, bid == ask | yes — real |

B's hard filters gate on bid depth **and** 20-day average trades, and both are
safety gates the allowlist may not bypass — so a single-source panel rejects
every item, every day. The join takes price/book from BUFF and volume from Steam.

The builder drops book rows it cannot trade against — `highest_buy == 0` (the old
iflow schema's "bid unavailable" sentinel) and crossed books where `bid > ask`
(6.8% of raw rows in the usable window; economically impossible at one instant,
so the sides were sampled at different times and buying the ask / selling the bid
would be a free lunch that does not exist). It also labels each source's day in
that source's own calendar: iflow filenames are UTC+8, Steam rows are midnight
UTC. Flooring iflow in UTC dated 99% of rows a day early, which put *tomorrow's*
BUFF book on today's row — a look-ahead leak, since fixed.

Three caveats travel with every real-data result:

1. **Cross-venue volume.** Steam executed volume is a *proxy* for BUFF volume,
   not the same quantity. Volume-derived factors (whale accumulation,
   volume-without-price) are directional evidence, not calibrated.
2. **Sparse book.** The usable window (2025-09-22 → 2026-05-19) carries a median
   of ~7 of 19 items per day. Gaps are bridged by a bounded forward-fill of the
   book (`--max-stale-days`, default 3); volume is never filled. ~40% of rows in
   the default panel are carried. Run `--max-stale-days 0` for observations only.
3. **Unknown structural metadata.** Supply, case price and aesthetics are
   human/vendor-supplied (HANDOFF §B) and don't exist for these 19 items, so they
   stay at unknown defaults and the items are allowlisted in
   `config/system_b.yaml` to waive the *structural* gates. Safety gates (depth,
   volume, pump shape) remain enforced. **The structural-selection half of the
   thesis is therefore untested on real data.**

## Measured results (2026-07-26)

All figures below are post-fix (timezone alignment + invalid-book filtering).

| Run | Window | Trades | Avg trade (net) | Total return | Mean rank IC |
|---|---|---|---|---|---|
| Synthetic, 720d | — | 8 | −0.85% | −0.19% | 0.010 |
| Real, filled book | 2025-11-21 → 2026-05-20 | 3 | −1.81% | −0.03% | **0.137** (t=3.73, p=0.0003) |
| Real, observations only | same | 2 | −11.2% | −0.30% | **0.409** (t=3.86, p=0.002, n=15) |

The go-live gate correctly **HOLDs** in every run.

Two findings worth acting on:

- **The ranker has real cross-sectional signal**, and it is not an artifact of
  the look-ahead leak — closing that leak *raised* the IC (0.094 → 0.137). It is
  significant and robust across model types on the same panel: xgboost 0.137
  (p=0.0003), random_forest 0.136 (p=0.0003), ridge 0.085 (p=0.032). On the
  synthetic market it is ~0.01, so the simulator badly *understates* the model.
  Sample is still small: 102 IC days, 19 items, one 6-month window.
- **That signal never reaches execution.** Closed trades are *identical* across
  all three model types — same items, same days, same P&L to the cent. Entry is
  decided by the structural composite + accumulation gates; the ranker only
  orders a queue too short for ordering to matter. Only 2 distinct items ever
  traded. Improving the ranker will change nothing until the entry path actually
  consumes its ranking. **This is the highest-value thing to fix.**

Two known limitations, documented rather than fixed:

- Carried book rows are indistinguishable from observed ones in the panel, so
  features and fills can transact against a stale book with same-day volume.
  Use `--max-stale-days 0` to eliminate them (at the cost of a thinner panel).
- Dropping thin cross-sections leaves calendar gaps, so an order placed at `t`
  may fill later than `t+1`. This is inherent to sparse real history, not to the
  pruning, but it does soften the "fills next day" guarantee.

## The daily cycle (`system_b/strategy.py`)

1. data sanity → pause on stale feed
2. regime classification (Shared §2) → deployment ceiling
3. **exits first**: TP +10/15% (trim/full), SL −10% cut / −18% liquidate, thesis-break
   on Tier-2 confirmed events, distribution-shape exit, bear-regime cuts — all
   T+7-aware, scale-out capped by book depth
4. features for the whole universe (`system_b/features.py`) → hard filters (Shared §4.3)
5. entry rule: structural-composite floor + ≥2 whale-accumulation signals
   (+ early Tier-3 attention); ML ranker (walk-forward XGB/RF) orders the queue
6. staged left-side builds: batch 1 now, adds only at −10% support after prior batch CD
7. risk gate (`system_b/risk.py`): regime ceilings, layers caps, vol-targeted sizing,
   volume-relative exit-ability cap, locked-capital cap, loss-limit halts, cooldowns
8. every decision journaled with full provenance (Shared §12)

## Honesty guarantees (backtest = paper = live code path)

- strategies only see `PanelView` — history hard-truncated at the decision day
- decisions at day t fill at day t+1 prices, capped at 25% of traded volume and book depth
- 1.5% sell fee (post Apr-2026 cut), slippage, T+7 item lock, **T+7 seller-fund settlement**
- ranker refits walk-forward with a horizon-length embargo; targets winsorized

## Config

Knobs live in `config/shared.yaml` + `config/system_b.yaml` (hot-editable, Shared §12);
the item universe + human aesthetics scores in `config/universe_b.yaml`. Secrets only in
the repo-root `.env`. Backtest artifacts land in `runs/<stamp>/` (equity, attribution,
rank-IC, feature importances, decision journal).
