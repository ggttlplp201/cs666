# HANDOFF — what to give Claude Code to build Systems A & B

The docs + papers give Claude Code the *understanding* and the *how-to-build*. This checklist covers what a document can't contain: **access** (keys, the BUFF execution path) and **human-only decisions** (rules table, factor weights, aesthetics). Work top-down; the shared section unblocks both systems.

Legend: ☐ = to provide · 🔑 = secret (goes in `.env`, never in docs/config/prompts).

---

## 0. Shared setup (BOTH builders, once)

**Context / grounding**
- ☐ Point Claude Code at the repo root so it reads `docs/` + `research/` first (no need to re-explain the market).

**Data access** *(updated after live API testing — see Shared §2a)*
- ☑ 🔑 `cs2.sh` API key (Developer tier) — bid/ask + depth counts via `/v1/prices/latest`, USD-normalized. Volume/float/archive are Scale tier (Phase 2).
- ☐ 🔑 `STEAM_LOGIN_SECURE` cookie — free Phase-1 backtest history (daily median + quantity sold).
- ☐ (Phase 2 only, gated on backtest edge) paid history: cs2.sh Scale one-month bulk download / CSPriceAPI / csmarketapi / SteamAnalyst.
- ☑ cs2.sh field names verified: `buff.ask/ask_volume/bid/bid_volume`, freshness = `collected_at`.

**BUFF execution path (the biggest unblock — without it, everything builds *except* order placement)**
- ☐ Decide: **Option A** official BUFF/NetEase API (Chinese account, ~$150/mo) **or Option B** unofficial session automation.
- ☐ 🔑 Credentials/session for whichever path (from a secrets store, not pasted into a prompt).

**Capital & risk (become `config/shared.yaml` values)**
- ☐ Total capital; currency handling (CNY on BUFF).
- ☐ Per-item / per-category caps, drawdown limits.
- ☐ Confirm BUFF fee (≈2.5%) and T+7 cooldown.

**Ownership**
- ☐ Agree who owns `src/shared/` (data layer, normalizer, signal bus, execution interface, ledger, backtester). Suggested: **Builder 1**, since System A leans on it hardest; Builder 2 consumes it.

**Definition of done (shared):** Claude Code can pull normalized BUFF data, run a backtest with fees + T+7 modeled, and place a *paper* order through the execution interface.

---

## A. System A — Event-Driven (Builder 1)

On top of §0:

**Domain content Claude Code can't infer**
- ☐ **Rules-table content** — the cause→effect maps: the substitute-pair map (M4A4↔M4A1-S, and others), trade-up/pool-change effects, new-case→discontinued-item effects, Armory-re-release→softening.
- ☐ A labeled set of **past update→reaction events** to seed the event-study backtest (which updates hit which items, and how). Claude Code can help assemble; you seed ground truth.

**Social monitor access**
- ☐ 🔑 X/Twitter API access (or chosen scraping method).
- ☐ The **account allowlist** — which leakers / official accounts to follow.
- ☐ Chinese-platform approach (Xiaohongshu/Weibo/BUFF forums).
- ☐ 🔑 LLM for classification (Claude API key, or a self-hosted model).

**Thresholds (→ `config/system_a.yaml`)**
- ☐ Monitor confidence tiers; right-side momentum-confirmation cutoffs; momentum-chase cap (≤2 layers default).

**Definition of done (A):** a leak/update → mapped to affected items via the rules table → liquidity + confirmation checked → paper buy placed within caps; break-detector fires independently on the price feed.

---

## B. System B — Positional (Builder 2)

On top of §0:

**Scope & model-shaping inputs**
- ☐ **Item universe** — full catalogue or a filtered scoring set?
- ☐ **Factor weights & selection thresholds** — confirm/tune the defaults (supply bands, case-price ≥80, ≥3 valid buy orders, category priority).
- ☐ **Aesthetics scores** — the one factor needing a human: your per-item/category art-quality ranking to seed the model.
- ☐ Confirm **model = RF/XGBoost** (per Pettersson); hand over the feature list (adapt Pettersson's Appendix B to BUFF fields).
- ☐ **Holding horizon** (N-day target) and regime-classifier thresholds.
- ☐ Historical lookback / universe for the backtest.

**Definition of done (B):** cross-sectional ranker scores the universe each cycle → top-ranked pass the hard filters + ≥2 accumulation signals → staged paper buys within the layers framework; walk-forward backtest clears a net-of-fee edge threshold.

---

## Credentials safety (applies to everyone)

- Never put API keys, the BUFF password, or session tokens into `docs/`, `config/`, or any prompt Claude Code reads — those get committed/shared.
- Keep them in `.env` / a secrets manager; code reads from there.
- The BUFF execution layer should pull its session from the secrets store at runtime, not from a config file or a pasted prompt.
