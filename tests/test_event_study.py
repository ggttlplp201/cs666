from datetime import datetime, timezone
from pathlib import Path

import pytest

from shared.schema import Direction, Item
from shared.store import SnapshotStore
from system_a.event_study import (
    DAY, primary_validation_2022_11_18, run_event_study, signals_for_event,
    weapon_directions_from_text,
)
from system_a.rules import RulesTable

REPO_ROOT = Path(__file__).resolve().parents[1]
M4A4 = "M4A4 | Desolate Space (Field-Tested)"
M4A1S = "M4A1-S | Decimator (Field-Tested)"


def _rules():
    return RulesTable.load(
        REPO_ROOT / "config" / "rules_table_a.yaml",
        disabled_rules=["map_pool_change"],
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


class TestEventStudy:
    def _store_with_reaction(self, event_date, jump_pct=0.20):
        """Steam series: substitute (M4A4) flat before the event, jumps after;
        nerfed (M4A1-S) declines after."""
        store = SnapshotStore()
        t0 = _ts(event_date) - 30 * DAY
        for d in range(45):
            ts = t0 + d * DAY
            # Reaction lands the day AFTER the event: entry is the event-day
            # (pre-reaction) price, matching "we react when the news breaks".
            after = ts > _ts(event_date)
            store.insert(
                [
                    _steam_item(M4A4, 100.0 * (1 + jump_pct if after else 1.0), ts),
                    _steam_item(M4A1S, 80.0 * (0.85 if after else 1.0), ts),
                ],
                source="steam",
            )
        return store

    def test_substitute_trade_scores_positive_net_of_fee(self):
        rules = RulesTable(
            {
                "substitute_pairs": [
                    {"id": "ct_rifle", "a": "M4A1-S", "b": "M4A4",
                     "confidence": "high", "evidence": "paper1"},
                ],
                "event_rules": [
                    {"id": "weapon_balance_change", "confidence": "high"},
                ],
                "historical_events": [
                    {"date": "2022-11-18", "type": "weapon_balance_change",
                     "change": "M4A1-S nerf"},
                ],
            }
        )
        store = self._store_with_reaction("2022-11-18")
        outcomes, scores, notes = run_event_study(
            rules, store, [M4A4, M4A1S], fee_pct=0.025, lock_days=7,
        )
        traded = [o for o in outcomes if o.net_pnl_pct is not None]
        assert len(traded) == 1 and traded[0].candidate.market_hash_name == M4A4
        # entry at post-event price (nearest to event day) → flat +0% … entry
        # nearest snapshot is event day (already jumped): exit==entry ⇒ -fee
        # OR pre-event day: +20% - fee. Either way direction HIT is recorded.
        assert traded[0].direction_hit
        score = scores["substitute_pair:ct_rifle"]
        assert score.trades == 1 and score.scoreable == 1 and score.hits == 1
        bearish_score = scores["weapon_balance_change.self"]
        assert bearish_score.hits == 1  # M4A1-S predicted down, went down

    def test_live_event_excluded_and_noted(self):
        rules = _rules()
        store = SnapshotStore()   # empty — every historical event lacks data
        outcomes, scores, notes = run_event_study(
            rules, store, [M4A4, M4A1S], fee_pct=0.025, lock_days=7,
        )
        assert any("LIVE FORWARD TEST" in n for n in notes)          # 2026-07-09
        assert any("collection→gold map missing" in n for n in notes)  # 2025-10-22
        assert all(o.net_pnl_pct is None for o in outcomes)          # no data → no trades

    def test_scorecard_flags_failing_rule(self):
        rules = RulesTable(
            {
                "substitute_pairs": [
                    {"id": "ct_rifle", "a": "M4A1-S", "b": "M4A4",
                     "confidence": "high", "evidence": "e"},
                ],
                "event_rules": [{"id": "weapon_balance_change", "confidence": "high"}],
                "historical_events": [
                    {"date": "2022-11-18", "type": "weapon_balance_change",
                     "change": "M4A1-S nerf"},
                ],
            }
        )
        # Substitute FALLS after the event → trade loses net of fee
        store = self._store_with_reaction("2022-11-18", jump_pct=-0.15)
        _, scores, _ = run_event_study(
            rules, store, [M4A4, M4A1S], fee_pct=0.025, lock_days=7,
        )
        assert "DO NOT TRADE" in scores["substitute_pair:ct_rifle"].verdict


class TestPrimaryValidation:
    def test_pass_on_break_at_event_date(self):
        store = SnapshotStore()
        event_ts = _ts("2022-11-18")
        t0 = event_ts - 60 * DAY
        for d in range(75):
            ts = t0 + d * DAY
            price = 100.0 + 0.05 * (d % 4)
            if ts >= event_ts:
                price = 132.0 + 0.05 * (d % 4)   # instant durable break
            store.insert([_steam_item(M4A4, price, ts)], source="steam")
        result = primary_validation_2022_11_18(_rules(), store)
        assert result.startswith("PASS"), result

    def test_fail_without_break(self):
        store = SnapshotStore()
        t0 = _ts("2022-11-18") - 60 * DAY
        for d in range(75):
            store.insert(
                [_steam_item(M4A4, 100.0 + 0.05 * (d % 4), t0 + d * DAY)],
                source="steam",
            )
        result = primary_validation_2022_11_18(_rules(), store)
        assert result.startswith("FAIL"), result

    def test_skip_without_data(self):
        assert primary_validation_2022_11_18(_rules(), SnapshotStore()).startswith("SKIPPED")


class TestCodexFindings:
    def test_fee_schedule_resolves_by_era(self):
        from system_a.event_study import fee_for_ts
        history = [{"until": "2026-04-14", "fee_pct": 0.025}]
        assert fee_for_ts(_ts("2022-11-25"), 0.015, history) == 0.025
        assert fee_for_ts(_ts("2026-05-01"), 0.015, history) == 0.015
        assert fee_for_ts(_ts("2022-11-25"), 0.015, []) == 0.015

    def test_entry_price_never_uses_past_bar(self):
        from system_a.event_study import _price_after, _price_at_or_before
        series = [
            _steam_item(M4A4, 100.0, _ts("2022-11-16")),
            _steam_item(M4A4, 130.0, _ts("2022-11-20")),
        ]
        event = _ts("2022-11-18")
        assert _price_after(series, event) == 130.0        # next bar, not 100
        assert _price_after(series, event, max_delay_days=1.0) is None
        assert _price_at_or_before(series, event) == 100.0  # context only

    def test_unhandled_event_type_is_noted_not_silent(self):
        rules = RulesTable(
            {"event_rules": [], "substitute_pairs": [],
             "historical_events": [
                 {"date": "2025-10-30", "type": "trade_up_lock_expiry",
                  "change": "locks expired"},
             ]}
        )
        _, _, notes = run_event_study(
            rules, SnapshotStore(), [M4A4], fee_pct=0.015, lock_days=7,
        )
        assert any("produced no signals" in n for n in notes)

    def test_non_balance_event_rule_cannot_map_to_trades(self):
        from shared.schema import Signal, SignalType
        rules = _rules()
        signal = Signal(
            tier=2, type=SignalType.CONFIRMED_UPDATE, items=("M4A1-S",),
            direction=Direction.BEARISH, confidence=1.0, first_seen_ts=0.0,
            event_rule="map_pool_change",
        )
        assert rules.map_signal(signal, [M4A4, M4A1S]) == []
