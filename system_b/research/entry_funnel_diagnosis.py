"""Where does the positional strategy's trade count actually die?

18 trades in 25 months, with the book in cash 79% of days and a total return of
+0.48% on 100k, is not a strategy — it is a strategy that never got to run. This
walks the funnel stage by stage and reports which gate removes the most, plus
where SIZING (as opposed to selection) throttles deployment.

Runs the real PositionalStrategy on a real panel; the only config change is
allowlisting the universe, because the 97-item draft still carries placeholder
supply/case_price and would otherwise be rejected wholesale by the structural
gates (which is itself one of the findings).

    PYTHONPATH=src:../system_a/src python research/entry_funnel_diagnosis.py \
        data/panel_iflow97_clean
"""

from __future__ import annotations

import collections
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from shared_b.backtest import run_backtest
from shared_b.config import REPO_ROOT, load_config
from shared_b.data import MarketPanel
from shared_b.journal import Journal
from system_b.model import forward_log_returns
from system_b.strategy import PositionalStrategy
from system_b.universe import load_universe

PANEL = sys.argv[1] if len(sys.argv) > 1 else "data/panel_iflow97_clean"
UNIVERSE = sys.argv[2] if len(sys.argv) > 2 else "config/universe_b_draft.yaml"


def main() -> None:
    cfg = dict(load_config("b"))
    uni = load_universe(REPO_ROOT / UNIVERSE)
    # Allowlist the whole universe: waives ONLY the structural gates (supply,
    # case_price) whose inputs are still TODO placeholders. Safety gates (bid
    # depth, volume, pump shape) stay fully enforced — see filters.SAFETY.
    cfg.setdefault("risk_controls", {})["allowlist"] = list(uni)
    cfg.setdefault("universe", {})["universe_path"] = UNIVERSE

    panel = MarketPanel.load(Path(PANEL))
    print(f"panel {PANEL}: {len(panel.frames)} items")
    print(f"universe {UNIVERSE}: {len(uni)} items (allowlisted for this diagnosis)\n")

    out = Path(tempfile.mkdtemp(prefix="funnel_"))
    journal = Journal(out / "journal.jsonl")
    strat = PositionalStrategy(cfg=cfg)
    strat.set_targets(forward_log_returns(panel.frames, int(cfg.get("model", {}).get("horizon_days", 21))))

    res = run_backtest(
        panel=panel, strategy=strat,
        starting_cash=float(cfg.get("capital", {}).get("total", 100_000)),
        fee_pct=float(cfg.get("costs", {}).get("buff_fee_pct", 0.015)),
        slippage_pct=float(cfg.get("execution", {}).get("slippage_pct", 0.005)),
        fill_fraction=float(cfg.get("execution", {}).get("fill_fraction", 0.25)),
        trade_lock_days=int(cfg.get("cooldown", {}).get("trade_lock_days", 7)),
        settlement_days=int(cfg.get("cooldown", {}).get("settlement_days", 7)),
        journal=journal, thesis_lookup=strat.thesis_for,
    )
    s = res.summary()

    # ---------------------------------------------------------------- funnel
    stages = ["scoreable", "pass_filters", "above_floor", "accum_ge2", "candidates"]
    acc = {k: 0 for k in stages}
    days = 0
    days_with_candidate = 0
    deployed = []
    vetoes = collections.Counter()
    buys = 0
    for line in open(out / "journal.jsonl"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "cycle":
            days += 1
            f = (r.get("funnel") or {}) if "funnel" in r else ((r.get("extra") or {}).get("funnel") or {})
            for k in stages:
                acc[k] += int(f.get(k, 0) or 0)
            if int(f.get("candidates", 0) or 0) > 0:
                days_with_candidate += 1
            deployed.append(float(r.get("deployed_pct", 0) or 0))
        elif r.get("kind") == "decision":
            a = str(r.get("action", ""))
            if a.startswith("buy_"):
                buys += 1
            elif a == "veto_buy":
                rule = str(r.get("rule", ""))
                vetoes[rule.split(":", 1)[1] if ":" in rule else rule] += 1

    print("=" * 74)
    print("ENTRY FUNNEL — total item-days surviving each stage")
    print("=" * 74)
    prev = None
    for k in stages:
        v = acc[k]
        drop = "" if prev is None or prev == 0 else f"  (-{100 * (1 - v / prev):.1f}% vs prev)"
        print(f"  {k:<14} {v:>9,}{drop}")
        prev = v
    print(f"\n  cycles run                 : {days:,}")
    print(f"  cycles with ANY candidate  : {days_with_candidate:,} "
          f"({100 * days_with_candidate / max(days,1):.1f}%)")
    print(f"  approved buy decisions     : {buys:,}")
    print(f"  closed trades              : {s.get('n_trades_closed')}")

    print("\n" + "=" * 74)
    print("SIZING — is capital the throttle, once an item IS selected?")
    print("=" * 74)
    d = pd.Series(deployed)
    print(f"  deployed_pct  mean {d.mean()*100:5.2f}%   median {d.median()*100:5.2f}%   "
          f"max {d.max()*100:5.2f}%")
    print(f"  days at 0% deployed        : {(d < 1e-9).sum():,} / {len(d):,} "
          f"({100*(d < 1e-9).mean():.0f}%)")
    print(f"  regime deployment ceiling  : bull 80% / sideways 50% / bear 30% / weak 20%")
    print("  -> if max deployed is far under the ceiling, the ceiling is NOT the binding")
    print("     constraint; per-item allocation and staged batching are.")

    if vetoes:
        print("\n" + "=" * 74)
        print("RISK-GATE VETOES on items that already cleared selection")
        print("=" * 74)
        for reason, n in vetoes.most_common(12):
            print(f"  {n:>6,}  {reason[:60]}")

    print("\n" + "=" * 74)
    print("RESULT")
    print("=" * 74)
    for k in ("n_trades_closed", "win_rate", "avg_trade_return_net", "total_return",
              "max_drawdown", "go_live_gate_pass"):
        print(f"  {k:<22} {s.get(k)}")
    print(f"\n  artifacts: {out}")


if __name__ == "__main__":
    main()
