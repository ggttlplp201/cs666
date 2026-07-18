"""Walk-forward backtest CLI for System B (System B §9).

    python -m system_b.run_backtest --synthetic            # simulator run
    python -m system_b.run_backtest --data-dir data/panel  # real CSV history

Models 2.5% fee, T+7 lock, thin-book fills, slippage; strategy decisions are
structurally no-lookahead; ranker refits walk-forward with embargo. Outputs
equity curve, summary, per-rule attribution, rank-IC, feature importances,
and the full decision journal under runs/<stamp>/.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from shared.backtest import run_backtest
from shared.config import REPO_ROOT, load_config
from shared.data import MarketPanel
from shared.journal import Journal
from shared.synthetic import generate

from .model import forward_log_returns
from .strategy import PositionalStrategy


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="run on the simulator")
    ap.add_argument("--data-dir", type=str, default=None, help="CSV panel directory")
    ap.add_argument("--days", type=int, default=720)
    ap.add_argument("--items", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--model", type=str, default=None, help="override model.type")
    args = ap.parse_args(argv)

    cfg = load_config("b")
    if args.model:
        cfg.setdefault("model", {})["type"] = args.model

    if args.synthetic or not args.data_dir:
        market = generate(n_items=args.items, n_days=args.days, seed=args.seed)
        panel = market.panel
        source = f"synthetic(seed={args.seed})"
    else:
        panel = MarketPanel.load(Path(args.data_dir))
        source = args.data_dir

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else REPO_ROOT / "runs" / f"backtest_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    journal = Journal(out_dir / "journal.jsonl")
    strategy = PositionalStrategy(cfg=dict(cfg))
    horizon = int(cfg.at("model.horizon_days", 21))
    strategy.set_targets(forward_log_returns(panel.frames, horizon))

    result = run_backtest(
        panel=panel,
        strategy=strategy,
        starting_cash=float(cfg.at("capital.total", 100_000)),
        fee_pct=float(cfg.at("costs.buff_fee_pct", 0.015)),
        slippage_pct=float(cfg.at("execution.slippage_pct", 0.005)),
        fill_fraction=float(cfg.at("execution.fill_fraction", 0.25)),
        trade_lock_days=int(cfg.at("cooldown.trade_lock_days", 7)),
        settlement_days=int(cfg.at("cooldown.settlement_days", 7)),
        journal=journal,
        thesis_lookup=strategy.thesis_for,
    )

    summary = result.summary()
    summary["source"] = source
    summary["model_type"] = strategy.ranker.model_type
    summary["n_refits"] = len(strategy.ranker.refits)

    # rank IC of the walk-forward predictions
    targets = forward_log_returns(panel.frames, horizon)
    ic = strategy.ranker.rank_ic(targets, strategy.predictions)
    summary["rank_ic_mean"] = float(ic.mean()) if len(ic) else None
    summary["rank_ic_days"] = int(len(ic))

    # go-live gate check (System B §9): net-of-fee edge, not just portfolio
    # return — a thinly-deployed paper book can carry real edge.
    gate = cfg.at("go_live_gate", {}) or {}
    min_trade_ret = float(gate.get("min_avg_trade_return_net", 0.03))
    min_trades = int(gate.get("min_closed_trades", 10))
    min_ic = float(gate.get("min_rank_ic", 0.0))
    max_dd = float(gate.get("max_drawdown", -0.20))
    atr = summary.get("avg_trade_return_net")
    ic_mean = summary.get("rank_ic_mean")
    # an untrained model (ic None) FAILS the gate — "never trained" is not a pass
    summary["go_live_gate_pass"] = bool(
        atr is not None and atr >= min_trade_ret
        and summary.get("n_trades_closed", 0) >= min_trades
        and ic_mean is not None and ic_mean >= min_ic
        and summary.get("max_drawdown", -1) >= max_dd
    )

    result.equity_curve.to_csv(out_dir / "equity.csv", header=["equity"])
    result.attribution().to_csv(out_dir / "attribution.csv", index=False)
    if len(ic):
        ic.to_csv(out_dir / "rank_ic.csv", header=["spearman_ic"])
    if strategy.ranker.refits:
        imp = pd.DataFrame([r.importances for r in strategy.ranker.refits],
                           index=[r.day for r in strategy.ranker.refits])
        imp.to_csv(out_dir / "feature_importances.csv")
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    print(json.dumps(summary, indent=2, default=str))
    print(f"\nartifacts -> {out_dir}")

    attr = result.attribution()
    if not attr.empty:
        by_rule = attr.groupby("exit_reason")["pnl"].agg(["count", "sum", "mean"])
        print("\nP&L by exit reason:")
        print(by_rule.to_string())
    return summary


if __name__ == "__main__":
    main()
