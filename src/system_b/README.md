# System B — Positional Value / Trend engine

Builder 2's implementation of `docs/System-B_Positional_Value-Trend.md`, grounded by
`docs/System-B_Research-Notes.md` (research pass of 2026-07-17). Shared infrastructure
lives in `src/shared/` (data layer, indicators, regime, ledger, execution, backtester).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[ml,dev]"

# walk-forward backtest on the synthetic market (no API keys needed)
PYTHONPATH=src .venv/bin/python -m system_b.run_backtest --synthetic --items 60 --days 720

# tests
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

With real keys in `.env` (see `.env.example` / HANDOFF §0):

```bash
# daily forward collection (cron this — volume/listings history only accrues forward)
PYTHONPATH=src .venv/bin/python -m shared.collector --data-dir data/panel

# one paper-trading decision cycle on collected data
PYTHONPATH=src .venv/bin/python -m system_b.agent --data-dir data/panel
```

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
`.env`. Backtest artifacts land in `runs/<stamp>/` (equity, attribution, rank-IC,
feature importances, decision journal).
