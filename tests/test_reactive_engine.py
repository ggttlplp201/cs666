from pathlib import Path

import pytest

from shared.bus import SignalBus
from shared.configuration import Config
from shared.execution import PaperBackend
from shared.ledger import DAY, Ledger
from shared.provenance import ProvenanceLog
from shared.schema import Direction, Fill, Item, OrderSide, Regime, Signal, SignalType
from shared.store import SnapshotStore
from system_a.break_detector import CusumDetector
from system_a.engine import ReactiveEngine
from system_a.risk import RiskGate
from system_a.rules import RulesTable

REPO_ROOT = Path(__file__).resolve().parents[1]
M4A4 = "M4A4 | Desolate Space (Field-Tested)"
M4A1S = "M4A1-S | Decimator (Field-Tested)"
T0 = 1_700_000_000.0


def _item(name, price, volume=30, buy_orders=5, ts=T0, listings=100):
    return Item(
        market_hash_name=name, buff_lowest_sell_cny=price,
        buff_highest_buy_cny=round(price * 0.97, 2), buff_listing_count=listings,
        buff_buy_order_count=buy_orders, buff_volume_24h=volume, ts=ts,
    )


def _leak(items=(M4A1S,), direction=Direction.BEARISH, conf=0.8, ts=T0, tier=1):
    return Signal(
        tier=tier, type=SignalType.UPDATE_LEAK, items=items, direction=direction,
        confidence=conf, first_seen_ts=ts, sources=("trusted",),
    )


def _fill_history(store, name, days, price=1000.0, volume=30, last_price=None,
                  last_volume=None, start=T0):
    """days snapshots ending at start+(days-1)*DAY; optional breakout last day."""
    for d in range(days):
        is_last = d == days - 1
        p = last_price if (is_last and last_price) else price
        v = last_volume if (is_last and last_volume) else volume
        store.insert([_item(name, p, volume=v, ts=start + d * DAY)])


class Harness:
    def __init__(self, tmp_path):
        self.config = Config.load(REPO_ROOT, system="system_a")
        self.store = SnapshotStore()
        self.bus = SignalBus()
        self.backend = PaperBackend(
            wallet_cny=self.config.require("capital.total"),
            fee_pct=self.config.require("costs.buff_fee_pct"),
            fill_volume_cap_k=self.config.require("position_sizing.volume_relative_k"),
        )
        self.ledger = Ledger(
            trade_lock_days=self.config.require("cooldown.trade_lock_days")
        )
        # Mechanics tests run with test-local gating, NOT the live config's:
        # the config reflects current backtest verdicts (which can disable
        # everything), while these tests verify the machinery works.
        self.rules = RulesTable.load(
            REPO_ROOT / "config" / "rules_table_a.yaml",
            disabled_rules=["map_pool_change"], disabled_pairs=[],
        )
        self.gate = RiskGate(self.config, self.ledger)
        self.provenance = ProvenanceLog(tmp_path / "prov.jsonl")
        self.engine = ReactiveEngine(
            self.config, self.store, self.bus, self.backend, self.ledger,
            self.rules, self.gate, self.provenance,
            universe=[M4A4, M4A1S],
        )

    def cycle(self, now_ts):
        self.backend.set_market(self.store.latest())
        self.engine.run_cycle(now_ts)

    def actions(self):
        return [(r["action"], r["item"], r["rule"]) for r in self.provenance.read_all()]


UNIVERSE = [M4A4, M4A1S]


def _rules(disabled=("map_pool_change",)):
    return RulesTable.load(
        REPO_ROOT / "config" / "rules_table_a.yaml", disabled_rules=list(disabled)
    )


class TestRules:
    def test_nerf_maps_self_bearish_substitute_bullish(self):
        candidates = _rules().map_signal(_leak(items=("M4A1-S",)), UNIVERSE)
        by_item = {c.market_hash_name: c for c in candidates}
        assert by_item[M4A1S].direction == Direction.BEARISH
        assert by_item[M4A4].direction == Direction.BULLISH
        assert by_item[M4A4].rule == "substitute_pair:ct_rifle"
        assert by_item[M4A4].confidence == "high" and by_item[M4A4].tradeable
        assert "Desolate Space" in by_item[M4A4].evidence  # evidence preserved

    def test_unclear_direction_maps_nothing(self):
        assert _rules().map_signal(_leak(direction=Direction.UNCLEAR), UNIVERSE) == []

    def test_low_confidence_pair_is_log_only(self):
        universe = ["AWP | Asiimov (Field-Tested)", "SSG 08 | Ghost Crusader (Field-Tested)"]
        candidates = _rules().map_signal(_leak(items=("AWP",)), universe)
        substitute = [c for c in candidates if c.rule == "substitute_pair:sniper"]
        assert substitute and not substitute[0].tradeable  # low confidence → log only
        self_side = [c for c in candidates if c.rule == "weapon_balance_change.self"]
        assert self_side and self_side[0].tradeable

    def test_both_sides_same_direction_no_substitution_trade(self):
        # e.g. Mar 2026 hit SSG-08 AND AWP: same direction ⇒ self-maps only
        universe = ["AWP | Asiimov (Field-Tested)"]
        candidates = _rules().map_signal(
            _leak(items=("AWP", "SSG 08")), universe
        )
        assert all(not c.rule.startswith("substitute_pair") for c in candidates)

    def test_weapon_extracted_from_item_name(self):
        candidates = _rules().map_signal(_leak(items=(M4A1S,)), UNIVERSE)
        assert {c.market_hash_name for c in candidates} == {M4A4, M4A1S}

    def test_disabled_rule_gates_tradeability(self):
        rules = _rules(disabled=("weapon_balance_change",))
        candidates = rules.map_signal(_leak(items=("M4A1-S",)), UNIVERSE)
        assert candidates and all(not c.tradeable for c in candidates)

    def test_trade_up_log_only_while_collection_map_missing(self):
        rules = _rules()
        signal = Signal(
            tier=2, type=SignalType.CONFIRMED_UPDATE, items=(M4A4, M4A1S),
            direction=Direction.BULLISH, confidence=1.0, first_seen_ts=T0,
            event_rule="trade_up_pool_change",
        )
        candidates = rules.map_trade_up_signal(
            signal, UNIVERSE, {M4A4: 100.0, M4A1S: 50.0}, collections_with_gold=[]
        )
        assert candidates and all(not c.tradeable for c in candidates)
        assert "COLLECTION→GOLD MAP MISSING" in candidates[0].evidence
        # cheap reds outrank expensive ones
        assert candidates[0].market_hash_name == M4A1S

    def test_calendar_rule_never_directional(self):
        rules = _rules()
        assert not rules.rule_tradeable("calendar_esports_event")
        assert not rules.event_rules["calendar_esports_event"].directional


class TestCusum:
    def test_alarm_on_structural_jump_and_reset(self):
        detector = CusumDetector(
            std_window=20, drift_k=0.5, threshold_h=5.0, emitted_confidence=0.75
        )
        history = [_item("X", 100.0 + 0.1 * (d % 3), ts=T0 + d * DAY) for d in range(25)]
        assert detector.update("X", history) is None
        history.append(_item("X", 140.0, ts=T0 + 25 * DAY))  # +40% break
        alarm = detector.update("X", history)
        assert alarm is not None
        assert alarm.type == SignalType.MARKET_BREAK
        assert alarm.direction == Direction.BULLISH
        assert alarm.tier == 2
        assert detector._pos["X"] == 0.0  # reset after alarm


class TestRiskGate:
    def _gate(self, tmp_path):
        h = Harness(tmp_path)
        return h, h.gate

    def test_bear_regime_blocks_tier1_allows_tier2(self, tmp_path):
        _, gate = self._gate(tmp_path)
        item = _item(M4A4, 1000.0)
        tier1 = gate.check_buy(item, Regime.BEAR, _leak(tier=1), 5, 100_000.0, T0)
        assert tier1.rule == "regime_bear_non_structural"
        tier2 = gate.check_buy(item, Regime.BEAR, _leak(tier=2), 5, 100_000.0, T0)
        assert tier2.approved

    def test_liquidity_and_buy_order_floors(self, tmp_path):
        _, gate = self._gate(tmp_path)
        thin = _item(M4A4, 1000.0, volume=5)
        assert gate.check_buy(thin, Regime.SIDEWAYS, _leak(), 5, 1e5, T0).rule == "liquidity_floor"
        no_bids = _item(M4A4, 1000.0, buy_orders=2)
        assert gate.check_buy(no_bids, Regime.SIDEWAYS, _leak(), 5, 1e5, T0).rule == "buy_order_floor"

    def test_chase_cap_shrinks_qty(self, tmp_path):
        _, gate = self._gate(tmp_path)
        # per-item cap = 100k * 2/10 = 20k → at 1500 CNY max 13 units
        item = _item(M4A4, 1500.0, volume=200)
        result = gate.check_buy(item, Regime.SIDEWAYS, _leak(), 50, 1e5, T0)
        assert result.approved and result.qty == 13

    def test_blocklist_and_kill_switch(self, tmp_path):
        _, gate = self._gate(tmp_path)
        item = _item(M4A4, 1000.0)
        gate.block(M4A4)
        assert gate.check_buy(item, Regime.SIDEWAYS, _leak(), 5, 1e5, T0).rule == "blocklist"
        gate.runtime_blocklist.clear()
        gate.trip_kill_switch("test")
        assert gate.check_buy(item, Regime.SIDEWAYS, _leak(), 5, 1e5, T0).rule == "kill_switch"


class TestEngine:
    def _prime_market(self, h, breakout=True):
        """21 days of history; M4A4 ends on a confirmed breakout (pattern 3
        + widening band) when breakout=True, else a weak rally (pattern 4)."""
        last_volume = 60 if breakout else 10
        _fill_history(
            h.store, M4A4, days=21, price=1000.0,
            last_price=1040.0, last_volume=last_volume,
        )
        _fill_history(h.store, M4A1S, days=21, price=800.0)
        return T0 + 20 * DAY + 60  # just after the last snapshot

    def test_nerf_leak_buys_substitute_on_confirmation(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h, breakout=True)
        h.bus.publish(_leak(items=("M4A1-S",), ts=now - 3600))
        h.cycle(now)
        assert h.ledger.position_qty(M4A4) > 0
        assert ("buy_placed", M4A4, "substitute_pair:ct_rifle") in h.actions()

    def test_weak_rally_pattern4_refused(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h, breakout=False)
        h.bus.publish(_leak(items=("M4A1-S",), ts=now - 3600))
        h.cycle(now)
        assert h.ledger.position_qty(M4A4) == 0
        assert ("buy_refused", M4A4, "weak_rally_pattern4") in h.actions()

    def test_low_confidence_signal_ignored(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h)
        h.bus.publish(_leak(items=("M4A1-S",), conf=0.4, ts=now - 3600))
        h.cycle(now)
        assert h.ledger.position_qty(M4A4) == 0

    def test_hype_signal_blocklists_item(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h)
        h.bus.publish(
            Signal(
                tier=1, type=SignalType.HYPE, items=(M4A4,),
                direction=Direction.BEARISH, confidence=0.5,
                first_seen_ts=now - 60, sources=("cn_forum",),
            )
        )
        h.bus.publish(_leak(items=("M4A1-S",), ts=now - 3600))
        h.cycle(now)
        assert h.gate.blocklisted(M4A4)
        assert h.ledger.position_qty(M4A4) == 0
        assert ("buy_refused", M4A4, "blocklist") in h.actions()

    def test_stale_data_pauses_pipeline(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h) + 10 * DAY  # feed long stale
        h.bus.publish(_leak(items=("M4A1-S",), ts=now - 3600))
        h.cycle(now)
        assert h.ledger.position_qty(M4A4) == 0
        assert ("pipeline_paused", None, "stale_data") in h.actions()

    def _seed_lot(self, h, name, buy_price, qty, buy_ts):
        fill = Fill(
            client_order_id=f"seed-{name}-{buy_ts}-{qty}", side=OrderSide.BUY,
            market_hash_name=name, qty=qty, price_cny=buy_price, fee_cny=0.0,
            ts=buy_ts,
        )
        h.ledger.record_buy(fill)
        h.backend.inventory[name] = h.backend.inventory.get(name, 0) + qty

    def test_take_profit_bracket_fires_after_unlock(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h, breakout=True)
        # bid on M4A4 last snapshot = 1040*0.97 = 1008.8 → +12% vs buy at 900
        self._seed_lot(h, M4A4, buy_price=900.0, qty=2, buy_ts=now - 8 * DAY)
        h.cycle(now)
        assert h.ledger.position_qty(M4A4) == 0
        assert ("sell_placed", M4A4, "take_profit") in h.actions()
        assert h.ledger.realized_pnl() > 0

    def test_stop_loss_bracket_cuts_loser(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h, breakout=True)
        self._seed_lot(h, M4A4, buy_price=1200.0, qty=2, buy_ts=now - 8 * DAY)
        h.cycle(now)  # bid 1008.8 vs 1200 → -16% ≤ -10% stop
        assert h.ledger.position_qty(M4A4) == 0
        assert ("sell_placed", M4A4, "stop_loss") in h.actions()

    def test_locked_lot_not_sold_by_brackets(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h, breakout=True)
        self._seed_lot(h, M4A4, buy_price=900.0, qty=2, buy_ts=now - 2 * DAY)
        h.cycle(now)
        assert h.ledger.position_qty(M4A4) == 2  # still locked → held

    def test_bearish_mark_exits_unlocked_holding(self, tmp_path):
        h = Harness(tmp_path)
        now = self._prime_market(h, breakout=True)
        # held M4A1-S lot bought at its flat price; nerf leak marks it bearish
        self._seed_lot(h, M4A1S, buy_price=800.0, qty=2, buy_ts=now - 9 * DAY)
        h.bus.publish(_leak(items=("M4A1-S",), ts=now - 3600))
        h.cycle(now)
        assert h.ledger.position_qty(M4A1S) == 0
        assert ("sell_placed", M4A1S, "thesis_break_exit") in h.actions()


class TestPostEventSizingAndScaleOut:
    def test_gate_sizes_on_baseline_not_spiked_volume(self, tmp_path):
        h = Harness(tmp_path)
        # baseline volume 30, event-day print 200 → cap must follow baseline
        result = h.gate.check_buy(
            _item(M4A4, 1000.0, volume=200), Regime.SIDEWAYS, _leak(), 50,
            1e5, T0, baseline_volume_24h=30.0,
        )
        assert result.approved
        assert result.qty == int(0.35 * 30)  # 10, not int(0.35*200)=70

    def test_thin_book_exit_scales_out_over_cycles(self, tmp_path):
        h = Harness(tmp_path)
        now = self._now = TestEngine._prime_market(TestEngine(), h, breakout=True)
        # 19 units bought at 900, unlocked, book takes only 0.35*60=21 → full;
        # shrink volume to 20 → depth cap 7 per cycle
        TestEngine._seed_lot(TestEngine(), h, M4A4, 900.0, 19, now - 9 * DAY)
        h.store.insert([_item(M4A4, 1040.0, volume=20, ts=now + 60)])
        h.cycle(now + 120)
        assert h.ledger.position_qty(M4A4) == 12  # sold 7
        assert ("sell_placed", M4A4, "take_profit") in h.actions()
        h.store.insert([_item(M4A4, 1040.0, volume=20, ts=now + DAY)])
        h.cycle(now + DAY + 60)
        h.store.insert([_item(M4A4, 1040.0, volume=20, ts=now + 2 * DAY)])
        h.cycle(now + 2 * DAY + 60)
        assert h.ledger.position_qty(M4A4) == 0  # fully scaled out


class TestMonitorTimeGate:
    def test_future_posts_not_ingested_early(self, tmp_path):
        import json as _json
        from system_a.monitor import FileReplaySource, KeywordClassifier, MonitorAgent
        posts = [
            {"source": "trusted", "platform": "x",
             "text": "Datamined: CS2 update nerfs the M4A1-S", "ts": 1000.0},
        ]
        f = tmp_path / "posts.jsonl"
        f.write_text("\n".join(_json.dumps(p) for p in posts))
        bus = SignalBus()
        agent = MonitorAgent(
            sources=[FileReplaySource(f)], classifier=KeywordClassifier(),
            bus=bus, allowlist={"trusted": 1.0},
            known_items=[M4A1S], corroboration_min_sources=3,
        )
        assert agent.run_cycle(now_ts=500.0) == []      # post is in the future
        assert len(agent.run_cycle(now_ts=1500.0)) == 1  # ingested once due


class TestDeploymentCeiling:
    def test_regime_ceiling_caps_total_deployment(self, tmp_path):
        h = Harness(tmp_path)
        # sideways ceiling = 0.50 * 100k = 50k; already deployed 45k
        TestEngine._seed_lot(TestEngine(), h, M4A1S, 900.0, 50, T0 - DAY)
        item = _item(M4A4, 1000.0, volume=200)
        result = h.gate.check_buy(
            item, Regime.SIDEWAYS, _leak(), 50, 1e5, T0,
        )
        assert result.approved and result.qty == 5  # 5k headroom / 1000
        TestEngine._seed_lot(TestEngine(), h, M4A4, 1000.0, 5, T0 - DAY)
        refused = h.gate.check_buy(item, Regime.SIDEWAYS, _leak(), 50, 1e5, T0)
        assert refused.rule == "deployment_ceiling"


class TestEchoPersistence:
    def test_scheduled_echo_survives_engine_restart(self, tmp_path):
        h = Harness(tmp_path)
        h.engine._scheduled_echoes.append((T0 + 7 * DAY, (M4A4,), "trade_up_lock_expiry_echo"))
        h.engine._save_echoes()
        h2 = Harness(tmp_path)   # fresh engine, same provenance dir
        assert h2.engine._scheduled_echoes == [(T0 + 7 * DAY, (M4A4,), "trade_up_lock_expiry_echo")]


def test_backtest_failed_pair_is_log_only_live():
    # scoped_rifles FAILED the 2026-07-18 event study (-9.3% net) → config
    # disabled_pairs makes it log-only in the live stack despite confidence=medium
    rules = RulesTable.load(
        REPO_ROOT / "config" / "rules_table_a.yaml",
        disabled_rules=["map_pool_change"], disabled_pairs=["scoped_rifles"],
    )
    universe = ["SG 553 | Cyrex (Field-Tested)"]
    candidates = rules.map_signal(_leak(items=("AUG",)), universe)
    pair_candidates = [c for c in candidates if c.rule == "substitute_pair:scoped_rifles"]
    assert pair_candidates and all(not c.tradeable for c in pair_candidates)
