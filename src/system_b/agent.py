"""Live paper-trading daily runner (System B §9 forward paper phase).

One invocation = one scheduled decision cycle ("check once a day, close the
app" — Shared §8). State (ledger, risk, pending orders) persists to
runs/paper_state.json between invocations; the decision journal appends to
runs/paper_journal.jsonl.

    python -m system_b.agent --data-dir data/panel [--date 2026-07-17]

Pending orders from the previous cycle are settled against today's data first
(same decide-at-t / fill-at-t+1 discipline as the backtest), then a new cycle
runs. Real-money execution stays behind the same interface and is gated by
config execution.paper_mode + the go-live gate — flipping it is a human call.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from shared_b.backtest import exit_side_prices
from shared_b.config import REPO_ROOT, load_config
from shared_b.data import MarketPanel
from shared_b.execution import PaperBroker
from shared_b.journal import Journal
from shared_b.ledger import Ledger
from shared_b.schema import Order, Side
from shared_b.signal_bus import JsonlBus, NullBus

from .strategy import PositionalStrategy


def _load_state(path: Path, starting_cash: float, lock_days: int, settle_days: int) -> tuple[Ledger, dict]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return Ledger.from_dict(d["ledger"]), d
    return Ledger(starting_cash=starting_cash, trade_lock_days=lock_days,
                  settlement_days=settle_days), {}


def run_cycle(data_dir: Path, on_day: date | None = None, state_path: Path | None = None) -> dict:
    cfg = load_config("b")
    if not bool(cfg.at("execution.paper_mode", True)):
        raise SystemExit("live execution path not implemented — paper_mode only "
                         "(go-live requires the §9 gate + human sign-off)")

    panel = MarketPanel.load(data_dir)
    cal = panel.calendar()
    if cal.empty:
        raise SystemExit(f"no data in {data_dir}")
    on_day = on_day or cal[-1].date()

    state_path = state_path or REPO_ROOT / "runs" / "paper_state.json"
    journal = Journal(REPO_ROOT / "runs" / "paper_journal.jsonl")
    ledger, state = _load_state(
        state_path,
        float(cfg.at("capital.total", 100_000)),
        int(cfg.at("cooldown.trade_lock_days", 7)),
        int(cfg.at("cooldown.settlement_days", 7)),
    )
    pending = state.get("pending_orders", [])

    # idempotency: one decision cycle per day, FORWARD only. Re-running the
    # same day (or a backward --date) would re-settle pending orders at
    # pre-decision prices and double-run cycles.
    asof = state.get("asof")
    if asof is not None and on_day.isoformat() <= str(asof):
        out = {"day": on_day.isoformat(), "status": "already_ran", "state_asof": asof}
        print(json.dumps(out))
        return out

    broker = PaperBroker(
        panel=panel,
        fee_pct=float(cfg.at("costs.buff_fee_pct", 0.015)),
        slippage_pct=float(cfg.at("execution.slippage_pct", 0.005)),
        fill_fraction=float(cfg.at("execution.fill_fraction", 0.25)),
    )

    bus_path = REPO_ROOT / "runs" / "signal_bus.jsonl"
    bus = JsonlBus(bus_path) if bus_path.exists() else NullBus()
    strategy = PositionalStrategy(cfg=dict(cfg), bus=bus)
    # restore cross-invocation strategy/risk state — without it, stop
    # cooldowns, turnover caps, loss-limit halts and order cooldowns never bind
    from .risk import RiskState

    if "risk_state" in state:
        strategy.risk.state = RiskState.from_dict(state["risk_state"])
    strategy.last_order_day = {
        k: date.fromisoformat(v) for k, v in state.get("last_order_day", {}).items()
    }
    strategy.theses = {k: tuple(v) for k, v in state.get("theses", {}).items()}

    # walk-forward training in paper mode too — otherwise the paper phase
    # validates a composite-only strategy while the backtest validated the
    # blended ranker. Targets are calendar-shifted and embargoed, so only
    # fully-realized windows ever train.
    from .model import forward_log_returns

    horizon = int(cfg.at("model.horizon_days", 21))
    strategy.set_targets(forward_log_returns(panel.frames, horizon))

    # 0) mature receivables, then settle last cycle's orders at today's prices
    ledger.settle_cash(on_day)
    for od in pending:
        o = Order(item=od["item"], side=Side(od["side"]), qty=od["qty"],
                  limit_price=od["limit_price"], day=date.fromisoformat(od["day"]),
                  client_order_id=od["client_order_id"], reason=od.get("reason", ""),
                  lot_id=od.get("lot_id"), batch_index=od.get("batch_index", 0))
        (broker.place_buy if o.side == Side.BUY else broker.place_sell)(o)
    for f in broker.settle(on_day):
        lot = ledger.apply_fill(f, *strategy.thesis_for(f.order))
        journal.trade(lot, day=on_day, note=f"paper fill {f.order.side.value} {f.qty} @ {f.fill_price:.2f}")

    # 1) run today's decision cycle; new orders wait for the NEXT cycle's data
    view = panel.up_to(on_day)
    orders = strategy.on_cycle(view, ledger, journal)

    # 2) persist state
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "asof": on_day.isoformat(),
                "ledger": ledger.to_dict(),
                "risk_state": strategy.risk.state.to_dict(),
                "last_order_day": {k: v.isoformat() for k, v in strategy.last_order_day.items()},
                "theses": {k: list(v) for k, v in strategy.theses.items()},
                "pending_orders": [
                    {
                        "item": o.item, "side": o.side.value, "qty": o.qty,
                        "limit_price": o.limit_price, "day": o.day.isoformat(),
                        "client_order_id": o.client_order_id, "reason": o.reason,
                        "lot_id": o.lot_id, "batch_index": o.batch_index,
                    }
                    for o in orders
                ],
            },
            f, indent=2,
        )

    marks = exit_side_prices(view)
    fee = float(cfg.at("costs.buff_fee_pct", 0.015))
    summary = {
        "day": on_day.isoformat(),
        "equity": ledger.equity(marks, fee),
        "cash": ledger.cash,
        "receivables": ledger.receivables(),
        "open_lots": len(ledger.open_lots()),
        "orders_placed": len(orders),
        "regime": strategy.last_regime.regime.value if strategy.last_regime else "?",
    }
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=str, default=str(REPO_ROOT / "data" / "panel"))
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: last data day)")
    args = ap.parse_args(argv)
    run_cycle(
        Path(args.data_dir),
        date.fromisoformat(args.date) if args.date else None,
    )


if __name__ == "__main__":
    main()
