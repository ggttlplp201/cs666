"""Measure what actually lifts the positional strategy's deployment.

The funnel diagnosis showed the >=2-accumulation-signal gate removing 96.2% of
everything that reaches it (12,413 item-days -> 475), while the risk gate vetoed
only 18 orders in the whole run. So selection, not sizing, is the binding
constraint — but per-trade edge is decent (+5.6% net, 78% win), so the prize is
applying that edge to more than 0.6% of the book.

Each variant changes ONE thing from the baseline so the attribution is clean.
Reports trades, deployment, per-trade edge and TOTAL return, because more trades
at a smaller size is not progress.

    PYTHONPATH=src:../system_a/src python research/entry_gate_variants.py
"""

from __future__ import annotations

import json
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

PANEL = "data/panel_iflow97_clean"
UNIVERSE = "config/universe_b_draft.yaml"

VARIANTS: list[tuple[str, dict]] = [
    ("baseline (min_sig=2, batches=4)", {}),
    ("min_signals 2->1", {"entry.min_accumulation_signals": 1}),
    ("model substitution ON", {"entry.model_signal_substitution.enabled": True}),
    ("batches 4->2", {"staged_entry.batches_per_item": 2}),
    ("max_new 3->8", {"entry.max_new_positions_per_cycle": 8}),
    ("min_sig=1 + batches=2 + max_new=8", {
        "entry.min_accumulation_signals": 1,
        "staged_entry.batches_per_item": 2,
        "entry.max_new_positions_per_cycle": 8,
    }),
]


def setdeep(d: dict, dotted: str, val) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = val


def run(label: str, overrides: dict, panel: MarketPanel, uni: list[str], targets) -> dict:
    cfg = dict(load_config("b"))
    cfg.setdefault("risk_controls", {})["allowlist"] = list(uni)
    for k, v in overrides.items():
        setdeep(cfg, k, v)

    out = Path(tempfile.mkdtemp(prefix="var_"))
    journal = Journal(out / "journal.jsonl")
    strat = PositionalStrategy(cfg=cfg)
    strat.set_targets(targets)
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
    dep = []
    for line in open(out / "journal.jsonl"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "cycle":
            dep.append(float(r.get("deployed_pct", 0) or 0))
    d = pd.Series(dep) if dep else pd.Series([0.0])
    return {
        "variant": label,
        "trades": s.get("n_trades_closed", 0),
        "win": s.get("win_rate"),
        "avg_net": s.get("avg_trade_return_net"),
        "total": s.get("total_return"),
        "maxDD": s.get("max_drawdown"),
        "dep_mean": d.mean(),
        "dep_max": d.max(),
        "idle_days": float((d < 1e-9).mean()),
    }


def main() -> None:
    panel = MarketPanel.load(Path(PANEL))
    uni = load_universe(REPO_ROOT / UNIVERSE)
    cfg0 = load_config("b")
    targets = forward_log_returns(panel.frames, int(cfg0.at("model.horizon_days", 21)))
    rows = []
    for label, ov in VARIANTS:
        print(f"running: {label} …", flush=True)
        rows.append(run(label, ov, panel, uni, targets))
    df = pd.DataFrame(rows)
    for c, f in [("win", "{:.1%}"), ("avg_net", "{:+.2%}"), ("total", "{:+.2%}"),
                 ("maxDD", "{:.2%}"), ("dep_mean", "{:.2%}"), ("dep_max", "{:.2%}"),
                 ("idle_days", "{:.0%}")]:
        df[c] = df[c].map(lambda v, f=f: f.format(v) if v is not None else "—")
    print("\n" + df.to_string(index=False))
    print("\nRead TOTAL, not trades: more trades at a smaller size is not progress.")


if __name__ == "__main__":
    main()
