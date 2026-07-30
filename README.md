# CS2 Quant — skin trading agents (BUFF163)

Two trading agents on the CS2 skin market, both paper-only.

- **System A — event-driven.** Trades repricings caused by game updates and
  balance changes. Edge is information: detect the announcement, act before the
  market finishes moving.
- **System B — positional.** Buys items that look structurally cheap and holds.
  Edge is factor selection plus patience.

Neither is cleared to trade live. Every run is paper; `execution.paper_mode` is
`true` and the go-live gate decides when that can change.

## What we have actually established

| Claim | Status |
|---|---|
| Reactive/event entry on ordinary updates | **Dead.** The spread is wider than the move. |
| Trade-up announcements as an event class | **Alive and large.** 2025-10-22 repriced trade-up fuel 10–25× in days. |
| System B's cross-sectional ranker | Real edge (rank IC 0.137, p=0.0003) that **never reaches an order** — the ≥2-signal entry gate empties the funnel first. |
| System B's tuned exits (go-live gate pass, n=18) | **Not trusted.** See `system_b/docs/EXIT_COST_FLOOR.md`. |
| Greedy ratchet strategy | Implemented and measured. Loses on the median trade; positive mean only from the 2025-10 event. |

The single most important number in this repo is the **round-trip cost floor**:
median ask→bid spread 3.37% + 1.5% BUFF fee + ~1% slippage ≈ **5.87%**. Any exit
threshold tighter than that trades the spread rather than the price. It is why
a "−2% stop" realizes about −7.6%, and why several tuned parameters that looked
good on 18 trades are wrong.

## How it works

Both systems share the same skeleton, in two parallel packages (`shared` +
`system_a`, `shared_b` + `system_b`):

**Data.** Normalized daily bars per item — ask, bid, listing/bid depth, volume.
System A polls live (Steam via Scrapling) into a SQLite store. System B builds
historical panels from the free iflow.work BUFF archive
(`shared_b/vendors/iflow_archive.py`). Prices are CNY. Volume is a Steam 24h-sold
**proxy** — the archive has no BUFF executed volume, so any volume threshold
reads tight against it.

**Decide.** A strategy sees a `PanelView` truncated to the decision day, so it
structurally cannot look ahead. It emits `Order`s and nothing else.

**Gate.** A risk gate sizes and can veto: regime ceiling, category and per-item
caps, locked-capital cap, volatility targeting, cash on hand (minus cash already
claimed by resting orders), post-stop cooldowns.

**Fill.** Orders decided at day *t* fill at *t+1* against observed book, capped
by depth and by a share of that day's volume. Fees, slippage and the **T+7 trade
lock** are modeled. Ignoring fills and the lock is what produces a beautiful,
fake equity curve, so none of it is optional.

**Record.** Every decision is journalled with the features that caused it, so any
trade can be explained after the fact.

The same strategy object runs in the backtester and in live paper mode — one code
path, no separate "live" implementation to drift.

### System B strategies

Selected with `--strategy`:

- **`positional`** (default) — hard filters → structural composite → requires ≥2
  accumulation signals → walk-forward ranker orders the survivors → staged entry
  in batches at support. Exits on ±brackets, thesis breaks, distribution shape,
  and bear-regime cuts.
- **`greedy`** (`system_b/greedy.py`) — deliberately simple price action. Buy
  anything passing the liquidity/safety gates. At +10% the ratchet *arms* and
  tracks a high-water return; give back 1 point from that high and sell
  everything. If it hits −5% before arming, sell and stop. Either way the item
  goes on a watch list at the exit price, and re-enters if price dips below and
  returns to it.

  Two facts constrain it, both surfaced rather than hidden. The T+7 lock means it
  cannot "sell immediately" — the high-water mark tracks through the lock and the
  exit fires at the first unlocked cycle where it still holds
  (`lock_blocked_exits` counts the deferrals). And a 1-point trail sits inside
  the 3.37% spread, so `spread_aware` (default on) floors the give-back at the
  spread and widens the stop to the cost floor. `--greedy-literal` runs the raw
  spec instead; both are measured, neither assumed.

## Layout

```
├── Makefile              dispatches into both systems
├── pyproject.toml        deps + pytest config for both suites
├── .env                  secrets, never committed
├── system_a/             event-driven — src/{shared,system_a}, config, docs,
│                         tests (181), Streamlit research dashboard
└── system_b/             positional — src/{shared_b,system_b}, config, docs,
                          tests (150), React run-artifact dashboard
```

Each folder is self-contained: its own `src/`, `config/`, `docs/`, `tests/`,
`dashboard/`, and gitignored `var/` + `runs/`. Package names don't collide, so
root `pytest` runs both suites and `cd system_b && pytest` runs one.

**One real coupling exists:** `shared_b/vendors/iflow_archive.py` imports System
A's `shared.iflow_history` for the archive download cache, so B is not currently
standalone. `system_b/conftest.py` bootstraps A's `src/` to cover it. Merging A
into B is the planned fix.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyyaml numpy pandas scipy scikit-learn pytest streamlit

make test                # both suites (331)
make demo                # System A end-to-end paper demo
make dashboard           # System A research dashboard (read-only, no keys needed)
```

System B:

```bash
make b-backtest          # walk-forward on the synthetic simulator (no data needed)
make b-panel-archive     # build the 97-item panel from the iflow archive
make b-backtest-real     # positional strategy on the real panel
make b-greedy            # greedy ratchet, spread-aware defaults
make b-greedy-literal    # greedy ratchet, spec thresholds verbatim
make b-greedy-sweep      # sweep greedy thresholds over every item-day
make b-cost-floor        # measure the cost floor and what each trigger realizes
make b-dashboard         # React dashboard over run artifacts (needs bun)
```

The A demo synthesizes an M4A1-S nerf end to end: monitor classifies posts →
signal bus → rules table maps the substitute (M4A4) → confirmation → risk gate →
paper buy → T+7 hold → take-profit exit, all logged to `var/provenance_a.jsonl`.

## Measuring a strategy honestly

A capital-constrained backtest closes only ~25 trades on a 6-month panel, which
cannot separate signal from noise — that is exactly how the n=18 exit tuning went
wrong. So parameters are tested two ways:

- **`research/greedy_sweep.py`** opens a notional position on every item-day and
  runs the rules forward, giving thousands of round trips per parameter set. It
  answers "do these thresholds extract money from this price series".
- **`run_backtest`** answers the different question of what the book would have
  returned under real sizing, caps and concurrency.

Both report a **pre/post sample split**. A parameter set that is positive overall
but negative in one half is fitted to the other half. Note that the 2024-02 →
2026-05 window contains the 2025-10 trade-up repricing, which is large enough to
carry an entire backtest on its own; a split that puts it wholly in one half
proves nothing.

## Data caveats

1. Archive prices are **CNY, not USD** (the older docstring in
   `shared/iflow_history.py` is wrong). Do not apply FX on top.
2. BUFF bids only exist in the archive from **2024-02-13**. Earlier bars would
   need fabricated bids, which would corrupt exit fills and marks.
3. `volume` is a **Steam 24h-sold proxy**, never BUFF executed volume.
4. Archive coverage flickers (~2.5–3.5k items tracked per snapshot). Missing days
   stay missing; `PanelView.today()` treats anything over 3 days old as stale.
5. `config/universe_b_draft.yaml` (97 items) has **placeholder** supply,
   case_price and aesthetics — human inputs per `HANDOFF.md` §B. Until they are
   filled, structural gates on those items are meaningless.

## Docs

`docs/*.md` is canonical; PDFs in `docs/pdf/` are snapshots — regenerate, never
edit. The crash-course notes and `docs/` are **primary**; the two papers are
secondary corroboration. Shared docs are duplicated into both system folders on
purpose — edit one, mirror the other.

Reading order:

1. `system_<x>/docs/Shared_Market-Fundamentals_Indicator-Library.md` — how the
   market works, plus governance (§12).
2. `system_<x>/docs/RESEARCH_INDEX.md` — what the papers add, notes-first
   precedence.
3. Your system's own doc.
4. `system_b/docs/EXIT_COST_FLOOR.md` — the cost floor, and which tuned
   parameters it invalidates. Read before changing any exit threshold.
5. `HANDOFF.md` — what each builder still has to supply.
