"""The paper desk trades real events only.

The single property worth defending here: the desk must never manufacture a
signal. A simulator that invents an event to have something to show would
produce a P&L number that means nothing, and the number would look exactly as
credible as a real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from system_a.paper_desk import (
    RealEvent, collect_events, events_from_labels, events_from_watch,
)
from system_a.rules import RulesTable

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rules() -> RulesTable:
    return RulesTable.load(REPO / "config" / "rules_table_a.yaml")


def _write_alert(repo: Path, day_ts: float, rule="trade_up_pool_change"):
    var = repo / "var"
    var.mkdir(parents=True, exist_ok=True)
    (var / "alerts.jsonl").write_text(json.dumps({
        "detected_at": day_ts, "posted_at": day_ts, "source": "Steam news",
        "event_rule": rule, "signal_type": "official_announcement",
        "confidence": 0.9, "excerpt": "Trade Up Contract extended, Covert",
    }) + "\n")


# ------------------------------------------------------------ no invention
def test_no_events_anywhere_means_no_events_traded(tmp_path):
    """With no alerts file and no labelled events, the desk must find nothing.
    Holding cash is the correct output, not a fabricated trade."""

    class Empty:
        historical_events: list = []

    assert collect_events(tmp_path, Empty()) == []


def test_watch_source_reads_only_real_alert_records(tmp_path):
    _write_alert(tmp_path, 1_761_091_200.0)
    evs = events_from_watch(tmp_path)
    assert len(evs) == 1
    assert evs[0].origin == "watch"
    assert evs[0].day == "2025-10-22"


def test_non_trade_up_alerts_are_ignored(tmp_path):
    """The desk trades one class. Other detections are not its business."""
    _write_alert(tmp_path, 1_761_091_200.0, rule="weapon_balance_change")
    assert events_from_watch(tmp_path) == []


def test_corrupt_alert_file_does_not_invent_or_crash(tmp_path):
    var = tmp_path / "var"
    var.mkdir()
    (var / "alerts.jsonl").write_text("{not json\n")
    assert events_from_watch(tmp_path) == []


def test_alert_without_a_timestamp_is_skipped(tmp_path):
    """An event with no date cannot be placed on the market timeline, so it
    must be dropped rather than defaulted to some convenient day."""
    var = tmp_path / "var"
    var.mkdir()
    (var / "alerts.jsonl").write_text(json.dumps({
        "event_rule": "trade_up_pool_change", "confidence": 0.9}) + "\n")
    assert events_from_watch(tmp_path) == []


# ------------------------------------------------------------ real labels
def test_labelled_source_finds_the_one_real_trade_up_event(rules):
    evs = events_from_labels(rules)
    assert [e.day for e in evs] == ["2025-10-22"], (
        "the labelled set should contain exactly one trade-up MECHANIC change")
    assert evs[0].origin == "labelled"


def test_the_lock_expiry_echo_is_not_treated_as_a_trigger(rules):
    """2025-10-30 is the T+7 consequence of the 10-22 change. Trading it as a
    second event would double-count one event."""
    assert "2025-10-30" not in [e.day for e in events_from_labels(rules)]


# ------------------------------------------------------------- precedence
def test_a_live_detection_supersedes_the_label_for_the_same_day(tmp_path, rules):
    _write_alert(tmp_path, 1_761_091_200.0)          # 2025-10-22
    evs = collect_events(tmp_path, rules)
    same_day = [e for e in evs if e.day == "2025-10-22"]
    assert len(same_day) == 1, "one real event must not become two"
    assert same_day[0].origin == "watch"


def test_events_are_ordered_in_time(tmp_path, rules):
    _write_alert(tmp_path, 1_800_000_000.0)
    evs = collect_events(tmp_path, rules)
    assert [e.ts for e in evs] == sorted(e.ts for e in evs)


def test_every_event_carries_its_provenance(tmp_path, rules):
    """The dashboard states where each trade came from, so origin is required
    on every event, never blank."""
    _write_alert(tmp_path, 1_761_091_200.0)
    for e in collect_events(tmp_path, rules):
        assert e.origin in {"watch", "labelled"}
        assert e.day and e.ts > 0


# ---------------------------------------------------------- book value
def test_position_value_is_value_not_pnl():
    """Regression. The equity line first used `marked_pnl`, which is
    unrealized P&L. Book value is cash plus what the inventory is worth, so
    using P&L subtracted the cost basis without adding the position back and
    drew an 18% drawdown that never happened. The reported total was correct
    because it is measured with every lot closed, which is exactly why the
    chart was the only place the error showed."""
    from shared.ledger import Ledger
    from system_a.paper_desk import position_value

    led = Ledger(trade_lock_days=7)
    lot = _fake_lot(led, item="AK-47 | Redline (Field-Tested)", qty=10, price=100.0)
    marks = {lot.market_hash_name: 100.0}

    value = position_value(led, marks, fee_pct=0.0)
    assert value == pytest.approx(1000.0), "flat price must be worth its cost"
    assert led.marked_pnl(marks, 0.0) == pytest.approx(0.0)


def test_position_value_nets_the_exit_fee():
    from shared.ledger import Ledger
    from system_a.paper_desk import position_value

    led = Ledger(trade_lock_days=7)
    lot = _fake_lot(led, item="AK-47 | Redline (Field-Tested)", qty=10, price=100.0)
    marks = {lot.market_hash_name: 100.0}
    assert position_value(led, marks, fee_pct=0.10) == pytest.approx(900.0)


def test_unmarked_lot_is_held_at_cost_not_dropped():
    """Dropping an unmarked lot reproduces the original bug in miniature."""
    from shared.ledger import Ledger
    from system_a.paper_desk import position_value

    led = Ledger(trade_lock_days=7)
    _fake_lot(led, item="AK-47 | Redline (Field-Tested)", qty=4, price=50.0)
    assert position_value(led, {}, fee_pct=0.0) == pytest.approx(200.0)


def _fake_lot(ledger, item: str, qty: int, price: float):
    """Open a lot through the Ledger's own entry point, so this test breaks if
    that contract changes rather than silently drifting."""
    from shared.schema import Fill, OrderSide
    return ledger.record_buy(Fill(
        client_order_id=f"t{qty}{price}", side=OrderSide.BUY,
        market_hash_name=item, qty=qty, price_cny=price, fee_cny=0.0, ts=0.0))
