# System B dashboard

A small React + TypeScript dashboard for the System B (positional value/trend)
engine's run artifacts. Zero chart dependencies — hand-rolled SVG line/bar
charts with crosshair tooltips, light/dark themes, and a table view.

## Run it

```bash
cd dashboard
bun install        # or npm install
bun run dev        # or npm run dev
```

It opens with a bundled sample run (synthetic-market walk-forward backtest —
not live results). To inspect a real run, **drag the files from any
`runs/<stamp>/` directory onto the page** (or use "load run files"):

- `summary.json` — KPI tiles + go-live gate
- `equity.csv` — equity curve
- `journal.jsonl` — regime timeline + deployment
- `rank_ic.csv` — walk-forward model IC
- `attribution.csv` — closed lots + P&L by exit rule
- `feature_importances.csv` — latest refit's importances

Any subset works; missing files just leave their card empty.

## What it shows

- **KPI row** — final equity, annualized return, max drawdown, closed trades /
  win rate, average net trade return, mean rank IC, and the **go-live gate**
  (the engine refuses live capital until paper results clear a net-of-cost
  edge threshold — HOLD is the expected state until then).
- **Equity curve** — marked at exit-side bids, net of the 1.5% BUFF fee.
- **Market regime & deployment** — the regime classifier's timeline (bull /
  sideways / bear / weak; bear additionally hatched so the red–green pair
  never relies on hue alone) under the deployed-% line it gates.
- **Walk-forward rank IC** — daily Spearman IC of the ranker vs realized
  forward returns, with a 30-day mean.
- **Net P&L by exit rule** — which exit rules make and lose money (diverging).
- **Feature importances** — what the latest refit of the ranker actually uses.
- **Closed lots** — the full trade-journal attribution table.

## Refresh the bundled sample

```bash
node scripts/make-sample.mjs ../runs/<stamp> "run name"
```

## Build

```bash
bun run build      # tsc -b && vite build -> dist/
```
