"""Measured-spread cost model over the poller's BUFF snapshots, and a
spread-aware re-costing of the out-of-sample event study.

System A is right-side/momentum, so execution is urgent on both legs:
  entry CROSSES UP to the ask, exit after T+7 CROSSES DOWN to the bid.
Round-trip cost = full spread + the era-correct BUFF seller fee.

Model (mid-to-mid gross move g from the Steam study, per-item spread s):
  entry price  = mid × (1 + s/2)
  exit proceeds = mid′ × (1 − s/2) × (1 − fee)
  net = (1 + g) × (1 − s/2) / (1 + s/2) × (1 − fee) − 1   (≈ g − s − fee)

This is BUFF costs applied to STEAM price moves — the best proxy available
without the paid BUFF archive. It answers: "if BUFF repriced like Steam did,
would real BUFF frictions eat the move?" Nothing here is tuned.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from shared.configuration import Config
from shared.store import SnapshotStore
from system_a.event_study import (
    PredictionOutcome, fee_for_source, run_event_study,
)
from system_a.rules import RulesTable


@dataclass(frozen=True)
class SpreadStats:
    item: str
    n: int
    median: float
    mean: float
    p25: float
    p75: float
    median_listings: float
    median_bids: float


def spread_stats(store: SnapshotStore, source: str = "buff") -> list[SpreadStats]:
    rows = store.conn.execute(
        "SELECT market_hash_name, lowest_sell, highest_buy, listing_count,"
        " buy_order_count FROM snapshots WHERE source = ?"
        " AND lowest_sell > 0 AND highest_buy > 0",
        (source,),
    ).fetchall()
    by_item: dict[str, list[tuple[float, int, int]]] = {}
    for name, ask, bid, listings, bids in rows:
        by_item.setdefault(name, []).append(((ask - bid) / ask, listings, bids))
    stats = []
    for name, obs in sorted(by_item.items()):
        spreads = sorted(s for s, _, _ in obs)
        q = statistics.quantiles(spreads, n=4) if len(spreads) >= 2 else [
            spreads[0], spreads[0], spreads[0]
        ]
        stats.append(
            SpreadStats(
                item=name, n=len(spreads),
                median=statistics.median(spreads),
                mean=statistics.mean(spreads),
                p25=q[0], p75=q[2],
                median_listings=statistics.median(l for _, l, _ in obs),
                median_bids=statistics.median(b for _, _, b in obs),
            )
        )
    return stats


def cross_spread_net(gross: float, spread: float, fee: float) -> float:
    """Net return when entry crosses to the ask and exit crosses to the bid."""
    return (1 + gross) * (1 - spread / 2) / (1 + spread / 2) * (1 - fee) - 1


def recost_outcomes(
    outcomes: list[PredictionOutcome],
    spreads: dict[str, float],
    buff_fee_pct: float,
    buff_fee_history: list[dict],
) -> list[tuple[PredictionOutcome, float, float, float]]:
    """(outcome, spread_used, era_fee, buff_costed_net) for every traded,
    out-of-sample outcome with a measured spread."""
    recosted = []
    for outcome in outcomes:
        if outcome.gross_pct is None or outcome.sample_class != "out_of_sample":
            continue
        spread = spreads.get(outcome.candidate.market_hash_name)
        if spread is None:
            continue
        fee = fee_for_source(
            "buff", outcome.exit_ts, buff_fee_pct, buff_fee_history, 0.0
        )
        recosted.append(
            (outcome, spread, fee,
             cross_spread_net(outcome.gross_pct, spread, fee))
        )
    return recosted


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config.load(repo_root, system="system_a")
    parser = argparse.ArgumentParser(description="Spread-aware OOS re-costing")
    parser.add_argument("--db", type=Path,
                        default=repo_root / config.require("data.snapshot_poller")["db_path"])
    args = parser.parse_args(argv)
    store = SnapshotStore(args.db)

    stats = spread_stats(store)
    if not stats:
        print("no buff snapshots — is the poller running?")
        return 1

    print("== MEASURED BUFF SPREADS (poller snapshots; spread = (ask-bid)/ask) ==")
    print(f"{'item':45} {'n':>3} {'median':>8} {'mean':>8} {'p25':>7} {'p75':>7} "
          f"{'listings':>9} {'bids':>6}")
    for s in stats:
        print(f"{s.item:45} {s.n:>3} {s.median:>8.2%} {s.mean:>8.2%} "
              f"{s.p25:>7.2%} {s.p75:>7.2%} {s.median_listings:>9.0f} {s.median_bids:>6.0f}")
    all_medians = [s.median for s in stats]
    print(f"\nacross items: median {statistics.median(all_medians):.2%}, "
          f"mean {statistics.mean(all_medians):.2%}, "
          f"min {min(all_medians):.2%}, max {max(all_medians):.2%}")

    # Liquidity relation: rank-split by listing count.
    ranked = sorted(stats, key=lambda s: s.median_listings)
    half = len(ranked) // 2
    thin, thick = ranked[:half], ranked[half:]
    print(
        f"liquidity relation: thin half (median {statistics.median(s.median_listings for s in thin):.0f} "
        f"listings) spread {statistics.median(s.median for s in thin):.2%}  vs  "
        f"thick half (median {statistics.median(s.median_listings for s in thick):.0f}) "
        f"spread {statistics.median(s.median for s in thick):.2%}"
    )

    # Re-run the OOS study (untouched) and re-cost under BUFF frictions.
    rules = RulesTable.load(repo_root / config.require("system_a.rules_table_path"))
    seed = repo_root / config.require("data.steam_history")["items_file"]
    universe = sorted(
        {line.strip() for line in seed.read_text().splitlines() if line.strip()}
    )
    outcomes, _, _ = run_event_study(
        rules, store, universe,
        lock_days=config.require("cooldown.trade_lock_days"),
        buff_fee_pct=config.require("costs.buff_fee_pct"),
        buff_fee_history=config.get("costs.fee_history", []),
        steam_fee_pct=config.require("costs.steam_fee_pct"),
    )
    spreads = {s.item: s.median for s in stats}
    recosted = recost_outcomes(
        outcomes, spreads,
        config.require("costs.buff_fee_pct"),
        config.get("costs.fee_history", []),
    )

    print("\n== OOS TRADES RE-COSTED AT BUFF FRICTIONS (cross to ask in, bid out) ==")
    by_rule: dict[str, list[float]] = {}
    for outcome, spread, fee, net in sorted(recosted, key=lambda r: r[0].event_date):
        by_rule.setdefault(outcome.candidate.rule, []).append(net)
        print(
            f"{outcome.event_date}  {outcome.candidate.market_hash_name:45} "
            f"[{outcome.candidate.rule}]  gross {outcome.gross_pct:+.1%}  "
            f"spread {spread:.2%} + fee {fee:.1%}  →  net {net:+.1%}"
        )
    print("\n== PER-RULE, BUFF-COSTED (out-of-sample only) ==")
    for rule, nets in sorted(by_rule.items()):
        clears = sum(1 for n in nets if n > 0)
        print(
            f"{rule:38} n={len(nets)}  mean {statistics.mean(nets):+.1%}  "
            f"median {statistics.median(nets):+.1%}  worst {min(nets):+.1%}  "
            f"clears costs: {clears}/{len(nets)}"
        )
    all_nets = [n for nets in by_rule.values() for n in nets]
    if all_nets:
        print(
            f"\nALL OOS TRADES: n={len(all_nets)}  mean {statistics.mean(all_nets):+.1%}  "
            f"median {statistics.median(all_nets):+.1%}  "
            f"positive: {sum(1 for n in all_nets if n > 0)}/{len(all_nets)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
