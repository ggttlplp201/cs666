"""System A runner — wires feed → store → monitor → engine → paper execution.

Modes:
  --demo                synthesize a nerf-event scenario end-to-end (no keys)
  --replay S [--posts P]  step through recorded snapshot/post JSONL files
  --live                requires real keys; refuses while placeholders remain

Live order placement does not exist yet by design: execution.paper_mode is a
go-live gate (§9), and only the paper backend is wired in.

Usage: PYTHONPATH=src python -m system_a.runner --demo
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from shared.bus import SignalBus
from shared.configuration import Config, secret
from shared.execution import PaperBackend
from shared.feed import Cs2shFeed, FeedUnavailable, ReplayFeed, item_to_json
from shared.ledger import DAY, Ledger
from shared.provenance import ProvenanceLog
from shared.store import SnapshotStore
from shared.synthetic import ItemSpec, generate_series
from system_a.engine import ReactiveEngine
from system_a.monitor import (
    FileReplaySource, KeywordClassifier, MonitorAgent, load_allowlist,
)
from system_a.risk import RiskGate
from system_a.rules import RulesTable

REPO_ROOT = Path(__file__).resolve().parents[2]
M4A4 = "M4A4 | Desolate Space (Field-Tested)"
M4A1S = "M4A1-S | Decimator (Field-Tested)"


def load_universe(config: Config) -> list[str]:
    """The tracked item universe — currently the Phase-1 seed list."""
    seed = REPO_ROOT / config.require("data.steam_history")["items_file"]
    return sorted(
        {line.strip() for line in seed.read_text().splitlines() if line.strip()}
    )


def build_stack(config: Config, posts_path: Path | None):
    store = SnapshotStore()
    bus = SignalBus()
    backend = PaperBackend(
        wallet_cny=config.require("capital.total"),
        fee_pct=config.require("costs.buff_fee_pct"),
        fill_volume_cap_k=config.require("position_sizing.volume_relative_k"),
    )
    ledger = Ledger(trade_lock_days=config.require("cooldown.trade_lock_days"))
    gating = config.get("system_a.rules_gating", {}) or {}
    rules = RulesTable.load(
        REPO_ROOT / config.require("system_a.rules_table_path"),
        disabled_rules=gating.get("disabled_rules", []),
        disabled_pairs=gating.get("disabled_pairs", []),
    )
    gate = RiskGate(config, ledger)
    provenance = ProvenanceLog(REPO_ROOT / "var" / "provenance_a.jsonl")
    universe = load_universe(config)
    engine = ReactiveEngine(
        config, store, bus, backend, ledger, rules, gate, provenance,
        universe=universe,
    )
    sources = [FileReplaySource(posts_path)] if posts_path else []
    monitor = MonitorAgent(
        sources=sources,
        classifier=KeywordClassifier(),
        bus=bus,
        allowlist=load_allowlist(REPO_ROOT / "config" / "monitor_allowlist.yaml"),
        known_items=universe + rules.weapons,
        corroboration_min_sources=config.require(
            "system_a.monitor.corroboration_min_sources"
        ),
    )
    return store, backend, ledger, engine, monitor, provenance


def run_replay(config: Config, snapshots_path: Path, posts_path: Path | None) -> int:
    store, backend, ledger, engine, monitor, provenance = build_stack(
        config, posts_path
    )
    feed = ReplayFeed(snapshots_path)
    cycles = 0
    for snapshot in feed:
        store.insert(snapshot)
        now_ts = snapshot[0].ts + 60
        backend.set_market(store.latest())
        monitor.run_cycle(now_ts)
        engine.run_cycle(now_ts)
        cycles += 1
    _summary(config, store, backend, ledger, provenance, cycles)
    return 0


def run_demo(config: Config) -> int:
    """Synthetic §4.1 scenario: an M4A1-S nerf ships on day 21; M4A4 (the
    substitute) breaks out with volume; the engine buys on confirmation,
    rides the repricing, and the TP bracket exits after the T+7 unlock."""
    demo_dir = REPO_ROOT / "var" / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        ItemSpec(
            M4A4, 1000.0, daily_vol=0.004, volume_24h=30,
            events={21: (0.04, 2.0), **{d: (0.025, 1.6) for d in range(22, 28)}},
        ),
        ItemSpec(M4A1S, 800.0, daily_vol=0.004, volume_24h=30),
    ]
    series = generate_series(specs, days=34, seed=11)
    snapshots_path = demo_dir / "snapshots.jsonl"
    snapshots_path.write_text(
        "\n".join(item_to_json(i) for snap in series for i in snap)
    )
    event_ts = series[21][0].ts
    posts = [
        {
            "source": "gabefollower", "platform": "x",
            "text": "Datamined: upcoming CS2 update nerfs the M4A1-S", "ts": event_ts - 3600,
        },
        {
            "source": "CounterStrike", "platform": "x",
            "text": "CS2 release notes: the M4A1-S has been nerfed — update is live",
            "ts": event_ts,
        },
    ]
    posts_path = demo_dir / "posts.jsonl"
    posts_path.write_text("\n".join(json.dumps(p) for p in posts))
    print(f"demo fixtures written to {demo_dir}\n")
    return run_replay(config, snapshots_path, posts_path)


def run_poller(config: Config, max_cycles: int | None = None) -> int:
    """Snapshot poller (Shared §2a.3): poll /v1/prices/latest on the refresh
    cadence and persist every response forever — our own BUFF depth history
    accumulates from day one; data not captured is lost permanently."""
    poller = config.require("data.snapshot_poller")
    if not poller.get("enabled"):
        print("data.snapshot_poller.enabled is false")
        return 1
    tracked = load_universe(config)
    feed = Cs2shFeed(tracked, config.require("fx.usd_cny_rate"))
    store = SnapshotStore(REPO_ROOT / poller["db_path"])
    interval = config.require("data.refresh_seconds")
    print(f"polling {len(tracked)} items every {interval}s → {poller['db_path']}")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            items = feed.fetch()
        except FeedUnavailable as e:
            print(f"feed unavailable: {e}")
            return 1
        store.insert(items, source="buff")
        cycles += 1
        print(
            f"[{cycles}] {len(items)} items @ {items[0].ts if items else '-'}"
            + (f"  api-errors: {len(feed.last_errors)}" if feed.last_errors else "")
        )
        if max_cycles is None or cycles < max_cycles:
            time.sleep(interval)
    return 0


def run_live(config: Config) -> int:
    missing = [k for k in ("CS2SH_API_KEY",) if secret(k) is None]
    if missing:
        print(f"live mode blocked — placeholder keys: {', '.join(missing)}")
        return 1
    if config.require("execution.paper_mode"):
        print(
            "execution.paper_mode is true (the go-live gate, §9). Live loop "
            "would run the paper backend against the real feed — not yet wired. "
            "TODO(Leon): schedule this once keys land."
        )
        return 1
    print("real-money execution backend is intentionally not implemented yet")
    return 1


def _summary(config, store, backend, ledger, provenance, cycles: int) -> None:
    fee = config.require("costs.buff_fee_pct")
    marks = {
        name: item.buff_highest_buy_cny for name, item in store.latest().items()
    }
    print(f"cycles run:        {cycles}")
    print(f"wallet:            {backend.get_wallet():,.2f} CNY")
    open_lots = ledger.open_lots()
    print(f"open lots:         {len(open_lots)}"
          + (f"  ({', '.join(f'{l.qty}x {l.market_hash_name.split(' |')[0]}@{l.buy_price}' for l in open_lots)})" if open_lots else ""))
    print(f"realized P&L:      {ledger.realized_pnl():+,.2f} CNY (net of fees)")
    print(f"marked open P&L:   {ledger.marked_pnl(marks, fee):+,.2f} CNY (net of exit fee)")
    actions = [r["action"] for r in provenance.read_all()]
    from collections import Counter
    print(f"decisions logged:  {dict(Counter(actions))}")
    print(f"provenance log:    {provenance.path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="System A — event-driven agent")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true")
    mode.add_argument("--replay", type=Path, metavar="SNAPSHOTS_JSONL")
    mode.add_argument("--poll", action="store_true",
                      help="snapshot poller: persist /v1/prices/latest forever")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--posts", type=Path, default=None)
    parser.add_argument("--cycles", type=int, default=None,
                        help="--poll only: stop after N polls (default: run forever)")
    args = parser.parse_args(argv)

    config = Config.load(REPO_ROOT, system="system_a")
    if args.demo:
        return run_demo(config)
    if args.replay:
        return run_replay(config, args.replay, args.posts)
    if args.poll:
        return run_poller(config, args.cycles)
    return run_live(config)


if __name__ == "__main__":
    sys.exit(main())
