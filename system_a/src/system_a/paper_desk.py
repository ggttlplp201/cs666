"""Paper trading desk, fired by REAL detected events only.

The difference between this and `trade_up_paper` matters. That module injects
a synthetic Tier-2 signal on a hardcoded date to prove the engine wiring. This
one refuses to invent a signal at all: every trade it makes traces back to an
event that actually happened, from one of exactly two real sources.

  watch     var/alerts.jsonl, written by system_a.watch when it detects a
            trade-up announcement in Valve's own news feed. This is the live
            path: when the watch fires, this desk trades it.
  labelled  trade_up entries in config/rules_table_a.yaml. Real, dated,
            verified Valve updates, detected retrospectively rather than live.

If neither source has an event, the desk holds cash and says so. It will not
manufacture one to have something to show. Everything else is real too: BUFF
prices from the iflow archive, measured per-item spreads, the 1.5% sell fee,
and the T+7 trade lock.

WHAT THIS CAN AND CANNOT TELL YOU. The trade-up class has fired once
(2025-10-22), so a run today is one event, not a track record. It shows what
the machine would have done with a real book on the one real event we have,
which is worth seeing and is not the same as an edge. The controls behind the
edge claim live in `system_a.trade_up_control`; the window behind the timing
lives in `system_a.trade_up_lag`.

Run:  make desk            (or: PYTHONPATH=src python -m system_a.paper_desk)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
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
DAY = 86400.0
DEFAULT_CAPITAL = 500_000.0     # CNY
PRE_DAYS, POST_DAYS = 30, 90    # market window simulated around each event


@dataclass(frozen=True)
class RealEvent:
    """An event the desk is allowed to trade. There is no constructor for a
    made-up one: both factories below read from disk."""
    day: str            # YYYY-MM-DD
    ts: float
    origin: str         # "watch" | "labelled"
    detail: str
    confidence: float


def events_from_watch(repo: Path) -> list[RealEvent]:
    """Live detections written by system_a.watch."""
    path = repo / "var" / "alerts.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            a = json.loads(line)
        except json.JSONDecodeError:
            continue
        if a.get("event_rule") != "trade_up_pool_change":
            continue
        ts = float(a.get("posted_at") or a.get("detected_at") or 0)
        if not ts:
            continue
        out.append(RealEvent(
            day=datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            ts=ts, origin="watch",
            detail=str(a.get("excerpt", ""))[:120],
            confidence=float(a.get("confidence", 0.9))))
    return out


def events_from_labels(rules: RulesTable) -> list[RealEvent]:
    """Verified Valve updates already labelled in the rules table."""
    out = []
    for e in rules.historical_events:
        kind = e.get("type")
        kind = ",".join(kind) if isinstance(kind, list) else str(kind)
        if "trade_up_pool_change" not in kind:
            continue          # the lock-expiry echo is a consequence, not a trigger
        day = str(e.get("date"))
        out.append(RealEvent(day=day, ts=_event_ts(day), origin="labelled",
                             detail=str(e.get("change", ""))[:120], confidence=0.95))
    return out


def collect_events(repo: Path, rules: RulesTable) -> list[RealEvent]:
    """Both real sources, de-duplicated by day, live detection winning."""
    by_day: dict[str, RealEvent] = {}
    for ev in events_from_labels(rules):
        by_day[ev.day] = ev
    for ev in events_from_watch(repo):
        by_day[ev.day] = ev          # a live detection supersedes the label
    return sorted(by_day.values(), key=lambda e: e.ts)


def position_value(ledger: Ledger, marks: dict[str, float], fee_pct: float) -> float:
    """Exit value of open lots, net of the fee we would pay to close them.

    NOT `marked_pnl`, which is unrealized P&L. Book value is cash plus what the
    inventory is worth, so using P&L there subtracts the cost basis without
    adding the position back and invents a drawdown the size of whatever was
    deployed. An unmarked lot is held at cost rather than dropped, since
    dropping it reproduces the same error on a smaller scale."""
    return sum(lot.qty * marks.get(lot.market_hash_name, lot.buy_price) * (1 - fee_pct)
               for lot in ledger.open_lots())


def _floor_day(ts: float) -> float:
    return ts - (ts % DAY)


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def run_desk(capital: float = DEFAULT_CAPITAL, repo: Path = REPO) -> dict:
    config = Config.load(repo, system="system_a")
    # iflow carries no executed volume, so liquidity is judged on book depth.
    # A data-tier posture, recorded rather than hidden.
    config.data["selection_filters"]["allow_unknown_volume"] = True
    config.data["capital"]["total"] = capital

    rules = RulesTable.load(repo / config.require("system_a.rules_table_path"))
    events = collect_events(repo, rules)
    src_store = SnapshotStore(repo / config.require("data.snapshot_poller")["db_path"])
    seed = repo / config.require("data.steam_history")["items_file"]
    universe = sorted({l.strip() for l in seed.read_text().splitlines() if l.strip()})

    result: dict = {
        "capital": capital,
        "events": [e.__dict__ for e in events],
        "fee_pct": config.require("costs.buff_fee_pct"),
        "lock_days": config.require("cooldown.trade_lock_days"),
    }
    if not events:
        result["status"] = "no_events"
        return result

    # Real BUFF prices around every real event.
    by_day: dict[float, list[Item]] = {}
    for ev in events:
        lo, hi = ev.ts - PRE_DAYS * DAY, ev.ts + POST_DAYS * DAY
        for name in universe:
            for item in src_store.series(name, source="buff_iflow"):
                if lo <= item.ts <= hi:
                    by_day.setdefault(_floor_day(item.ts), []).append(item)
    if not by_day:
        result["status"] = "no_market_data"
        return result

    store = SnapshotStore()               # fresh; the engine reads source='buff'
    bus = SignalBus()
    backend = PaperBackend(
        wallet_cny=capital,
        fee_pct=config.require("costs.buff_fee_pct"),
        fill_volume_cap_k=config.require("position_sizing.volume_relative_k"))
    ledger = Ledger(trade_lock_days=config.require("cooldown.trade_lock_days"))
    prov = ProvenanceLog(repo / "var" / "paper_desk.jsonl")
    if prov.path.exists():
        prov.path.unlink()
    engine = ReactiveEngine(config, store, bus, backend, ledger, rules,
                            RiskGate(config, ledger), prov, universe=universe)

    fired: set[str] = set()
    equity: list[dict] = []
    for day in sorted(by_day):
        snapshot = by_day[day]
        store.insert(snapshot, source="buff")
        backend.set_market({i.market_hash_name: i for i in snapshot})
        now_ts = day + 60
        for ev in events:
            # The signal is published because a REAL event exists on this day,
            # never on a schedule and never to fill a quiet stretch.
            if ev.day not in fired and day >= _floor_day(ev.ts):
                bus.publish(Signal(
                    tier=2, type=SignalType.CONFIRMED_UPDATE, items=(),
                    direction=Direction.BULLISH, confidence=ev.confidence,
                    first_seen_ts=now_ts, sources=(ev.origin,),
                    event_rule="trade_up_pool_change"))
                fired.add(ev.day)
        engine.run_cycle(now_ts)

        marks = {n: i.buff_highest_buy_cny for n, i in store.latest().items()}
        fee = config.require("costs.buff_fee_pct")
        cash = backend.get_wallet()
        held = position_value(ledger, marks, fee)
        equity.append({
            "day": _fmt(day), "equity": cash + held, "cash": cash,
            "positions": held, "open_lots": len(ledger.open_lots()),
        })

    marks = {n: i.buff_highest_buy_cny for n, i in store.latest().items()}
    fee = config.require("costs.buff_fee_pct")
    realized, marked = ledger.realized_pnl(), ledger.marked_pnl(marks, fee)
    records = prov.read_all()
    buys = [r for r in records if r["action"] == "buy_placed"]

    result.update({
        "status": "ran",
        "days": len(by_day),
        "start": _fmt(min(by_day)), "end": _fmt(max(by_day)),
        "decisions": dict(Counter(r["action"] for r in records)),
        "positions_opened": len(buys),
        "realized_pnl": realized,
        "marked_pnl": marked,
        "total_pnl": realized + marked,
        "return_pct": (realized + marked) / capital,
        "open_lots": len(ledger.open_lots()),
        "equity": equity,
        "trades": [{"item": r["item"], "day": _fmt(r["ts"]),
                    "price": r["inputs"].get("price"), "rule": r["rule"]}
                   for r in buys],
    })
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL,
                    help="paper book in CNY (default 500000)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    res = run_desk(capital=args.capital)
    out = args.out or REPO / "var" / "paper_desk.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str))

    print(f"== PAPER DESK — real events only, {res['capital']:,.0f} CNY book ==")
    if res["status"] == "no_events":
        print("\n  No real trade-up event has been detected, and none is labelled.")
        print("  The desk holds cash. It will not invent a signal to trade.")
        print("  Start the watch with `make watch` so live events reach it.")
        return 0
    if res["status"] == "no_market_data":
        print("\n  Events exist but no BUFF market data covers their windows.")
        print("  Load history with `python -m shared.iflow_history`.")
        return 0

    print(f"   events traded:    {len(res['events'])}")
    for e in res["events"]:
        print(f"     {e['day']}  via {e['origin']:9s} {e['detail'][:60]}")
    print(f"   simulated:        {res['days']} days, {res['start']} to {res['end']}")
    print(f"   decisions:        {res['decisions']}")
    print(f"   positions opened: {res['positions_opened']}")
    print(f"   realized P&L:     {res['realized_pnl']:+,.0f} CNY")
    print(f"   marked open P&L:  {res['marked_pnl']:+,.0f} CNY (net of exit fee)")
    print(f"   TOTAL:            {res['total_pnl']:+,.0f} CNY "
          f"({res['return_pct']:+.1%} on capital)")
    print(f"\n   artifact -> {out}")
    print("   Paper only, $0 real. One event is not a track record: this shows"
          "\n   what the machine did with a real book on the one real event we have.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
