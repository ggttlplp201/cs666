"""Anticipatory-entry study: can System A EARN the spread instead of paying it?

Compares three entry modes on the same out-of-sample trades:
  (a) reactive      — market-buy crossing to the ask on announcement day
  (b) anticipatory limit  — passive bid-side limit placed L days before the
      event; fills only if price actually trades down to it (a never-filled
      order is a MISSED trade and is counted); earns the spread
  (c) anticipatory market — crossing to the ask L days early (pays spread,
      but pre-serves the T+7 lock)

Fill model for (b), stated plainly: with daily medians as the only history,
a limit at price P is deemed filled on the first pre-event day whose median
≤ P (the typical trade that day was at/below our limit). Size is assumed ≤
the measured median bid depth. No intraday lows exist in this corpus, so
fills at levels between the daily median and low are missed → the model
UNDER-counts fills slightly; it never over-counts.

Lock interaction — the architectural point under test: an entry filled ≥7
days pre-announcement has already served its T+7 when the news lands, so
the exit can sell INTO the announcement (exit = first bar at/after
max(unlock, event)); a reactive entry must hold through 7 days of
post-event decay.

False-leak cost: mode (b) mechanics on random non-event dates (nothing
ships, exit at unlock). Leak hit-rate: our allowlist records 10 verified
Tier-1 hits and — survivorship in our own notes — zero recorded misses, so
there is NO defensible point estimate; expected value is reported across
p ∈ {0.9, 0.7, 0.5}. Nothing here is tuned; config stays locked.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from shared.configuration import Config
from shared.schema import Item
from shared.store import SnapshotStore
from system_a.event_study import (
    DAY, _bar_after, _bar_at_or_before, _event_ts, fee_for_source,
    run_event_study,
)
from system_a.rules import RulesTable
from system_a.spread_study import cross_spread_net, spread_stats

LEAD_DAYS = (3, 7, 14, 30)
HIT_RATES = (0.9, 0.7, 0.5)


@dataclass
class ModeResult:
    item: str
    event_date: str
    reactive: float | None = None            # (a)
    limit_by_lead: dict = None               # L -> net | "missed"
    market_by_lead: dict = None              # L -> net | None


def _limit_fill(
    series: list[Item], limit_price: float, start_ts: float, end_ts: float
) -> float | None:
    """First pre-event day whose median trades at/below the limit."""
    for bar in series:
        if start_ts < bar.ts < end_ts and bar.buff_lowest_sell_cny <= limit_price:
            return bar.ts
    return None


def _exit_net(
    series: list[Item], entry_price: float, fill_ts: float, event_ts: float,
    spread: float, fee: float, lock_days: float,
) -> float | None:
    """Sell into the announcement if the lock is already served, else at
    unlock — always crossing down to the bid."""
    exit_target = max(fill_ts + lock_days * DAY, event_ts)
    exit_bar = _bar_after(series, exit_target)
    if exit_bar is None:
        return None
    exit_bid = exit_bar[1] * (1 - spread / 2)
    return exit_bid * (1 - fee) / entry_price - 1


def run_modes(
    store: SnapshotStore,
    outcomes,
    spreads: dict[str, float],
    lock_days: float,
    buff_fee_pct: float,
    buff_fee_history: list[dict],
    source: str = "steam",
) -> list[ModeResult]:
    results = []
    for outcome in outcomes:
        if outcome.gross_pct is None or outcome.sample_class != "out_of_sample":
            continue
        item = outcome.candidate.market_hash_name
        spread = spreads.get(item)
        if spread is None:
            continue
        series = store.series(item, source=source)
        event_ts = _event_ts(outcome.event_date)
        fee = fee_for_source("buff", outcome.exit_ts, buff_fee_pct,
                             buff_fee_history, 0.0)
        result = ModeResult(item, outcome.event_date,
                            limit_by_lead={}, market_by_lead={})
        result.reactive = cross_spread_net(outcome.gross_pct, spread, fee)
        for lead in LEAD_DAYS:
            t0 = event_ts - lead * DAY
            leak_bar = _bar_at_or_before(series, t0)
            if leak_bar is None:
                result.limit_by_lead[lead] = None
                result.market_by_lead[lead] = None
                continue
            leak_mid = leak_bar[1]
            # (b) passive limit at the bid
            limit_price = leak_mid * (1 - spread / 2)
            fill_ts = _limit_fill(series, limit_price, t0, event_ts)
            if fill_ts is None:
                result.limit_by_lead[lead] = "missed"
            else:
                result.limit_by_lead[lead] = _exit_net(
                    series, limit_price, fill_ts, event_ts, spread, fee, lock_days
                )
            # (c) market-buy at the ask, L days early
            entry_bar = _bar_after(series, t0, max_delay_days=2.0)
            if entry_bar is None:
                result.market_by_lead[lead] = None
            else:
                result.market_by_lead[lead] = _exit_net(
                    series, entry_bar[1] * (1 + spread / 2), entry_bar[0],
                    event_ts, spread, fee, lock_days,
                )
        results.append(result)
    return results


def false_leak_costs(
    store: SnapshotStore,
    portfolio: list[str],
    spreads: dict[str, float],
    lock_days: float,
    fee: float,
    lead: float = 7,
    n_dates: int = 25,
    seed: int = 7,
    source: str = "steam",
) -> tuple[list[float], int]:
    """Mode-(b) mechanics on random dates where nothing ships: place the
    limit, exit at unlock if filled. Returns (nets, missed_count)."""
    rng = random.Random(seed)
    nets, missed = [], 0
    series_by = {n: store.series(n, source=source) for n in portfolio}
    usable = [n for n in portfolio if series_by[n]]
    lo = max(series_by[n][0].ts for n in usable)
    hi = min(series_by[n][-1].ts for n in usable) - (lead + lock_days + 5) * DAY
    for _ in range(n_dates):
        ts = rng.uniform(lo, hi)
        for name in usable:
            spread = spreads.get(name)
            if spread is None:
                continue
            series = series_by[name]
            leak_bar = _bar_at_or_before(series, ts)
            if leak_bar is None:
                continue
            limit_price = leak_bar[1] * (1 - spread / 2)
            fill_ts = _limit_fill(series, limit_price, ts, ts + lead * DAY)
            if fill_ts is None:
                missed += 1
                continue
            net = _exit_net(series, limit_price, fill_ts, fill_ts, spread,
                            fee, lock_days)
            if net is not None:
                nets.append(net)
    return nets, missed


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config.load(repo_root, system="system_a")
    parser = argparse.ArgumentParser(description="Anticipatory-entry study")
    parser.add_argument("--db", type=Path,
                        default=repo_root / config.require("data.snapshot_poller")["db_path"])
    args = parser.parse_args(argv)
    store = SnapshotStore(args.db)

    stats = spread_stats(store)
    spreads = {s.item: s.median for s in stats}
    depth = {s.item: s.median_bids for s in stats}
    rules = RulesTable.load(repo_root / config.require("system_a.rules_table_path"))
    seed_file = repo_root / config.require("data.steam_history")["items_file"]
    universe = sorted(
        {l.strip() for l in seed_file.read_text().splitlines() if l.strip()}
    )
    lock_days = config.require("cooldown.trade_lock_days")
    fee_hist = config.get("costs.fee_history", [])
    fee_now = config.require("costs.buff_fee_pct")
    outcomes, _, _ = run_event_study(
        rules, store, universe, lock_days=lock_days,
        buff_fee_pct=fee_now, buff_fee_history=fee_hist,
        steam_fee_pct=config.require("costs.steam_fee_pct"),
    )
    results = run_modes(store, outcomes, spreads, lock_days, fee_now, fee_hist)

    print("== ENTRY-MODE COMPARISON (same OOS trades; BUFF frictions; "
          "balance events = HYPOTHETICAL leak lead) ==")
    for r in results:
        limit7 = r.limit_by_lead.get(7)
        line = (f"{r.event_date}  {r.item:45} reactive {r.reactive:+.1%}")
        for lead in LEAD_DAYS:
            v = r.limit_by_lead.get(lead)
            line += f"  L{lead}:" + (
                "miss" if v == "missed" else "n/a" if v is None else f"{v:+.1%}"
            )
        print(line)

    print("\n== AGGREGATES PER MODE ==")
    reactive = [r.reactive for r in results if r.reactive is not None]
    print(f"(a) reactive:            n={len(reactive)}  "
          f"mean {statistics.mean(reactive):+.1%}  "
          f"median {statistics.median(reactive):+.1%}")
    for lead in LEAD_DAYS:
        fills = [r.limit_by_lead[lead] for r in results
                 if isinstance(r.limit_by_lead.get(lead), float)]
        misses = sum(1 for r in results if r.limit_by_lead.get(lead) == "missed")
        total = len(fills) + misses
        if not total:
            continue
        ev = sum(fills) / total if total else 0.0   # missed trade = 0 return
        print(
            f"(b) limit  L={lead:>2}d: fills {len(fills)}/{total} "
            f"({len(fills)/total:.0%})  mean-of-fills "
            f"{statistics.mean(fills):+.1%}  " if fills else
            f"(b) limit  L={lead:>2}d: fills 0/{total}  ", end="",
        )
        if fills:
            print(f"median {statistics.median(fills):+.1%}  "
                  f"EV(misses=0) {ev:+.1%}")
        else:
            print()
        markets = [r.market_by_lead[lead] for r in results
                   if isinstance(r.market_by_lead.get(lead), float)]
        if markets:
            print(f"(c) market L={lead:>2}d: n={len(markets)}  "
                  f"mean {statistics.mean(markets):+.1%}  "
                  f"median {statistics.median(markets):+.1%}")

    m4a4_portfolio = [n for n in universe if n.startswith("M4A4 |")]
    false_nets, false_missed = false_leak_costs(
        store, m4a4_portfolio, spreads, lock_days, 0.025,
    )
    print(f"\n== FALSE-LEAK COST (mode-b mechanics, random dates, L=7) ==")
    if false_nets:
        false_mean = statistics.mean(false_nets)
        fill_rate = len(false_nets) / (len(false_nets) + false_missed)
        print(f"fills {len(false_nets)}/{len(false_nets) + false_missed} "
              f"({fill_rate:.0%})  mean {false_mean:+.1%}  "
              f"median {statistics.median(false_nets):+.1%}")
        best_lead = 7
        fills7 = [r.limit_by_lead[best_lead] for r in results
                  if isinstance(r.limit_by_lead.get(best_lead), float)]
        if fills7:
            win = statistics.mean(fills7)
            print("\n== EXPECTED VALUE UNDER LEAK HIT-RATE p "
                  "(no defensible point estimate — our track-record notes "
                  "record 10 hits, 0 misses = survivorship) ==")
            for p in HIT_RATES:
                print(f"p={p:.0%}: EV = {p * win + (1 - p) * false_mean:+.1%}")

    print("\n== LEAD TIME NEEDED FOR PASSIVE FILLS (measured bid depth) ==")
    for lead in LEAD_DAYS:
        entries = [r.limit_by_lead.get(lead) for r in results]
        total = sum(1 for e in entries if e is not None)
        fills = sum(1 for e in entries if isinstance(e, float))
        if total:
            print(f"L={lead:>2}d: fill rate {fills}/{total} ({fills/total:.0%})")
    thin = [s for s in stats if s.median_bids < 30]
    print(f"depth note: median bid-side depth ranges "
          f"{min(s.median_bids for s in stats):.0f}–"
          f"{max(s.median_bids for s in stats):.0f} orders; "
          f"{len(thin)} items have <30 standing bids — at size, thin books "
          f"need the LONG lead times regardless of price.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
