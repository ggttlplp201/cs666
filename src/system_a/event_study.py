"""Event-study backtest over the rules table's labeled historical_events.

Closes the HANDOFF §A labeled-event-set item. For each event: apply the
rules table as of that date (the same mapping code the live engine uses),
take the predicted positions, and measure P&L AFTER the 2.5% BUFF fee and
the T+7 lock, on the Phase-1 Steam price history (source="steam" in the
snapshot store — run `python -m shared.steam_history` first).

Outputs per-event results, an aggregate, and a PER-RULE SCORECARD. Any rule
whose traded P&L fails net of fees+lock is flagged DO-NOT-TRADE — cross-check
against config system_a.rules_gating.disabled_rules.

Special cases from the wiring spec:
  - 2022-11-18 (M4A1-S nerf → instant M4A4 Desolate Space reaction, the
    Bai-Perron-confirmed break in paper1) is the PRIMARY VALIDATION CASE:
    our CUSUM detector must fire on the substitute's series near that date,
    or the pipeline itself is broken. Reported as CORRECTNESS, not strategy.
  - Events with status ACTIVE/live (2026-07-09) are excluded from the
    aggregate and reported as live forward tests.
  - Directional accuracy is scored for every prediction (including log-only
    rules); trade P&L is only booked for tradeable bullish candidates
    (long-only venue).

Caveat (Shared §2a): Steam median prices — no bid/ask spread is modeled
here, and Steam trades ~30-40% above BUFF. This validates the PREMISE
(does the predicted move clear fees + lock?), not live BUFF economics.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shared.configuration import Config
from shared.schema import Direction, Item, Signal, SignalType
from shared.store import SnapshotStore
from system_a.break_detector import CusumDetector
from system_a.rules import MappedCandidate, RulesTable

DAY = 86400.0
_MARKER = re.compile(r"\b(buff\w*|nerf\w*)\b", re.I)


def _event_ts(date_str: str) -> float:
    return datetime.strptime(str(date_str), "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    ).timestamp()


def weapon_directions_from_text(rules: RulesTable, text: str) -> dict[str, Direction]:
    """Assign each known weapon the direction of the nearest PRECEDING
    buff/nerf marker (segment semantics: 'Buffs: …, X, …. Nerfs: …'), falling
    back to the nearest following marker for suffix forms ('M4A1-S nerf')."""
    markers = [
        (m.start(), Direction.BULLISH if m.group(1).lower().startswith("buff")
         else Direction.BEARISH)
        for m in _MARKER.finditer(text)
    ]
    if not markers:
        return {}
    directions: dict[str, Direction] = {}
    for weapon in rules.weapons:
        match = re.search(rf"(?<![\w-]){re.escape(weapon)}(?![\w-])", text, re.I)
        if match is None:
            continue
        pos = match.start()
        preceding = [mk for mk in markers if mk[0] <= pos]
        if preceding:
            directions[weapon] = preceding[-1][1]
        else:
            directions[weapon] = min(markers, key=lambda mk: mk[0] - pos)[1]
    return directions


def signals_for_event(rules: RulesTable, event: dict) -> list[Signal]:
    """Synthesize the bus signal(s) this event would have produced, per
    weapon direction — the exact input shape the live engine consumes."""
    ts = _event_ts(event["date"])
    types = event["type"] if isinstance(event["type"], list) else [event["type"]]
    signals = []
    if "weapon_balance_change" in types:
        directions = weapon_directions_from_text(rules, str(event.get("change", "")))
        by_direction: dict[Direction, list[str]] = {}
        for weapon, direction in directions.items():
            by_direction.setdefault(direction, []).append(weapon)
        for direction, weapons in by_direction.items():
            signals.append(
                Signal(
                    tier=2, type=SignalType.CONFIRMED_UPDATE,
                    items=tuple(sorted(weapons)), direction=direction,
                    confidence=1.0, first_seen_ts=ts,
                    sources=("historical_events",),
                    event_rule="weapon_balance_change",
                )
            )
    if "trade_up_pool_change" in types:
        signals.append(
            Signal(
                tier=2, type=SignalType.CONFIRMED_UPDATE, items=(),
                direction=Direction.BEARISH, confidence=1.0,
                first_seen_ts=ts, sources=("historical_events",),
                event_rule="trade_up_pool_change",
            )
        )
    return signals


@dataclass
class PredictionOutcome:
    event_date: str
    candidate: MappedCandidate
    entry: float | None = None
    exit: float | None = None
    direction_hit: bool | None = None
    net_pnl_pct: float | None = None    # traded (bullish+tradeable) only, after fee


@dataclass
class RuleScore:
    rule: str
    predictions: int = 0
    scoreable: int = 0
    hits: int = 0
    trades: int = 0
    net_pnl_pct_sum: float = 0.0
    data_gaps: int = 0

    @property
    def verdict(self) -> str:
        if self.scoreable == 0:
            return "DATA-GAP (untested)"
        if self.trades and self.net_pnl_pct_sum <= 0:
            return "FAIL net of fees+lock — DO NOT TRADE"
        if self.trades:
            return "PASS"
        return f"directional only ({self.hits}/{self.scoreable} hits)"


def _price_at_or_before(series: list[Item], ts: float) -> float | None:
    """Last price known at decision time — for pre-event context only."""
    best = None
    for item in series:
        if item.ts <= ts:
            best = item.buff_lowest_sell_cny
        else:
            break
    return best


def _price_after(
    series: list[Item], ts: float, max_delay_days: float | None = None
) -> float | None:
    """Next-bar execution: the first price at/after ts. Never a past bar —
    that would be a fill at a price no longer available (look-ahead)."""
    for item in series:
        if item.ts >= ts:
            if max_delay_days is not None and item.ts - ts > max_delay_days * DAY:
                return None
            return item.buff_lowest_sell_cny
    return None


def fee_for_ts(ts: float, current_fee_pct: float, fee_history: list[dict]) -> float:
    """Resolve the seller fee in force at ts from the dated schedule
    (costs.fee_history): entries are {until: 'YYYY-MM-DD' (exclusive),
    fee_pct}. Historical events must pay the fee of their era."""
    for entry in sorted(fee_history, key=lambda e: str(e["until"])):
        if ts < _event_ts(str(entry["until"])):
            return float(entry["fee_pct"])
    return current_fee_pct


def run_event_study(
    rules: RulesTable,
    store: SnapshotStore,
    universe: list[str],
    fee_pct: float,
    lock_days: float,
    source: str = "steam",
    fee_history: list[dict] | None = None,
) -> tuple[list[PredictionOutcome], dict[str, RuleScore], list[str]]:
    outcomes: list[PredictionOutcome] = []
    scores: dict[str, RuleScore] = {}
    notes: list[str] = []
    series_cache = {name: store.series(name, source=source) for name in universe}

    for event in rules.historical_events:
        if "ACTIVE" in str(event.get("status", "")):
            notes.append(
                f"{event['date']}: LIVE FORWARD TEST — excluded from aggregate; "
                "tracking begins once poller/steam data covers it."
            )
            continue
        ts = _event_ts(event["date"])
        signals = signals_for_event(rules, event)
        if not signals:
            # Never skip an event silently — a labeled event the pipeline
            # can't synthesize is itself a finding.
            notes.append(
                f"{event['date']}: event type {event['type']} produced no "
                "signals — unhandled type or unparseable change text; "
                "needs mapping data (e.g. trade_up_lock_expiry item lists)."
            )
            continue
        for signal in signals:
            if signal.event_rule == "trade_up_pool_change":
                prices = {
                    n: (_price_at_or_before(series_cache[n], ts) or 0.0)
                    for n in universe
                }
                candidates = rules.map_trade_up_signal(signal, universe, prices, [])
                if not candidates:
                    scores.setdefault(
                        "trade_up_pool_change",
                        RuleScore("trade_up_pool_change"),
                    ).data_gaps += 1
                    notes.append(
                        f"{event['date']}: trade_up_pool_change unmappable — "
                        "collection→gold map missing (rules table §4 todo)."
                    )
            else:
                candidates = rules.map_signal(signal, universe)
            event_fee = fee_for_ts(
                ts + lock_days * DAY, fee_pct, fee_history or []
            )
            for candidate in candidates:
                score = scores.setdefault(candidate.rule, RuleScore(candidate.rule))
                score.predictions += 1
                outcome = PredictionOutcome(str(event["date"]), candidate)
                series = series_cache.get(candidate.market_hash_name, [])
                entry = _price_after(series, ts, max_delay_days=3.0)
                exit_price = _price_after(series, ts + lock_days * DAY)
                if entry is None or exit_price is None:
                    score.data_gaps += 1
                    outcomes.append(outcome)
                    continue
                outcome.entry, outcome.exit = entry, exit_price
                moved_up = exit_price > entry
                outcome.direction_hit = (
                    moved_up if candidate.direction == Direction.BULLISH
                    else not moved_up
                )
                score.scoreable += 1
                score.hits += int(outcome.direction_hit)
                if candidate.tradeable and candidate.direction == Direction.BULLISH:
                    net = (exit_price * (1 - event_fee) - entry) / entry
                    outcome.net_pnl_pct = net
                    score.trades += 1
                    score.net_pnl_pct_sum += net
                outcomes.append(outcome)
    return outcomes, scores, notes


def primary_validation_2022_11_18(
    rules: RulesTable,
    store: SnapshotStore,
    source: str = "steam",
    std_window: int = 20,
    drift_k: float = 0.5,
    threshold_h: float = 5.0,
) -> str:
    """CORRECTNESS gate: CUSUM must fire on M4A4 Desolate Space near the
    2022-11-18 M4A1-S nerf (paper1's Bai-Perron-confirmed instant break).
    Detector params must be the LIVE config's (system_a.break_detector) so
    the gate certifies the configuration that actually trades."""
    name = "M4A4 | Desolate Space (Field-Tested)"
    series = store.series(name, source=source)
    if not series:
        return "SKIPPED — no steam data for Desolate Space (run steam_history)"
    event_ts = _event_ts("2022-11-18")
    detector = CusumDetector(
        std_window=std_window, drift_k=drift_k, threshold_h=threshold_h,
        emitted_confidence=1.0,
    )
    for end in range(2, len(series) + 1):
        window = series[:end]
        alarm = detector.update(name, window)
        if alarm and abs(window[-1].ts - event_ts) <= 5 * DAY:
            return f"PASS — break detected at {window[-1].ts} (within 5d of event)"
        if window[-1].ts > event_ts + 10 * DAY:
            return "FAIL — no break detected within 10d of 2022-11-18: PIPELINE BROKEN"
    return "FAIL — series ends before the event window"


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config.load(repo_root, system="system_a")
    parser = argparse.ArgumentParser(description="Event-study backtest (rules table §3)")
    parser.add_argument("--db", type=Path,
                        default=repo_root / config.require("data.snapshot_poller")["db_path"])
    args = parser.parse_args(argv)

    # The event study measures EVERY rule/pair, including ones config has
    # disabled for live trading — that's how a disabled rule earns its way
    # back in (or stays out). Gating applies in the live stack only.
    rules = RulesTable.load(
        repo_root / config.require("system_a.rules_table_path"),
    )
    seed = repo_root / config.require("data.steam_history")["items_file"]
    universe = sorted(
        {line.strip() for line in seed.read_text().splitlines() if line.strip()}
    )
    store = SnapshotStore(args.db)
    if not store.counts_by_source().get("steam"):
        print("no source='steam' rows in the store — run:  PYTHONPATH=src "
              "python -m shared.steam_history   (needs STEAM_LOGIN_SECURE)")
        return 1

    outcomes, scores, notes = run_event_study(
        rules, store, universe,
        fee_pct=config.require("costs.buff_fee_pct"),
        lock_days=config.require("cooldown.trade_lock_days"),
        fee_history=config.get("costs.fee_history", []),
    )

    bd = config.require("system_a.break_detector")
    print("== PRIMARY VALIDATION (2022-11-18, correctness not strategy) ==")
    print(primary_validation_2022_11_18(
        rules, store,
        std_window=bd["std_window"], drift_k=bd["drift_k"],
        threshold_h=bd["threshold_h"],
    ))

    print("\n== PER-EVENT OUTCOMES ==")
    for o in outcomes:
        if o.entry is None:
            print(f"{o.event_date}  {o.candidate.market_hash_name}: no price data")
            continue
        traded = f" net {o.net_pnl_pct:+.1%}" if o.net_pnl_pct is not None else ""
        print(
            f"{o.event_date}  {o.candidate.market_hash_name}"
            f"  [{o.candidate.rule}/{o.candidate.confidence}]"
            f"  {o.candidate.direction.value}"
            f"  {'HIT' if o.direction_hit else 'miss'}{traded}"
        )

    print("\n== PER-RULE SCORECARD ==")
    for rule, s in sorted(scores.items()):
        pnl = f"  trade-pnl {s.net_pnl_pct_sum:+.1%}" if s.trades else ""
        print(
            f"{rule}: {s.hits}/{s.scoreable} directional hits, "
            f"{s.trades} trades{pnl}, {s.data_gaps} data gaps → {s.verdict}"
        )
    for note in notes:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
