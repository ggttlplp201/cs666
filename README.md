# CS2 Quant — Skin Trading Agents (Systems A & B)

Two independent trading agents on the CS2 skin market (BUFF163), sharing one knowledge layer, data layer, and signal bus.

- **System A — Event-Driven / Reactive** (Builder 1): trades game-update / balance-change repricings. Alpha = information.
- **System B — Positional Value / Trend** (Builder 2): accumulates undervalued items on structural factors. Alpha = factor edge + patience.

Start with `docs/Shared_Market-Fundamentals_Indicator-Library.md` and `docs/RESEARCH_INDEX.md`, then your system's doc.

---

## Repo layout

```
cs2-quant/
├── README.md                      ← this file
├── HANDOFF.md                     ← what each builder must give Claude Code to build A / B
├── docs/                          ← CANONICAL, editable source (Claude Code reads these)
│   ├── Shared_Market-Fundamentals_Indicator-Library.md
│   ├── RESEARCH_INDEX.md
│   ├── System-A_Event-Driven_Reactive.md
│   ├── System-B_Positional_Value-Trend.md
│   └── pdf/                        ← generated snapshots (humans read these; do NOT edit)
│       └── (same four, .pdf)
├── research/
│   ├── papers/
│   │   ├── paper1.pdf              ← Nikolaenko — ARMA-GARCH, stationarity, structural breaks
│   │   └── paper2.pdf              ← Pettersson — ML price prediction (RF/XGBoost/LSTM)
│   └── crash course.txt            ← original practitioner notes (PRIMARY market source)
├── config/                         ← hot-editable knobs (see docs/Shared §12)
│   ├── shared.yaml
│   ├── system_a.yaml
│   └── system_b.yaml
├── .env                            ← secrets (NOT committed) — API keys, BUFF session
└── src/
    ├── shared/                     ← data layer, normalizer, signal bus, execution iface, ledger, backtester
    ├── system_a/                   ← Builder 1's code
    └── system_b/                   ← Builder 2's code
```

**Why "shared + two" and not two separate folders:** A and B depend on the *same* Shared doc, papers, crash course, and infrastructure. Keeping one shared copy prevents drift (edit the Shared doc once, both systems see it) and keeps the docs' internal `/docs` and `/research/papers` links valid. The two-way split lives in `src/` (`system_a` / `system_b`).

---

## Who reads / edits what

| Artifact | Format | Audience | Editable? |
|---|---|---|---|
| `docs/*.md` | Markdown | **Claude Code** (canonical source) + humans | **Yes — edit these** |
| `docs/pdf/*.pdf` | PDF | humans (devs, stakeholders) | No — regenerate from md |
| `research/papers/*.pdf` | PDF | Claude Code + humans (reference) | No |
| `research/crash course.txt` | Text | Claude Code + humans (PRIMARY notes) | Rarely |
| `config/*.yaml` | YAML | code (runtime) + devs (tuning) | **Yes — the fast-adjust path** |
| `.env` | env | code only | Yes, never commit |

**Precedence reminder** (see `docs/RESEARCH_INDEX.md`): the crash-course notes + `docs/` are PRIMARY; the two papers are SECONDARY (corroboration + backlog to test). Notes win in live trading.

---

## Quick start per builder

**Both, once:** point Claude Code at the repo root so it reads `docs/` + `research/` before writing code. Fill `.env` and `config/shared.yaml` (fees, capital, data keys, execution path). See `HANDOFF.md`.

- **Builder 1 (System A):** work in `src/system_a` + `src/shared`. Read `docs/System-A_Event-Driven_Reactive.md`. Supply the rules-table content + social-monitor access (HANDOFF §A).
- **Builder 2 (System B):** work in `src/system_b` + `src/shared`. Read `docs/System-B_Positional_Value-Trend.md`. Supply factor weights + aesthetics scores + item universe (HANDOFF §B).

---

## Implementation status (Builder 1: shared + System A)

`src/shared/` and `src/system_a/` are implemented and tested (paper mode only).
Live data/execution stay disabled until the placeholder keys in `.env` are
replaced — see HANDOFF §0/§A for what's still human-supplied (rules-table
content, account allowlist, API keys).

```bash
python3 -m venv .venv && .venv/bin/pip install pyyaml pytest
.venv/bin/pytest                                        # test suite
PYTHONPATH=src .venv/bin/python -m system_a.runner --demo   # end-to-end paper demo
```

The demo synthesizes an M4A1-S nerf: monitor classifies the posts → signal
bus → rules table maps the substitute (M4A4) → right-side confirmation →
risk gate → paper buy → T+7 hold → take-profit exit, all provenance-logged
to `var/provenance_a.jsonl`.

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

## System B engine (built)

`src/shared/` + `src/system_b/` contain Builder 2's engine: normalized data layer,
indicator library, regime classifier, T+7-aware ledger (item lock AND seller-fund
settlement), paper broker, honest walk-forward backtester, factor/accumulation-signal
strategy, risk gate, provenance journaling, synthetic market for key-less development,
and a daily paper-trading runner. See `src/system_b/README.md` for usage.
