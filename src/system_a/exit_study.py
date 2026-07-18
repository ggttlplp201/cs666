"""Exit-policy study: does POSITION MANAGEMENT change the viability verdict?

Same out-of-sample trades and BUFF frictions as the spread study; the entry
leg is fixed (reactive, cross to the ask). Exit policies compared:
  (a) fixed T+7        — the previous baseline
  (b) fixed 14/30/60d
  (c) TA-managed       — position_manager rules (Shared §3) from unlock,
                         thesis-invalidation N ∈ {7,14,30}, 60d horizon cap
All policies use the PATIENT ask-listing exit (System A's exit is not
urgent): list at mid×(1+s/2); filled on the first day whose median trades
at/above the listing; unfilled after grace_days ⇒ cross down to the bid.

Magnitude filter (PRE-REGISTERED, ex-ante): act only when the patch text
itself states a percentage change of ≥20% for the weapon (parsed from the
event's `change` text — never from realized returns). Events stating only
absolute rounds or no number are excluded from the filtered subset.

Trade-up class (2025-10-22 + 2025-10-30 echo): the collection→gold map is
still Leon's todo, so the item lists here come verbatim from the event's own
`observed` text in the rules table (not invented): reds MP9 Starlight
Protector / MP7 Bloodsport; knives (echo, bearish, direction-only)
Karambit Doppler / Butterfly Fade / Navaja Crimson Web.

Nothing is tuned; per-rule attribution reports which rhymes carried edge.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shared.configuration import Config
from shared.schema import Item
from shared.store import SnapshotStore
from system_a.event_study import (
    DAY, _bar_after, _bar_at_or_before, _event_ts, fee_for_source,
    run_event_study, weapon_directions_from_text,
)
from system_a.position_manager import assess
from system_a.rules import RulesTable
from system_a.spread_study import spread_stats

MAGNITUDE_THRESHOLD = 0.20   # pre-registered: stated patch change ≥ 20%
FIXED_HORIZONS = (7, 14, 30, 60)
THESIS_NS = (7, 14, 30)
HORIZON_CAP_DAYS = 60

# From the rules table's own `observed` field for 2025-10-22 (not invented):
TRADE_UP_REDS = [
    "MP9 | Starlight Protector (Field-Tested)",
    "MP7 | Bloodsport (Factory New)",
]
TRADE_UP_KNIVES = [   # echo leg — bearish, long-only ⇒ direction check only
    "Karambit | Doppler (Factory New)",
    "Butterfly Knife | Fade (Factory New)",
    "Navaja Knife | Crimson Web (Factory New)",
]


def parse_stated_magnitudes(rules: RulesTable, change_text: str) -> dict[str, float]:
    """Ex-ante magnitude per weapon: a ±N% or ±N-M% stated within 40 chars
    after the weapon's mention. Absolute rounds ('-60 rounds') do not parse
    to a magnitude and exclude the weapon from the filtered subset."""
    magnitudes: dict[str, float] = {}
    for weapon in rules.weapons:
        m = re.search(rf"(?<![\w-]){re.escape(weapon)}(?![\w-])", change_text, re.I)
        if not m:
            continue
        window = change_text[m.end():m.end() + 40]
        pct = re.search(r"([+-]?\d+)(?:-(\d+))?%", window)
        if pct:
            lo = abs(float(pct.group(1)))
            hi = float(pct.group(2)) if pct.group(2) else lo
            magnitudes[weapon] = (lo + hi) / 2 / 100.0
    return magnitudes


def patient_sell(
    series: list[Item], decision_ts: float, spread: float, fee: float,
    grace_days: float,
) -> tuple[float, float, str] | None:
    """(exit_ts, net proceeds per unit / pre-entry-price, fill_kind).
    Lists at the ask; crosses to the bid after grace."""
    decision_bar = _bar_after(series, decision_ts)
    if decision_bar is None:
        return None
    listing = decision_bar[1] * (1 + spread / 2)
    for bar in series:
        if decision_bar[0] <= bar.ts <= decision_ts + grace_days * DAY:
            if bar.buff_lowest_sell_cny >= listing:
                return (bar.ts, listing * (1 - fee), "ask_filled")
    cross_bar = _bar_after(series, decision_ts + grace_days * DAY)
    if cross_bar is None:
        return None
    return (cross_bar[0], cross_bar[1] * (1 - spread / 2) * (1 - fee), "crossed_bid")


@dataclass
class ManagedTrade:
    item: str
    event_date: str
    entry_price: float
    nets: dict = field(default_factory=dict)        # policy -> net
    exit_rules: dict = field(default_factory=dict)  # policy -> attributed rule


def run_policies(
    store: SnapshotStore,
    trades: list[tuple[str, str, float, float]],   # (item, date, entry_ts, entry_price)
    spreads: dict[str, float],
    config: Config,
    source: str = "steam",
) -> list[ManagedTrade]:
    pm = config.require("system_a.position_management")
    indicators_cfg = config.require("indicators")
    brackets = config.require("brackets")
    grace = pm["patient_exit"]["grace_days"]
    lock_days = config.require("cooldown.trade_lock_days")
    fee_now = config.require("costs.buff_fee_pct")
    fee_hist = config.get("costs.fee_history", [])
    results = []
    for item, date, entry_ts, entry_price in trades:
        spread = spreads.get(item)
        series = store.series(item, source=source)
        if spread is None or not series:
            continue
        trade = ManagedTrade(item, date, entry_price)
        fee = fee_for_source("buff", entry_ts, fee_now, fee_hist, 0.0)

        for horizon in FIXED_HORIZONS:
            sold = patient_sell(
                series, entry_ts + horizon * DAY, spread, fee, grace
            )
            if sold:
                trade.nets[f"fixed_{horizon}d"] = sold[1] / entry_price - 1
                trade.exit_rules[f"fixed_{horizon}d"] = sold[2]

        for thesis_n in THESIS_NS:
            policy = f"managed_N{thesis_n}"
            pm_variant = {**pm, "thesis_invalidation_days": thesis_n}
            unlock_ts = entry_ts + lock_days * DAY
            decision_ts, rule = None, "horizon_cap"
            day_bars = [b for b in series if unlock_ts <= b.ts
                        <= entry_ts + HORIZON_CAP_DAYS * DAY]
            for bar in day_bars:
                window = [b for b in series if b.ts <= bar.ts]
                result = assess(
                    window, entry_price,
                    held_days=(bar.ts - entry_ts) / DAY,
                    pm=pm_variant, indicators_cfg=indicators_cfg,
                    brackets=brackets,
                )
                if result.action == "exit":
                    decision_ts, rule = bar.ts, result.rule
                    break
            if decision_ts is None:
                decision_ts = entry_ts + HORIZON_CAP_DAYS * DAY
            sold = patient_sell(series, decision_ts, spread, fee, grace)
            if sold:
                trade.nets[policy] = sold[1] / entry_price - 1
                trade.exit_rules[policy] = rule
        results.append(trade)
    return results


def _policy_table(results: list[ManagedTrade], policies: list[str]) -> None:
    print(f"{'policy':16} {'n':>3} {'mean':>8} {'median':>8} {'worst':>8} {'best':>8} {'pos':>7}")
    for policy in policies:
        nets = [t.nets[policy] for t in results if policy in t.nets]
        if not nets:
            continue
        pos = sum(1 for n in nets if n > 0)
        print(f"{policy:16} {len(nets):>3} {statistics.mean(nets):>+8.1%} "
              f"{statistics.median(nets):>+8.1%} {min(nets):>+8.1%} "
              f"{max(nets):>+8.1%} {pos:>4}/{len(nets)}")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config.load(repo_root, system="system_a")
    parser = argparse.ArgumentParser(description="Exit-policy study")
    parser.add_argument("--db", type=Path,
                        default=repo_root / config.require("data.snapshot_poller")["db_path"])
    args = parser.parse_args(argv)
    store = SnapshotStore(args.db)

    stats = spread_stats(store)
    spreads = {s.item: s.median for s in stats}
    median_spread = statistics.median(s.median for s in stats)
    rules = RulesTable.load(repo_root / config.require("system_a.rules_table_path"))
    seed = repo_root / config.require("data.steam_history")["items_file"]
    universe = sorted(
        {l.strip() for l in seed.read_text().splitlines() if l.strip()}
    )
    outcomes, _, _ = run_event_study(
        rules, store, universe,
        lock_days=config.require("cooldown.trade_lock_days"),
        buff_fee_pct=config.require("costs.buff_fee_pct"),
        buff_fee_history=config.get("costs.fee_history", []),
        steam_fee_pct=config.require("costs.steam_fee_pct"),
    )
    trades, magnitudes_by_trade = [], {}
    events_by_date = {str(e["date"]): e for e in rules.historical_events}
    for o in outcomes:
        if o.gross_pct is None or o.sample_class != "out_of_sample":
            continue
        item = o.candidate.market_hash_name
        spread = spreads.get(item, median_spread)
        entry = o.entry * (1 + spread / 2)   # reactive: cross to the ask
        key = (item, o.event_date)
        if key in magnitudes_by_trade:
            continue   # same physical trade attributed to two rules — dedupe
        trades.append((item, o.event_date, o.entry_ts, entry))
        event = events_by_date.get(o.event_date, {})
        stated = parse_stated_magnitudes(rules, str(event.get("change", "")))
        weapon = item.split(" |")[0]
        magnitudes_by_trade[key] = stated.get(weapon)

    results = run_policies(store, trades, spreads, config)
    policies = ([f"fixed_{h}d" for h in FIXED_HORIZONS]
                + [f"managed_N{n}" for n in THESIS_NS])

    print("== EXIT POLICIES — ALL OOS TRADES (entry crossed to ask; patient exits) ==")
    _policy_table(results, policies)

    filtered = [
        t for t in results
        if (magnitudes_by_trade.get((t.item, t.event_date)) or 0) >= MAGNITUDE_THRESHOLD
    ]
    print(f"\n== MAGNITUDE-FILTERED SUBSET (stated patch change ≥ "
          f"{MAGNITUDE_THRESHOLD:.0%}; pre-registered, ex-ante) ==")
    if filtered:
        for t in filtered:
            print(f"  {t.event_date}  {t.item}  "
                  f"(stated {magnitudes_by_trade[(t.item, t.event_date)]:.0%})")
        _policy_table(filtered, policies)
    else:
        print("no trades qualified")

    print("\n== PER-RULE EXIT ATTRIBUTION (managed_N14 vs fixed_7d baseline) ==")
    by_rule: dict[str, list[tuple[float, float]]] = {}
    for t in results:
        if "managed_N14" in t.nets and "fixed_7d" in t.nets:
            by_rule.setdefault(t.exit_rules["managed_N14"], []).append(
                (t.nets["managed_N14"], t.nets["fixed_7d"])
            )
    for rule, pairs in sorted(by_rule.items()):
        deltas = [m - b for m, b in pairs]
        print(f"{rule:26} n={len(pairs):>2}  managed {statistics.mean(m for m, _ in pairs):+.1%}  "
              f"baseline {statistics.mean(b for _, b in pairs):+.1%}  "
              f"delta {statistics.mean(deltas):+.1%}")

    print("\n== TRADE-UP CLASS 2025-10-22 (+ 2025-10-30 echo) — items from the "
          "table's observed text ==")
    event_ts = _event_ts("2025-10-22")
    tu_trades = []
    for item in TRADE_UP_REDS:
        series = store.series(item, source="steam")
        if not series:
            print(f"  NO DATA: {item} — add to config/steam_backtest_items.txt")
            continue
        bar = _bar_after(series, event_ts, max_delay_days=3.0)
        if bar is None:
            print(f"  no bar near event: {item}")
            continue
        spread = spreads.get(item, median_spread)
        tu_trades.append((item, "2025-10-22", bar[0], bar[1] * (1 + spread / 2)))
    if tu_trades:
        tu_results = run_policies(store, tu_trades, spreads, config)
        _policy_table(tu_results, policies)
        for t in tu_results:
            best = max(t.nets, key=lambda p: t.nets[p])
            print(f"  {t.item}: best policy {best} {t.nets[best]:+.1%} "
                  f"(exit rule: {t.exit_rules.get(best, '-')})")
    echo_ts = _event_ts("2025-10-30")
    for item in TRADE_UP_KNIVES:
        series = store.series(item, source="steam")
        if not series:
            print(f"  ECHO NO DATA: {item} — add to config/steam_backtest_items.txt")
            continue
        before = _bar_at_or_before(series, echo_ts)
        after = _bar_after(series, echo_ts + 7 * DAY)
        if before and after:
            move = after[1] / before[1] - 1
            print(f"  echo direction check {item}: {move:+.1%} over echo+7d "
                  f"(predicted bearish → {'HIT' if move < 0 else 'miss'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
