"""Event-study backtest over the rules table's labeled historical_events.

Accounting rules (audited 2026-07-18 — see git history for the flaws fixed):
  - The FEE FOLLOWS THE DATA SOURCE: source="steam" pays Steam's ~15% CS2
    sale fee; only BUFF-sourced data pays the BUFF fee (era-correct via
    costs.fee_history). Steam numbers validate the PREMISE on Steam
    economics; BUFF economics need BUFF data.
  - The T+7 lock runs from the FILL time (first bar at/after the event),
    not the event timestamp.
  - Fills are at the quoted daily median, uncapped — Steam medians carry no
    book, so returns are notional per-item, not capital-weighted. Stated
    limitation, not hidden.
  - 2022-11-18 is IN-SAMPLE by construction: the ct_rifle rule was written
    FROM that event (paper1 documented the break). It is reported ONLY as a
    pipeline-correctness gate and never counts toward any edge statistic.
    2021-09-22 is SEMI-in-sample (also cited in the pair's evidence) — it is
    scored but flagged; treat its numbers with suspicion.

Negative controls: a placebo run (same portfolio mechanics on random
non-event dates) and a naive buy-and-hold baseline over the same windows.
The rule must beat both, or the harness is measuring drift, not alpha.
"""

from __future__ import annotations

import argparse
import random
import re
import statistics
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

# In-sample events: the rule was authored from these — correctness gates only.
CORRECTNESS_ONLY_DATES = {"2022-11-18"}
# Cited in rule evidence but not the authoring event — scored, flagged.
SEMI_IN_SAMPLE_DATES = {"2021-09-22"}


def _event_ts(date_str: str) -> float:
    return datetime.strptime(str(date_str), "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    ).timestamp()


def _fmt(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


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


# --------------------------------------------------------------------- #
# price helpers — (ts, price) pairs so locks run from FILL time
# --------------------------------------------------------------------- #
def _bar_at_or_before(series: list[Item], ts: float) -> tuple[float, float] | None:
    best = None
    for item in series:
        if item.ts <= ts:
            best = (item.ts, item.buff_lowest_sell_cny)
        else:
            break
    return best


def _bar_after(
    series: list[Item], ts: float, max_delay_days: float | None = None
) -> tuple[float, float] | None:
    """Next-bar execution: first bar at/after ts. Never a past bar."""
    for item in series:
        if item.ts >= ts:
            if max_delay_days is not None and item.ts - ts > max_delay_days * DAY:
                return None
            return (item.ts, item.buff_lowest_sell_cny)
    return None


def fee_for_source(
    source: str,
    exit_ts: float,
    buff_fee_pct: float,
    buff_fee_history: list[dict],
    steam_fee_pct: float,
) -> float:
    """The fee follows the data source. Steam's CS2 fee is flat; BUFF's is
    the dated schedule (era-correct at exit time)."""
    if source == "steam":
        return steam_fee_pct
    for entry in sorted(buff_fee_history, key=lambda e: str(e["until"])):
        if exit_ts < _event_ts(str(entry["until"])):
            return float(entry["fee_pct"])
    return buff_fee_pct


# --------------------------------------------------------------------- #
@dataclass
class PredictionOutcome:
    event_date: str
    candidate: MappedCandidate
    sample_class: str                   # out_of_sample | semi_in_sample | in_sample
    entry_ts: float | None = None
    exit_ts: float | None = None
    entry: float | None = None
    exit: float | None = None
    direction_hit: bool | None = None
    gross_pct: float | None = None      # traded (bullish+tradeable) only
    net_pnl_pct: float | None = None    # after the SOURCE's fee


@dataclass
class RuleScore:
    rule: str
    confidence: str = "?"
    events: set = field(default_factory=set)
    scoreable: int = 0
    hits: int = 0
    returns: list = field(default_factory=list)   # net traded returns
    data_gaps: int = 0

    @property
    def n(self) -> int:
        return len(self.returns)

    @property
    def mean(self) -> float | None:
        return statistics.mean(self.returns) if self.returns else None

    @property
    def median(self) -> float | None:
        return statistics.median(self.returns) if self.returns else None

    @property
    def worst(self) -> float | None:
        return min(self.returns) if self.returns else None

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.scoreable if self.scoreable else None

    @property
    def verdict(self) -> str:
        if self.scoreable == 0:
            return "DATA-GAP / untested"
        if not self.returns:
            return f"directional-only ({self.hits}/{self.scoreable})"
        if self.mean <= 0:
            return "DO-NOT-TRADE (fails net of fees+lock)"
        if self.n < 3 or len(self.events) < 2:
            return "needs-more-data (positive but thin)"
        return "TRADE"


def run_event_study(
    rules: RulesTable,
    store: SnapshotStore,
    universe: list[str],
    lock_days: float,
    buff_fee_pct: float,
    buff_fee_history: list[dict],
    steam_fee_pct: float,
    source: str = "steam",
) -> tuple[list[PredictionOutcome], dict[str, RuleScore], list[str]]:
    """Scorecard covers OUT-OF-SAMPLE (+flagged semi-in-sample) events only;
    correctness-only events produce outcomes but never touch the scores."""
    outcomes: list[PredictionOutcome] = []
    scores: dict[str, RuleScore] = {}
    notes: list[str] = []
    series_cache = {name: store.series(name, source=source) for name in universe}

    for event in rules.historical_events:
        date = str(event["date"])
        if "ACTIVE" in str(event.get("status", "")):
            notes.append(
                f"{date}: LIVE FORWARD TEST — excluded from aggregate; "
                "tracking begins once poller/steam data covers it."
            )
            continue
        sample_class = (
            "in_sample" if date in CORRECTNESS_ONLY_DATES
            else "semi_in_sample" if date in SEMI_IN_SAMPLE_DATES
            else "out_of_sample"
        )
        ts = _event_ts(date)
        signals = signals_for_event(rules, event)
        if not signals:
            notes.append(
                f"{date}: event type {event['type']} produced no signals — "
                "unhandled type or unparseable change text; needs mapping "
                "data (e.g. trade_up_lock_expiry item lists)."
            )
            continue
        for signal in signals:
            if signal.event_rule == "trade_up_pool_change":
                prices = {}
                for n in universe:
                    bar = _bar_at_or_before(series_cache[n], ts)
                    prices[n] = bar[1] if bar else 0.0
                candidates = rules.map_trade_up_signal(signal, universe, prices, [])
                if not candidates:
                    score = scores.setdefault(
                        "trade_up_pool_change", RuleScore("trade_up_pool_change")
                    )
                    score.data_gaps += 1
                    notes.append(
                        f"{date}: trade_up_pool_change unmappable — "
                        "collection→gold map missing (rules table §4 todo)."
                    )
            else:
                candidates = rules.map_signal(signal, universe)
            for candidate in candidates:
                outcome = PredictionOutcome(date, candidate, sample_class)
                series = series_cache.get(candidate.market_hash_name, [])
                entry_bar = _bar_after(series, ts, max_delay_days=3.0)
                counting = sample_class != "in_sample"
                score = scores.setdefault(
                    candidate.rule,
                    RuleScore(candidate.rule, candidate.confidence),
                )
                if entry_bar is None:
                    if counting:
                        score.data_gaps += 1
                    outcomes.append(outcome)
                    continue
                outcome.entry_ts, outcome.entry = entry_bar
                # T+7 lock runs from the FILL, not the event.
                exit_bar = _bar_after(series, outcome.entry_ts + lock_days * DAY)
                if exit_bar is None:
                    if counting:
                        score.data_gaps += 1
                    outcomes.append(outcome)
                    continue
                outcome.exit_ts, outcome.exit = exit_bar
                moved_up = outcome.exit > outcome.entry
                outcome.direction_hit = (
                    moved_up if candidate.direction == Direction.BULLISH
                    else not moved_up
                )
                if counting:
                    score.events.add(date)
                    score.scoreable += 1
                    score.hits += int(outcome.direction_hit)
                if candidate.tradeable and candidate.direction == Direction.BULLISH:
                    fee = fee_for_source(
                        source, outcome.exit_ts, buff_fee_pct,
                        buff_fee_history, steam_fee_pct,
                    )
                    outcome.gross_pct = outcome.exit / outcome.entry - 1
                    outcome.net_pnl_pct = (
                        outcome.exit * (1 - fee) - outcome.entry
                    ) / outcome.entry
                    if counting:
                        score.returns.append(outcome.net_pnl_pct)
                outcomes.append(outcome)
    return outcomes, scores, notes


# --------------------------------------------------------------------- #
# Negative controls
# --------------------------------------------------------------------- #
def placebo_study(
    rules: RulesTable,
    store: SnapshotStore,
    portfolio: list[str],
    lock_days: float,
    steam_fee_pct: float,
    n_dates: int = 25,
    seed: int = 7,
    source: str = "steam",
) -> list[float]:
    """Same mechanics (next-bar entry, lock-from-fill exit, source fee) on
    random NON-event dates. If this looks like the event runs, the harness
    is measuring drift or a bug, not event alpha."""
    rng = random.Random(seed)
    event_ts = [
        _event_ts(str(e["date"])) for e in rules.historical_events
        if "ACTIVE" not in str(e.get("status", ""))
    ]
    series = {name: store.series(name, source=source) for name in portfolio}
    lo = max(s[0].ts for s in series.values() if s)
    hi = min(s[-1].ts for s in series.values() if s) - (lock_days + 5) * DAY
    returns: list[float] = []
    attempts = 0
    while len(returns) < n_dates * len(portfolio) and attempts < n_dates * 50:
        attempts += 1
        ts = rng.uniform(lo, hi)
        if any(abs(ts - e) < 14 * DAY for e in event_ts):
            continue  # stay clear of real events
        for name in portfolio:
            entry_bar = _bar_after(series[name], ts, max_delay_days=3.0)
            if entry_bar is None:
                continue
            exit_bar = _bar_after(series[name], entry_bar[0] + lock_days * DAY)
            if exit_bar is None:
                continue
            returns.append(
                (exit_bar[1] * (1 - steam_fee_pct) - entry_bar[1]) / entry_bar[1]
            )
    return returns


def buy_and_hold_baseline(
    store: SnapshotStore,
    universe: list[str],
    event_dates: list[str],
    lock_days: float,
    steam_fee_pct: float,
    source: str = "steam",
) -> dict[str, list[float]]:
    """Naive control: buy EVERY universe item at each event and hold through
    the lock — what indiscriminate exposure to the same windows returned."""
    result: dict[str, list[float]] = {}
    for date in event_dates:
        ts = _event_ts(date)
        window: list[float] = []
        for name in universe:
            series = store.series(name, source=source)
            entry_bar = _bar_after(series, ts, max_delay_days=3.0)
            if entry_bar is None:
                continue
            exit_bar = _bar_after(series, entry_bar[0] + lock_days * DAY)
            if exit_bar is None:
                continue
            window.append(
                (exit_bar[1] * (1 - steam_fee_pct) - entry_bar[1]) / entry_bar[1]
            )
        result[date] = window
    return result


def primary_validation_2022_11_18(
    rules: RulesTable,
    store: SnapshotStore,
    source: str = "steam",
    std_window: int = 20,
    drift_k: float = 0.5,
    threshold_h: float = 5.0,
) -> str:
    """CORRECTNESS gate, explicitly IN-SAMPLE: the ct_rifle rule was written
    from this event, so detection here proves the pipeline computes what
    paper1 documented — it is NOT evidence of predictive edge."""
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
            return (
                f"PASS — break detected {_fmt(window[-1].ts)} (within 5d). "
                "IN-SAMPLE correctness only; not edge."
            )
        if window[-1].ts > event_ts + 10 * DAY:
            return "FAIL — no break detected within 10d of 2022-11-18: PIPELINE BROKEN"
    return "FAIL — series ends before the event window"


# --------------------------------------------------------------------- #
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
    rules = RulesTable.load(repo_root / config.require("system_a.rules_table_path"))
    seed = repo_root / config.require("data.steam_history")["items_file"]
    universe = sorted(
        {line.strip() for line in seed.read_text().splitlines() if line.strip()}
    )
    store = SnapshotStore(args.db)
    if not store.counts_by_source().get("steam"):
        print("no source='steam' rows in the store — run:  PYTHONPATH=src "
              "python -m shared.steam_history   (needs STEAM_LOGIN_SECURE)")
        return 1

    lock_days = config.require("cooldown.trade_lock_days")
    steam_fee = config.require("costs.steam_fee_pct")
    outcomes, scores, notes = run_event_study(
        rules, store, universe,
        lock_days=lock_days,
        buff_fee_pct=config.require("costs.buff_fee_pct"),
        buff_fee_history=config.get("costs.fee_history", []),
        steam_fee_pct=steam_fee,
    )

    oos = [o for o in outcomes if o.sample_class == "out_of_sample"
           and o.net_pnl_pct is not None]
    semi = [o for o in outcomes if o.sample_class == "semi_in_sample"
            and o.net_pnl_pct is not None]
    print("== HEADLINE: OUT-OF-SAMPLE TRADES (Steam 15% fee, lock from fill) ==")
    if oos:
        rets = [o.net_pnl_pct for o in oos]
        print(f"n={len(rets)}  mean {statistics.mean(rets):+.1%}  "
              f"median {statistics.median(rets):+.1%}  worst {min(rets):+.1%}")
    else:
        print("no tradeable out-of-sample observations")
    if semi:
        rets = [o.net_pnl_pct for o in semi]
        print(f"semi-in-sample (2021-09-22, cited in rule evidence — suspect): "
              f"n={len(rets)}  mean {statistics.mean(rets):+.1%}")

    ct_portfolio = [n for n in universe if n.startswith("M4A4 |")]
    placebo = placebo_study(rules, store, ct_portfolio, lock_days, steam_fee)
    if placebo:
        print(f"\n== PLACEBO (same mechanics, {len(placebo)} random-date obs, "
              f"same M4A4 portfolio) ==")
        print(f"mean {statistics.mean(placebo):+.1%}  "
              f"median {statistics.median(placebo):+.1%}")

    oos_dates = sorted({o.event_date for o in outcomes
                        if o.sample_class == "out_of_sample"})
    baseline = buy_and_hold_baseline(store, universe, oos_dates, lock_days, steam_fee)
    print("\n== BUY-AND-HOLD BASELINE (all universe items, same windows) ==")
    for date, rets in baseline.items():
        if rets:
            print(f"{date}: mean {statistics.mean(rets):+.1%} (n={len(rets)})")

    print("\n== PER-EVENT OUTCOMES ==")
    for o in outcomes:
        tag = {"in_sample": " [IN-SAMPLE — correctness only]",
               "semi_in_sample": " [semi-in-sample]"}.get(o.sample_class, "")
        if o.entry is None:
            print(f"{o.event_date}  {o.candidate.market_hash_name}: no price data{tag}")
            continue
        trade = (f"  entry {_fmt(o.entry_ts)}@{o.entry:.2f} → exit "
                 f"{_fmt(o.exit_ts)}@{o.exit:.2f}  gross {o.gross_pct:+.1%} "
                 f"net {o.net_pnl_pct:+.1%}"
                 if o.net_pnl_pct is not None else "")
        print(f"{o.event_date}  {o.candidate.market_hash_name}"
              f"  [{o.candidate.rule}/{o.candidate.confidence}]"
              f"  {o.candidate.direction.value}"
              f"  {'HIT' if o.direction_hit else 'miss'}{trade}{tag}")

    print("\n== PER-RULE SCORECARD (out-of-sample + flagged semi; in-sample excluded) ==")
    header = (f"{'rule':38} {'conf':7} {'events':6} {'hit-rate':9} "
              f"{'mean':8} {'median':8} {'worst':8} {'n':3}  verdict")
    print(header)
    for rule, s in sorted(scores.items()):
        hr = f"{s.hits}/{s.scoreable}" if s.scoreable else "-"
        fmt = lambda v: f"{v:+.1%}" if v is not None else "-"
        print(f"{rule:38} {s.confidence:7} {len(s.events):6} {hr:9} "
              f"{fmt(s.mean):8} {fmt(s.median):8} {fmt(s.worst):8} {s.n:3}  {s.verdict}")

    print("\n== CORRECTNESS GATE (in-sample by construction) ==")
    bd = config.require("system_a.break_detector")
    print(primary_validation_2022_11_18(
        rules, store, std_window=bd["std_window"], drift_k=bd["drift_k"],
        threshold_h=bd["threshold_h"],
    ))
    for note in notes:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
