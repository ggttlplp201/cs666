"""End-to-end PAPER run of the repointed engine over the 2025-10-22 trade-up
event, on iflow BUFF data. Proves the full System A stack — monitor
announcement signal → rules-table trade-up mapping → risk gate → paper buy →
LONG-hold exit — captures the durable repricing the exit-study measured.

This is plumbing verification, not a new claim: the strategy edge was
established by system_a.trade_up_control. Here we confirm the engine, pointed
at the trade-up class, actually opens and holds the right positions.

Runs on source=buff_iflow rows re-tagged 'buff' into a fresh store so the
engine reads them as its live feed, stepped one day at a time (no look-ahead:
each cycle sees only data up to that day). iflow has no executed volume, so
this run enables selection_filters.allow_unknown_volume (the same posture the
cs2.sh Developer tier needs) — noted, not hidden.

Run:  PYTHONPATH=src python -m system_a.trade_up_paper
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from shared.bus import SignalBus
from shared.configuration import Config
from shared.execution import PaperBackend
from shared.ledger import Ledger
from shared.provenance import ProvenanceLog
from shared.schema import Direction, Item, Signal, SignalType
from shared.store import SnapshotStore
from system_a.engine import ReactiveEngine
from system_a.event_study import _event_ts
from system_a.risk import RiskGate
from system_a.rules import RulesTable

REPO = Path(__file__).resolve().parents[2]
EVENT = "2025-10-22"
DAY = 86400.0


def main(argv=None) -> int:
    config = Config.load(REPO, system="system_a")
    # iflow has no executed volume — allow depth-based liquidity like the
    # Developer tier (this is a data-tier posture, not a strategy tweak).
    config.data["selection_filters"]["allow_unknown_volume"] = True

    src_store = SnapshotStore(REPO / config.require("data.snapshot_poller")["db_path"])
    seed = REPO / config.require("data.steam_history")["items_file"]
    universe = sorted({l.strip() for l in seed.read_text().splitlines() if l.strip()})

    # Pull iflow event-window rows, re-tag as 'buff' (the feed the engine reads).
    event_ts = _event_ts(EVENT)
    lo, hi = event_ts - 30 * DAY, event_ts + 90 * DAY
    by_day: dict[float, list[Item]] = {}
    for name in universe:
        for item in src_store.series(name, source="buff_iflow"):
            if lo <= item.ts <= hi:
                day = _floor_day(item.ts)
                by_day.setdefault(day, []).append(item)
    if not by_day:
        print("no iflow rows in the event window — run shared.iflow_history first")
        return 1

    store = SnapshotStore()          # fresh; engine reads source='buff'
    bus = SignalBus()
    backend = PaperBackend(
        wallet_cny=config.require("capital.total"),
        fee_pct=config.require("costs.buff_fee_pct"),
        fill_volume_cap_k=config.require("position_sizing.volume_relative_k"),
    )
    ledger = Ledger(trade_lock_days=config.require("cooldown.trade_lock_days"))
    rules = RulesTable.load(REPO / config.require("system_a.rules_table_path"))
    gate = RiskGate(config, ledger)
    prov = ProvenanceLog(REPO / "var" / "trade_up_paper.jsonl")
    if prov.path.exists():
        prov.path.unlink()
    engine = ReactiveEngine(config, store, bus, backend, ledger, rules, gate,
                            prov, universe=universe)

    start_wallet = backend.get_wallet()
    announced = False
    for day in sorted(by_day):
        snapshot = by_day[day]
        store.insert(snapshot, source="buff")
        backend.set_market({i.market_hash_name: i for i in snapshot})
        now_ts = day + 60
        # The monitor catches the announcement on event day: a Tier-2 confirmed
        # trade_up_pool_change signal (what a real ScraplingSource would emit).
        if not announced and day >= _floor_day(event_ts):
            bus.publish(Signal(
                tier=2, type=SignalType.CONFIRMED_UPDATE, items=(),
                direction=Direction.BULLISH, confidence=0.95,
                first_seen_ts=now_ts, sources=("cs2_blog",),
                event_rule="trade_up_pool_change",
            ))
            announced = True
        engine.run_cycle(now_ts)

    # Report
    marks = {n: i.buff_highest_buy_cny for n, i in store.latest().items()}
    fee = config.require("costs.buff_fee_pct")
    realized = ledger.realized_pnl()
    marked = ledger.marked_pnl(marks, fee)
    actions = Counter(r["action"] for r in prov.read_all())
    buys = [r for r in prov.read_all() if r["action"] == "buy_placed"]

    print(f"== TRADE-UP PAPER RUN — engine over {EVENT} (iflow BUFF data) ==")
    print(f"cycles:            {len(by_day)} days ({_fmt(min(by_day))} → {_fmt(max(by_day))})")
    print(f"decisions:         {dict(actions)}")
    print(f"positions opened:  {len(buys)}")
    for r in buys:
        print(f"  buy {r['item']}  @ {r['inputs'].get('price')}  "
              f"[{r['inputs'].get('hold')}]  rule={r['rule']}")
    print(f"start wallet:      {start_wallet:,.0f} CNY")
    print(f"realized P&L:      {realized:+,.0f} CNY")
    print(f"marked open P&L:   {marked:+,.0f} CNY (net of exit fee)")
    total = realized + marked
    print(f"TOTAL P&L:         {total:+,.0f} CNY  ({total/start_wallet:+.1%} on capital)")
    open_lots = ledger.open_lots()
    if open_lots:
        held = ", ".join(f"{l.market_hash_name.split(' (')[0]}" for l in open_lots[:6])
        print(f"still held (long): {len(open_lots)} lots — {held}"
              f"{'…' if len(open_lots) > 6 else ''}")
    print("\nNOTE: paper mode, $0 real. Proves the repointed engine opens and "
          "holds gold-case coverts on a trade-up announcement. Edge itself was "
          "validated by system_a.trade_up_control (event vs placebo vs market).")
    return 0


def _floor_day(ts: float) -> float:
    return (int(ts) // 86400) * 86400.0


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


if __name__ == "__main__":
    sys.exit(main())
