from datetime import datetime, timezone
from pathlib import Path

import pytest

from shared.schema import Direction, Item, Signal, SignalType
from shared.store import SnapshotStore
from system_a.event_study import (
    DAY, buy_and_hold_baseline, fee_for_source, placebo_study,
    primary_validation_2022_11_18, run_event_study, signals_for_event,
    weapon_directions_from_text, _bar_after, _bar_at_or_before,
)
from system_a.rules import RulesTable

REPO_ROOT = Path(__file__).resolve().parents[1]
M4A4 = "M4A4 | Desolate Space (Field-Tested)"
M4A1S = "M4A1-S | Decimator (Field-Tested)"
STUDY_KW = dict(
    lock_days=7, buff_fee_pct=0.015,
    buff_fee_history=[{"until": "2026-04-14", "fee_pct": 0.025}],
    steam_fee_pct=0.15,
)


def _rules():
    return RulesTable.load(
        REPO_ROOT / "config" / "rules_table_a.yaml",
        disabled_rules=["map_pool_change"],
    )


def _synthetic_rules(event_date="2024-05-15"):
    """ct_rifle pair + one OUT-OF-SAMPLE synthetic event (never 2022-11-18 —
    that date is quarantined as in-sample by the study)."""
    return RulesTable(
        {
            "substitute_pairs": [
                {"id": "ct_rifle", "a": "M4A1-S", "b": "M4A4",
                 "confidence": "high", "evidence": "paper1"},
            ],
            "event_rules": [{"id": "weapon_balance_change", "confidence": "high"}],
            "historical_events": [
                {"date": event_date, "type": "weapon_balance_change",
                 "change": "M4A1-S nerf"},
            ],
        }
    )


def _ts(date):
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _steam_item(name, price, ts):
    return Item(
        market_hash_name=name, buff_lowest_sell_cny=price,
        buff_highest_buy_cny=price, buff_listing_count=0,
        buff_buy_order_count=0, buff_volume_24h=50, ts=ts,
    )


class TestTextParsing:
    def test_simple_nerf(self):
        directions = weapon_directions_from_text(_rules(), "M4A1-S nerf")
        assert directions == {"M4A1-S": Direction.BEARISH}

    def test_segmented_buffs_and_nerfs(self):
        text = ("Reload/ammo overhaul. Buffs: Galil AR +40-50%, M4A4 +25%, FAMAS +9%. "
                "Nerfs: SSG 08 -70%, AWP -57%, MP9 -60 rounds, M4A1-S -20.")
        directions = weapon_directions_from_text(_rules(), text)
        assert directions["M4A4"] == Direction.BULLISH
        assert directions["Galil AR"] == Direction.BULLISH
        assert directions["AWP"] == Direction.BEARISH
        assert directions["M4A1-S"] == Direction.BEARISH

    def test_signals_for_2022_11_18(self):
        rules = _rules()
        event = next(
            e for e in rules.historical_events if str(e["date"]) == "2022-11-18"
        )
        signals = signals_for_event(rules, event)
        assert len(signals) == 1
        assert signals[0].direction == Direction.BEARISH
        assert "M4A1-S" in signals[0].items
        assert signals[0].event_rule == "weapon_balance_change"


class TestAccounting:
    def test_fee_follows_source(self):
        history = [{"until": "2026-04-14", "fee_pct": 0.025}]
        # Steam data pays Steam's fee regardless of era
        assert fee_for_source("steam", _ts("2022-11-25"), 0.015, history, 0.15) == 0.15
        # BUFF data pays the era-correct BUFF fee
        assert fee_for_source("buff", _ts("2022-11-25"), 0.015, history, 0.15) == 0.025
        assert fee_for_source("buff", _ts("2026-05-01"), 0.015, history, 0.15) == 0.015

    def test_entry_never_uses_past_bar(self):
        series = [
            _steam_item(M4A4, 100.0, _ts("2022-11-16")),
            _steam_item(M4A4, 130.0, _ts("2022-11-20")),
        ]
        event = _ts("2022-11-18")
        assert _bar_after(series, event)[1] == 130.0        # next bar, not 100
        assert _bar_after(series, event, max_delay_days=1.0) is None
        assert _bar_at_or_before(series, event)[1] == 100.0  # context only

    def test_lock_runs_from_fill_not_event(self):
        """Entry fills 3 days late → exit must be ≥ fill+7d, not event+7d."""
        event = "2024-05-15"
        store = SnapshotStore()
        # no bars until event+3; then daily bars
        for d in range(3, 20):
            ts = _ts(event) + d * DAY
            store.insert([_steam_item(M4A4, 100.0 + d, ts),
                          _steam_item(M4A1S, 80.0, ts)], source="steam")
        outcomes, _, _ = run_event_study(
            _synthetic_rules(event), store, [M4A4, M4A1S], **STUDY_KW,
        )
        traded = [o for o in outcomes if o.net_pnl_pct is not None]
        assert traded
        held_days = (traded[0].exit_ts - traded[0].entry_ts) / DAY
        assert held_days >= 7.0


class TestEventStudy:
    def _store_with_reaction(self, event_date, jump_pct=0.30):
        store = SnapshotStore()
        t0 = _ts(event_date) - 30 * DAY
        for d in range(45):
            ts = t0 + d * DAY
            after = ts > _ts(event_date)
            store.insert(
                [
                    _steam_item(M4A4, 100.0 * (1 + jump_pct if after else 1.0), ts),
                    _steam_item(M4A1S, 80.0 * (0.85 if after else 1.0), ts),
                ],
                source="steam",
            )
        return store

    def test_substitute_trade_scored_at_steam_fee(self):
        rules = _synthetic_rules("2024-05-15")
        store = self._store_with_reaction("2024-05-15", jump_pct=0.30)
        outcomes, scores, _ = run_event_study(
            rules, store, [M4A4, M4A1S], **STUDY_KW,
        )
        traded = [o for o in outcomes if o.net_pnl_pct is not None]
        assert len(traded) == 1
        # +30% gross at Steam's 15% fee → 130*0.85/100-1 = +10.5%
        assert traded[0].gross_pct == pytest.approx(0.30)
        assert traded[0].net_pnl_pct == pytest.approx(1.30 * 0.85 - 1)
        score = scores["substitute_pair:ct_rifle"]
        assert score.n == 1 and score.mean > 0
        assert score.verdict.startswith("needs-more-data")  # n too small to TRADE

    def test_in_sample_event_never_scores(self):
        rules = _synthetic_rules("2022-11-18")   # the quarantined date
        store = self._store_with_reaction("2022-11-18", jump_pct=0.30)
        outcomes, scores, _ = run_event_study(
            rules, store, [M4A4, M4A1S], **STUDY_KW,
        )
        assert all(o.sample_class == "in_sample" for o in outcomes)
        # outcomes reported, but nothing counts toward the scorecard
        assert all(s.scoreable == 0 and s.n == 0 for s in scores.values())

    def test_scorecard_flags_failing_rule(self):
        rules = _synthetic_rules("2024-05-15")
        store = self._store_with_reaction("2024-05-15", jump_pct=-0.15)
        _, scores, _ = run_event_study(rules, store, [M4A4, M4A1S], **STUDY_KW)
        assert "DO-NOT-TRADE" in scores["substitute_pair:ct_rifle"].verdict

    def test_live_event_excluded_and_noted(self):
        rules = _rules()
        outcomes, scores, notes = run_event_study(
            rules, SnapshotStore(), [M4A4, M4A1S], **STUDY_KW,
        )
        assert any("LIVE FORWARD TEST" in n for n in notes)
        # trade-up not mapped when no collection_map is passed (default None)
        assert any("trade_up_pool_change not mapped" in n for n in notes)
        assert any("produced no signals" in n for n in notes)   # 2025-10-30 echo
        assert all(o.net_pnl_pct is None for o in outcomes)

    def test_scorecard_stats_fields(self):
        from system_a.event_study import RuleScore
        s = RuleScore("r", "high")
        s.returns = [0.10, -0.02, 0.04]
        s.scoreable, s.hits = 3, 2
        s.events = {"a", "b"}
        assert s.n == 3
        assert s.mean == pytest.approx(0.04)
        assert s.median == pytest.approx(0.04)
        assert s.worst == pytest.approx(-0.02)
        # benchmark defaults to 0 → edge +4% beats placebo → TRADE
        assert s.verdict.startswith("TRADE")
        assert s.edge_over_benchmark == pytest.approx(0.04)
        # but if the placebo also returns +4%, there is NO edge → DO-NOT-TRADE
        s.benchmark = 0.04
        assert "no edge over placebo" in s.verdict
        s.benchmark = 0.0
        s.returns = [-0.05]
        assert "DO-NOT-TRADE" in s.verdict


class TestNegativeControls:
    def _long_store(self):
        store = SnapshotStore()
        t0 = _ts("2023-01-01")
        for d in range(400):
            ts = t0 + d * DAY
            store.insert(
                [_steam_item(M4A4, 100.0 + 0.01 * d, ts),
                 _steam_item(M4A1S, 80.0, ts)],
                source="steam",
            )
        return store

    def test_placebo_deterministic_and_avoids_events(self):
        rules = _synthetic_rules("2023-06-15")
        store = self._long_store()
        p1 = placebo_study(rules, store, [M4A4], 7, 0.15, n_dates=10, seed=3)
        p2 = placebo_study(rules, store, [M4A4], 7, 0.15, n_dates=10, seed=3)
        assert p1 == p2 and len(p1) > 0
        # flat-drift series at 15% fee → placebo strongly negative
        assert max(p1) < 0

    def test_buy_and_hold_baseline_shape(self):
        store = self._long_store()
        baseline = buy_and_hold_baseline(store, [M4A4, M4A1S], ["2023-06-15"], 7, 0.15)
        assert set(baseline) == {"2023-06-15"}
        assert len(baseline["2023-06-15"]) == 2


class TestPrimaryValidation:
    def test_pass_on_break_at_event_date(self):
        store = SnapshotStore()
        event_ts = _ts("2022-11-18")
        t0 = event_ts - 60 * DAY
        for d in range(75):
            ts = t0 + d * DAY
            price = 100.0 + 0.05 * (d % 4)
            if ts >= event_ts:
                price = 132.0 + 0.05 * (d % 4)
            store.insert([_steam_item(M4A4, price, ts)], source="steam")
        result = primary_validation_2022_11_18(_rules(), store)
        assert result.startswith("PASS"), result
        assert "IN-SAMPLE" in result   # labeled as correctness, not edge

    def test_fail_without_break(self):
        store = SnapshotStore()
        t0 = _ts("2022-11-18") - 60 * DAY
        for d in range(75):
            store.insert(
                [_steam_item(M4A4, 100.0 + 0.05 * (d % 4), t0 + d * DAY)],
                source="steam",
            )
        assert primary_validation_2022_11_18(_rules(), store).startswith("FAIL")

    def test_skip_without_data(self):
        assert primary_validation_2022_11_18(_rules(), SnapshotStore()).startswith("SKIPPED")


def test_non_balance_event_rule_cannot_map_to_trades():
    rules = _rules()
    signal = Signal(
        tier=2, type=SignalType.CONFIRMED_UPDATE, items=("M4A1-S",),
        direction=Direction.BEARISH, confidence=1.0, first_seen_ts=0.0,
        event_rule="map_pool_change",
    )
    assert rules.map_signal(signal, [M4A4, M4A1S]) == []
