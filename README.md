# CS2 Quant — Skin Trading Agents (Systems A & B)

Two independent trading agents on the CS2 skin market (BUFF163).

- **System A — Event-Driven / Reactive**: trades game-update / balance-change repricings. Alpha = information.
- **System B — Positional Value / Trend**: accumulates undervalued items on structural factors. Alpha = factor edge + patience.

Each system is a **self-contained folder** — its own `src/`, `config/`, `docs/`,
`tests/`, `dashboard/` and runtime `var/`. Either could be cloned out and run on
its own. The shared knowledge layer (the Shared indicator doc, the research
index, the papers) is *duplicated* into both rather than referenced across the
boundary, so neither system can break the other.

Start with `system_<x>/docs/Shared_Market-Fundamentals_Indicator-Library.md` and
`system_<x>/docs/RESEARCH_INDEX.md`, then that system's own doc.

---

## Repo layout

```
cs2-quant/
├── README.md            ← this file
├── HANDOFF.md           ← what each builder must supply to build A / B
├── Makefile             ← dispatches into both systems (make test / demo / b-backtest-real)
├── pyproject.toml       ← deps + pytest config for both suites
├── .env                 ← secrets (NOT committed); shared by both systems
│
├── system_a/            ← Event-Driven / Reactive — self-contained
│   ├── src/shared/          data layer, feed, store, indicators, ledger, backtester
│   ├── src/system_a/        engine, monitor, rules table, event/spread/exit studies
│   ├── config/              shared.yaml, system_a.yaml, rules_table_a.yaml, …
│   ├── docs/                Shared + RESEARCH_INDEX + System-A doc (+ pdf/)
│   ├── research/            papers + crash course
│   ├── dashboard/           read-only Streamlit research dashboard
│   ├── tests/               141 tests
│   └── var/, runs/          runtime state (gitignored): market.db, provenance, archives
│
└── system_b/            ← Positional Value / Trend — self-contained
    ├── src/shared_b/        parallel data layer, indicators, regime, ledger, backtester,
    │                        synthetic market, real_panel (real-history builder)
    ├── src/system_b/        features, filters, ranker, strategy, risk gate, runner
    ├── config/              shared.yaml, system_b.yaml, universe_b.yaml
    ├── docs/                Shared + RESEARCH_INDEX + System-B docs (+ pdf/)
    ├── research/            papers + crash course
    ├── dashboard/           React + TS run-artifact dashboard
    ├── tests/               82 tests
    └── var/, runs/          runtime state (gitignored): panels, backtest artifacts
```

**Why two self-contained folders rather than a shared layer:** A and B were built
by different people at different times and their infrastructure has already
diverged — A's `shared/` grew live feeds and a SQLite store, B's `shared_b/` did
not. Forcing them back onto one layer would couple two systems that are supposed
to be independent tests of two different theses. The knowledge layer (Shared doc,
research index, papers) is duplicated instead; when you edit one, mirror it.

Package names don't collide — A owns `shared`/`system_a`, B owns
`shared_b`/`system_b` — so `pytest` at the root runs both suites, while
`cd system_b && pytest` runs one alone.

---

## Who reads / edits what

Paths below are relative to a system folder (`system_a/` or `system_b/`).

| Artifact | Format | Audience | Editable? |
|---|---|---|---|
| `docs/*.md` | Markdown | **Claude Code** (canonical source) + humans | **Yes — edit these** |
| `docs/pdf/*.pdf` | PDF | humans (devs, stakeholders) | No — regenerate from md |
| `research/papers/*.pdf` | PDF | Claude Code + humans (reference) | No |
| `research/crash course.txt` | Text | Claude Code + humans (PRIMARY notes) | Rarely |
| `config/*.yaml` | YAML | code (runtime) + devs (tuning) | **Yes — the fast-adjust path** |
| `../.env` | env | code only | Yes, never commit |

Shared docs (`Shared_Market-Fundamentals_Indicator-Library.md`, `RESEARCH_INDEX.md`)
and `research/` exist in **both** system folders. Edit one, mirror the other.

**Precedence reminder** (see `docs/RESEARCH_INDEX.md`): the crash-course notes + `docs/` are PRIMARY; the two papers are SECONDARY (corroboration + backlog to test). Notes win in live trading.

---

## Quick start per builder

**Both, once:** point Claude Code at the system folder you're working in so it
reads that folder's `docs/` + `research/` first. Fill the root `.env` and the
system's `config/shared.yaml` (fees, capital, data keys, execution path). See
`HANDOFF.md`.

- **System A:** work in `system_a/`. Read `system_a/docs/System-A_Event-Driven_Reactive.md`. Supply the rules-table content + social-monitor access (HANDOFF §A).
- **System B:** work in `system_b/`. Read `system_b/docs/System-B_Positional_Value-Trend.md`. Supply factor weights + aesthetics scores + item universe (HANDOFF §B).

```bash
make test              # both suites (223)
make test-a            # System A alone (141)
make test-b            # System B alone (82)
make demo              # System A end-to-end paper demo
make dashboard         # System A read-only research dashboard
make b-panel           # build System B's real BUFF panel from var/market.db
make b-backtest-real   # System B walk-forward on that real panel
```

---

## Implementation status (System A)

`system_a/src/shared/` and `system_a/src/system_a/` are implemented and tested
(paper mode only). Live data/execution stay disabled until the placeholder keys
in `.env` are replaced — see HANDOFF §0/§A for what's still human-supplied
(rules-table content, account allowlist, API keys).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyyaml numpy pandas scipy scikit-learn pytest streamlit
.venv/bin/pytest        # both suites (223)
make demo               # end-to-end paper demo
```

The demo synthesizes an M4A1-S nerf: monitor classifies the posts → signal
bus → rules table maps the substitute (M4A4) → right-side confirmation →
risk gate → paper buy → T+7 hold → take-profit exit, all provenance-logged
to `var/provenance_a.jsonl`.

---

## System A research dashboard (read-only)

### Running it locally (works on a fresh clone — no API keys needed)

```bash
git clone https://github.com/ggttlplp201/cs666.git && cd cs666
python3 -m venv .venv
.venv/bin/python -m pip install pyyaml numpy pandas scipy scikit-learn streamlit
make dashboard          # → opens http://localhost:8501
```

No `.env` is required — the dashboard is read-only and never touches secrets.
It renders from local data files that are **not in git** (`var/` is ignored):

- `var/market.db` — poller snapshots + Steam backtest history. **Without it,
  pages show "no poller data yet" placeholders** — that means missing data,
  not a broken install. Either copy `var/market.db` from the machine running
  the poller (ask Leon — single SQLite file, safe to share, contains no
  credentials), or generate your own history with a Steam session cookie:
  `cd system_a && PYTHONPATH=src ../.venv/bin/python -m shared.steam_history`.
- `var/provenance_a.jsonl` — decision log for the Prediction Log page.
  `make demo` generates one from the synthetic paper demo in seconds.

Both live under `system_a/var/`.

If port 8501 is taken: `cd system_a && ../.venv/bin/streamlit run dashboard/app.py --server.port 8502`.
(No `make` on the machine? Run that same `streamlit run` command directly.)

### What each page answers

The dashboard cannot trade or change config — read-only by construction.
Start on **Overview** — a plain-language, one-screen summary of what we found
(reactive dead, trade-up alive) with the headline chart. Then:

1. **Data health** — is the poller alive? Gaps in the series are loud (a
   silently broken poller is the failure mode we care most about).
2. **Live market** — current book per item: ask/bid, spread, depth, staleness.
3. **Spread analysis** — what trading costs: spread distribution + the
   spread-vs-liquidity relationship that killed reactive entry.
4. **Rule scorecard** — per-rule out-of-sample results and current gating
   (in-sample numbers are quarantined, never mixed in).
5. **Event timeline** — every labeled event, prediction vs realized, plus the
   live forward tests (2026-07-09 Cache/Armory).
6. **Prediction log** — browsable provenance: which signals fired, which rule
   decided, and why.
7. **Trade-up class** — the one viable event class: negative-control results,
   collection-map coverage, last end-to-end paper run.
8. **Monopoly watch** — item classes ranked by monopolization (high barrier +
   thin supply) — the predictive read on Valve's likely next access target.

A persistent banner shows the operating mode (LOG-ONLY), spend to date ($0),
and how many rules are currently DO-NOT-TRADE.

---

## Regenerating the PDFs

The `.md` files are canonical; the PDFs in `docs/pdf/` are snapshots. After editing a doc, regenerate its PDF (e.g. `markdown` → HTML → `wkhtmltopdf`, or `pandoc`). Never edit the PDF directly.

---

## Reading order (new contributor / Claude Code)

1. `docs/Shared_Market-Fundamentals_Indicator-Library.md` — how the market works + governance (§12).
2. `docs/RESEARCH_INDEX.md` — what the papers add, and the notes-first precedence.
3. Your system doc (A or B).
4. `docs/System-B_Research-Notes.md` — Builder 2's 2026-07 research pass: verified venue
   facts (fee cut to 1.5%, T+7 seller-fund settlement, Armory mechanics), vendor due
   diligence (csmarketapi has NO BUFF163; volume history must be self-collected),
   paper number extraction, and the System B engine's build decisions.
5. `research/papers/paper1.pdf` / `paper2.pdf` — only when you need a specific method or number.

## System B engine (built, and now tested on real data)

`system_b/src/shared_b/` + `system_b/src/system_b/` contain the positional engine:
normalized data layer, indicator library, regime classifier, T+7-aware ledger
(item lock AND seller-fund settlement), paper broker, walk-forward backtester,
factor/accumulation-signal strategy, risk gate, provenance journaling, synthetic
market for key-less development, and a daily paper-trading runner.

Its original data path (cs2.sh) died with the vendor on 2026-07-25.
`shared_b/real_panel.py` replaces it by joining the BUFF iflow archive (book) with
Steam history (executed volume) out of `var/market.db`. On that real panel the
cross-sectional ranker shows a **statistically significant edge** (mean rank IC
0.137, t=3.73, p=0.0003; robust across xgboost/RF/ridge) — but the trading layer
never uses it: closed trades are identical whichever model runs. The go-live gate
correctly HOLDs.

See `system_b/src/system_b/README.md` for the full result table, the three data
caveats, and the two documented limitations.
