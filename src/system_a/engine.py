"""Reactive engine (System A §4): monitor/bus → rules table → confirmation →
risk gate → paper execution, with bracket exits and per-cycle safety rails.

Cycle order (§10 architecture):
  1. safety rails — stale data pause, reconciliation pause, daily-loss trip
  2. break detector on the market feed (a non-social update alarm, §4.1a)
  3. regime classification (Shared §2)
  4. hype signals → runtime blocklist (§4.2: risk signal, never an entry)
  5. actionable signals → rules-table mapping → right-side confirmation
     (§3.1/§3.3) → risk gate (§8) → buy
  6. bearish marks on held items → thesis-break exits post-unlock
  7. bracket exits: hard TP/SL on unlocked lots (Shared §6.3, gross price
     thresholds per the notes; profitability is still reported net)

Every decision — including every refusal — lands in the provenance log
(Shared §12) with the rule that produced it.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone

from shared.bus import SignalBus
from shared.configuration import Config
from shared.execution import ExecutionBackend, reconcile
from shared.indicators import bandwidth_widening, volume_price_state
from shared.ledger import Ledger, Lot
from shared.provenance import ProvenanceLog
from shared.regime import classify_regime
from shared.schema import (
    Direction, Item, Order, OrderSide, Regime, Signal, SignalType,
)
from shared.store import SnapshotStore
from system_a.break_detector import CusumDetector
from system_a.risk import RiskGate
from system_a.rules import MappedCandidate, RulesTable

ACTIONABLE_TYPES = (
    SignalType.UPDATE_LEAK,
    SignalType.OFFICIAL_ANNOUNCEMENT,
    SignalType.CONFIRMED_UPDATE,
    SignalType.MARKET_BREAK,
)


class ReactiveEngine:
    def __init__(
        self,
        config: Config,
        store: SnapshotStore,
        bus: SignalBus,
        backend: ExecutionBackend,
        ledger: Ledger,
        rules: RulesTable,
        gate: RiskGate,
        provenance: ProvenanceLog,
        universe: list[str],
    ):
        self.config = config
        self.store = store
        self.bus = bus
        self.backend = backend
        self.ledger = ledger
        self.rules = rules
        self.gate = gate
        self.provenance = provenance
        bd = config.require("system_a.break_detector")
        self.break_detector = CusumDetector(
            std_window=bd["std_window"], drift_k=bd["drift_k"],
            threshold_h=bd["threshold_h"],
            emitted_confidence=bd["emitted_confidence"],
        ) if bd.get("enabled") else None
        self._ids = itertools.count()
        self._daily_realized_baseline: tuple[str, float] | None = None
        self.tracked_items = sorted(set(universe))
        # Scheduled T+7 lock-expiry echoes (rules table: trade_up timing) —
        # (due_ts, bearish item names, originating rule id). Persisted to disk
        # so a restart between the trade-up event and lock expiry cannot lose
        # the predictable second-wave exit.
        self._echo_state_path = provenance.path.parent / "scheduled_echoes_a.json"
        self._scheduled_echoes: list[tuple[float, tuple[str, ...], str]] = (
            self._load_echoes()
        )
        self._alerted_watch_keys: set[str] = set()

    def _load_echoes(self) -> list[tuple[float, tuple[str, ...], str]]:
        if not self._echo_state_path.exists():
            return []
        return [
            (float(due), tuple(items), str(rule))
            for due, items, rule in json.loads(self._echo_state_path.read_text())
        ]

    def _save_echoes(self) -> None:
        self._echo_state_path.write_text(
            json.dumps([[due, list(items), rule]
                        for due, items, rule in self._scheduled_echoes])
        )

    def _next_id(self, prefix: str) -> str:
        return f"a-{prefix}-{next(self._ids)}"

    # ------------------------------------------------------------------ #
    def run_cycle(self, now_ts: float) -> None:
        snapshot = self.store.latest()
        regime = self._classify_regime()

        if not self._safety_rails_ok(now_ts, snapshot, regime):
            return
        if self.break_detector:
            self._run_break_detector(snapshot)

        signals = self._actionable_signals(now_ts)
        self._blocklist_hyped_items(now_ts, regime)
        self._log_watch_alerts(now_ts, regime)

        bearish_marks: set[str] = set()
        for signal in signals:
            for candidate in self._map(signal, snapshot):
                if not candidate.tradeable:
                    # Low-confidence / disabled / data-gap rules never trade —
                    # they are logged so the backtest can score them (§12).
                    self._log(
                        now_ts, "rule_log_only", candidate.market_hash_name,
                        candidate.rule, regime, [_sig(signal)],
                        inputs={"direction": candidate.direction.value,
                                "confidence": candidate.confidence,
                                "evidence": candidate.evidence},
                    )
                elif candidate.direction == Direction.BEARISH:
                    bearish_marks.add(candidate.market_hash_name)
                else:
                    self._try_buy(candidate, signal, snapshot, regime, now_ts)
            self._maybe_schedule_echo(signal, snapshot, now_ts)
        bearish_marks |= self._due_echo_marks(now_ts, regime)
        self._thesis_break_exits(bearish_marks, snapshot, regime, now_ts)
        self._bracket_exits(snapshot, regime, now_ts)

    def _map(self, signal: Signal, snapshot: dict[str, Item]):
        if signal.event_rule == "trade_up_pool_change":
            prices = {
                n: i.buff_lowest_sell_cny for n, i in snapshot.items()
            }
            gating = self.config.get("system_a.rules_gating", {}) or {}
            return self.rules.map_trade_up_signal(
                signal, self.tracked_items, prices,
                gating.get("collections_with_gold", []),
            )
        return self.rules.map_signal(signal, self.tracked_items)

    def _maybe_schedule_echo(
        self, signal: Signal, snapshot: dict[str, Item], now_ts: float
    ) -> None:
        """Trade-up events echo at T+7 when crafted-item locks expire
        (2025-10-22 → 2025-10-30: further 10–15% dip). Schedule the bearish
        leg to re-fire then, so exits are planned around the second wave."""
        if signal.event_rule != "trade_up_pool_change":
            return
        bearish = tuple(
            c.market_hash_name for c in self._map(signal, snapshot)
            if c.direction == Direction.BEARISH
        )
        if not bearish:
            return
        lock_days = self.config.require("cooldown.trade_lock_days")
        due = signal.first_seen_ts + lock_days * 86400.0
        if all(e[0] != due or e[1] != bearish for e in self._scheduled_echoes):
            self._scheduled_echoes.append((due, bearish, "trade_up_lock_expiry_echo"))
            self._save_echoes()

    def _due_echo_marks(self, now_ts: float, regime: Regime) -> set[str]:
        marks: set[str] = set()
        remaining = []
        for due, items, rule in self._scheduled_echoes:
            if due <= now_ts:
                marks.update(items)
                self._log(
                    now_ts, "scheduled_echo_fired", None, rule, regime, [],
                    inputs={"items": list(items), "scheduled_for": due},
                )
            else:
                remaining.append((due, items, rule))
        if len(remaining) != len(self._scheduled_echoes):
            self._scheduled_echoes = remaining
            self._save_echoes()
        return marks

    def _log_watch_alerts(self, now_ts: float, regime: Regime) -> None:
        """Keyword-watch signals (e.g. a Cache-collection announcement, which
        would resolve the map_pool_change ambiguity) — surfaced, never traded."""
        decay = self.config.require("system_a.monitor")["signal_decay_hours"]
        for signal in self.bus.active([1, 2, 3], now_ts, decay):
            if signal.type == SignalType.ATTENTION and signal.key() not in self._alerted_watch_keys:
                self._alerted_watch_keys.add(signal.key())
                self._log(
                    now_ts, "watch_alert", None, "monitor_keyword_watch",
                    regime, [_sig(signal)],
                    inputs={"watched": list(signal.items),
                            "event_rule": signal.event_rule},
                )

    # ------------------------------------------------------------------ #
    def _safety_rails_ok(
        self, now_ts: float, snapshot: dict[str, Item], regime: Regime
    ) -> bool:
        max_age = 4 * self.config.require("data.refresh_seconds")
        if self.config.get("data.pause_trading_on_stale_or_divergent") and (
            not snapshot or self.store.is_stale(now_ts, max_age)
        ):
            self._log(now_ts, "pipeline_paused", None, "stale_data", regime, [])
            return False
        problems = reconcile(
            self.backend,
            {name: self.ledger.position_qty(name) for name in self.tracked_items},
        )
        if problems:
            self._log(
                now_ts, "pipeline_paused", None, "reconciliation_mismatch",
                regime, [], inputs={"problems": problems},
            )
            return False
        self._check_daily_loss(now_ts, regime)
        if self.gate.kill_switch:
            self._log(now_ts, "pipeline_paused", None, "kill_switch", regime, [])
            return False
        return True

    def _check_daily_loss(self, now_ts: float, regime: Regime) -> None:
        day = datetime.fromtimestamp(now_ts, tz=timezone.utc).date().isoformat()
        realized = self.ledger.realized_pnl()
        if self._daily_realized_baseline is None or self._daily_realized_baseline[0] != day:
            self._daily_realized_baseline = (day, realized)
            return
        loss_today = realized - self._daily_realized_baseline[1]
        limit = self.config.require("risk_controls.daily_loss_limit_pct") * \
            self.config.require("capital.total")
        if loss_today <= limit:  # limit is negative
            self.gate.trip_kill_switch("daily_loss_limit")
            self._log(
                now_ts, "kill_switch_tripped", None, "daily_loss_limit", regime,
                [], inputs={"loss_today": loss_today, "limit": limit},
            )

    def _classify_regime(self) -> Regime:
        r = self.config.require("regime")
        history = {
            name: self.store.series(name) for name in self.tracked_items
        }
        return classify_regime(
            history,
            breadth_window=r["breadth_window"],
            bull_breadth_min=r["bull_breadth_min"],
            bear_breadth_max=r["bear_breadth_max"],
            weak_volume_ratio_max=r["weak_volume_ratio_max"],
            bollinger_num_std=self.config.require("indicators")["bollinger_num_std"],
        )

    def _run_break_detector(self, snapshot: dict[str, Item]) -> None:
        for name in self.tracked_items:
            history = self.store.series(name)
            alarm = self.break_detector.update(name, history)
            if alarm:
                self.bus.publish(alarm)

    def _actionable_signals(self, now_ts: float) -> list[Signal]:
        monitor_config = self.config.require("system_a.monitor")
        signals = self.bus.active(
            tiers=list(monitor_config["tiers_consumed"]),
            now_ts=now_ts,
            max_age_hours=monitor_config["signal_decay_hours"],
        )
        min_confidence = monitor_config["confidence_min_to_act"]
        return [
            s for s in signals
            if s.type in ACTIONABLE_TYPES and s.confidence >= min_confidence
        ]

    def _blocklist_hyped_items(self, now_ts: float, regime: Regime) -> None:
        monitor_config = self.config.require("system_a.monitor")
        for signal in self.bus.active([1, 2], now_ts, monitor_config["signal_decay_hours"]):
            if signal.type == SignalType.HYPE:
                for item in signal.items:
                    if not self.gate.blocklisted(item):
                        self.gate.block(item)
                        self._log(
                            now_ts, "blocklisted", item, "hype_pump_detector",
                            regime, [_sig(signal)],
                        )

    # ------------------------------------------------------------------ #
    def _try_buy(
        self,
        candidate: MappedCandidate,
        signal: Signal,
        snapshot: dict[str, Item],
        regime: Regime,
        now_ts: float,
    ) -> None:
        name = candidate.market_hash_name
        # Rule provenance rides along on every outcome (Shared §12): which
        # rule fired and how well-supported it was.
        rule_inputs = {
            "mapping_rule": candidate.rule,
            "rule_confidence": candidate.confidence,
            "rule_evidence": candidate.evidence,
        }
        item = snapshot.get(name)
        if item is None:
            return
        confirmation_rule = self._confirm_right_side(name)
        if confirmation_rule != "confirmed":
            self._log(
                now_ts, "buy_refused", name, confirmation_rule, regime,
                [_sig(signal)], inputs=rule_inputs,
            )
            return

        price = item.buff_lowest_sell_cny
        # Request the largest qty the chase cap could allow; the gate shrinks.
        capital_total = self.config.require("capital.total")
        chase_layers = self.config.require("system_a.momentum_chase_max_layers")
        layers_total = self.config.require("position_sizing.layers_total")
        requested = max(1, int(capital_total * chase_layers / layers_total // price))
        result = self.gate.check_buy(
            item, regime, signal, requested, self.backend.get_wallet(), now_ts,
            baseline_volume_24h=self._baseline_volume(name),
        )
        if not result.approved:
            self._log(
                now_ts, "buy_refused", name, result.rule, regime, [_sig(signal)],
                inputs={**rule_inputs, "requested_qty": requested},
            )
            return
        order = Order(self._next_id("buy"), OrderSide.BUY, name, result.qty, price)
        fill = self.backend.place_buy(order)
        if fill:
            self.ledger.record_buy(fill)
            self._log(
                now_ts, "buy_placed", name, candidate.rule, regime, [_sig(signal)],
                inputs={**rule_inputs, "qty": fill.qty, "price": fill.price_cny},
                score=signal.confidence, order_id=order.client_order_id,
            )

    def _baseline_volume(self, name: str) -> float | None:
        """Median 24h volume over the indicator baseline window, excluding the
        latest (possibly event-spiked) snapshot — what exit liquidity will
        look like once the event cools."""
        window = self.config.require("indicators")["volume_baseline_window"]
        history = self.store.series(name)[:-1]
        volumes = sorted(
            i.buff_volume_24h for i in history[-window:]
            if i.buff_volume_24h is not None
        )
        if not volumes:
            return None  # volume unavailable on this feed tier
        return float(volumes[len(volumes) // 2])

    def _confirm_right_side(self, name: str) -> str:
        """§4.1 step 4: volume↑+price↑ (pattern 3), band widening; never
        pattern 4. Returns 'confirmed' or the refusing rule."""
        indicator_config = self.config.require("indicators")
        confirmation = self.config.require("system_a.confirmation")
        history = self.store.series(name)
        window = indicator_config["bollinger_window"]
        if len(history) < window + 1:
            return "insufficient_history"
        state = volume_price_state(
            history,
            baseline_window=indicator_config["volume_baseline_window"],
            flat_price_pct=indicator_config["flat_price_pct"],
            volume_high_ratio=indicator_config["volume_high_ratio"],
            volume_low_ratio=indicator_config["volume_low_ratio"],
        )
        if state is None:
            # Executed volume missing in the window (cs2.sh Developer tier):
            # right-side confirmation genuinely cannot run — refuse rather
            # than fake the pattern from listings (docs Shared §2a).
            return "volume_data_unavailable"
        if confirmation["reject_price_up_volume_down"] and state.pattern == 4:
            return "weak_rally_pattern4"
        if confirmation["require_volume_and_price_up"] and state.pattern != 3:
            return "no_pattern3_confirmation"
        prices = [i.buff_lowest_sell_cny for i in history]
        if confirmation["require_bandwidth_widening"] and not bandwidth_widening(
            prices, window, indicator_config["bollinger_num_std"]
        ):
            return "no_bandwidth_widening"
        return "confirmed"

    # ------------------------------------------------------------------ #
    def _thesis_break_exits(
        self,
        bearish_marks: set[str],
        snapshot: dict[str, Item],
        regime: Regime,
        now_ts: float,
    ) -> None:
        for name in bearish_marks:
            for lot in self.ledger.sellable_lots(name, now_ts):
                self._sell_lot(lot, snapshot, regime, now_ts, "thesis_break_exit")

    def _bracket_exits(
        self, snapshot: dict[str, Item], regime: Regime, now_ts: float
    ) -> None:
        brackets = self.config.require("brackets")
        take_profit = brackets["take_profit_pct"][0]
        stop_cut = brackets["stop_loss_cut_pct"]
        for name in self.tracked_items:
            item = snapshot.get(name)
            if item is None:
                continue
            for lot in self.ledger.sellable_lots(name, now_ts):
                gross_return = item.buff_highest_buy_cny / lot.buy_price - 1
                if gross_return >= take_profit:
                    self._sell_lot(lot, snapshot, regime, now_ts, "take_profit")
                elif gross_return <= stop_cut:
                    self._sell_lot(lot, snapshot, regime, now_ts, "stop_loss")

    def _sell_lot(
        self,
        lot: Lot,
        snapshot: dict[str, Item],
        regime: Regime,
        now_ts: float,
        rule: str,
    ) -> None:
        item = snapshot.get(lot.market_hash_name)
        if item is None:
            return
        # Scale out, don't dump into a thin book (Shared §6.2 spirit): if the
        # book can't take the whole lot this cycle, split and sell the slice
        # the depth cap allows; the remainder exits on later cycles.
        k = self.config.require("position_sizing.volume_relative_k")
        sell_depth = (
            item.buff_volume_24h
            if item.buff_volume_24h is not None
            else item.buff_buy_order_count   # volume-less tier: standing bids
        )
        depth_cap = max(1, int(k * sell_depth))
        if lot.qty > depth_cap:
            lot = self.ledger.split_lot(lot.lot_id, depth_cap)
            self._log(
                now_ts, "sell_scaled_out", lot.market_hash_name,
                f"{rule}.thin_book_split", regime, [],
                inputs={"selling_qty": lot.qty, "depth_cap": depth_cap},
            )
        order = Order(
            self._next_id("sell"), OrderSide.SELL, lot.market_hash_name,
            lot.qty, item.buff_highest_buy_cny,
        )
        fill = self.backend.place_sell(order)
        if fill:
            self.ledger.record_sell(lot.lot_id, fill)
            self._log(
                now_ts, "sell_placed", lot.market_hash_name, rule, regime, [],
                inputs={
                    "qty": fill.qty, "price": fill.price_cny,
                    "buy_price": lot.buy_price, "fee": fill.fee_cny,
                },
                order_id=order.client_order_id,
            )

    # ------------------------------------------------------------------ #
    def _log(
        self, ts, action, item, rule, regime, signals, inputs=None, score=None,
        order_id=None,
    ) -> None:
        self.provenance.record(
            ts=ts, action=action, item=item, rule=rule, regime=regime.value,
            signals=signals, inputs=inputs or {}, score=score, order_id=order_id,
        )


def _sig(signal: Signal) -> dict:
    return {
        "tier": signal.tier, "type": signal.type.value,
        "items": list(signal.items), "direction": signal.direction.value,
        "confidence": signal.confidence, "sources": list(signal.sources),
    }
