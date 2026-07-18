"""System B positional strategy — the daily decision cycle (System B §3-§8).

Order of operations each cycle (left-side, CD-gated, regime-first):
 1. data sanity -> pause on stale feed
 2. regime classification -> deployment ceiling + behavior bias
 3. exits FIRST (brackets, thesis breaks, distribution shape, bear cuts)
 4. score the universe -> hard filters -> composite + model rank
 5. entry rule: high composite + >=2 accumulation signals (+ early attention)
 6. staged builds: batch 1 new items; adds only at -10% support after CD
 7. risk gate approves/shrinks; journal every decision with provenance
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from shared.backtest import exit_side_prices
from shared.breaks import cusum_break
from shared.data import PanelView
from shared.journal import Journal
from shared.ledger import Ledger
from shared.regime import RegimeReading, classify_regime, market_index_returns
from shared.schema import Order, Regime, Side
from shared.signal_bus import NullBus, SignalBus, confirmed_events_for_item

from .features import VolCache, build_feature_frame
from .filters import apply_hard_filters
from .model import WalkForwardRanker, forward_log_returns
from .risk import CycleReservations, RiskGate, RiskState


@dataclass
class PositionalStrategy:
    cfg: dict
    bus: SignalBus = field(default_factory=NullBus)
    ranker: WalkForwardRanker | None = None
    vol_cache: VolCache = field(default_factory=VolCache)
    risk: RiskGate | None = None
    # targets for walk-forward refits (backtest injects the full frame lazily;
    # live mode recomputes from stored history)
    _targets: pd.DataFrame | None = None
    theses: dict[str, tuple[str, str]] = field(default_factory=dict)  # order_id -> (thesis, invalidation)
    last_regime: RegimeReading | None = None
    last_features: pd.DataFrame | None = None
    predictions: dict[pd.Timestamp, pd.Series] = field(default_factory=dict)
    # items with a working order from the previous cycle (fills at t+1) — do not
    # re-order until it settles/expires, else consecutive-day batch stacking
    last_order_day: dict[str, date] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.risk is None:
            self.risk = RiskGate(self.cfg, RiskState())
        if self.ranker is None:
            m = self.cfg.get("model", {})
            self.ranker = WalkForwardRanker(
                model_type=str(m.get("type", "xgboost")),
                horizon=int(m.get("horizon_days", 21)),
                refit_every=int(m.get("refit_every_days", 30)),
                min_train_rows=int(m.get("min_train_rows", 500)),
            )

    # ------------------------------------------------------------------ cycle
    def on_cycle(self, view: PanelView, ledger: Ledger, journal: Journal) -> list[Order]:
        day = view.day.date()
        orders: list[Order] = []

        # 1) data sanity: enough live items or stand down (Shared §8.4)
        active = view.active_items()
        if len(active) < max(3, len(view.items) // 10):
            journal.log("pause", day=day, reason="stale_or_thin_feed", active=len(active))
            return orders

        # 2) regime
        regime = classify_regime(view, **self.cfg.get("regime_params", {}))
        self.last_regime = regime

        # bus signals as of the decision day (a replayed cycle must not see
        # signals first seen later); graceful degradation: NullBus -> empty
        as_of = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        bus_signals = self.bus.read(tiers=(2, 3), as_of=as_of)

        # 3) features for the full universe
        features = build_feature_frame(view, regime, bus_signals, self.vol_cache, self.cfg)
        self.last_features = features
        if features.empty:
            return orders

        # marks cover EVERY item incl. stale/delisted ones (last observed bid) —
        # marking held-but-stale items from the fresh-features frame alone would
        # value them at cost and hide losses from equity and the loss limits
        marks = exit_side_prices(view)
        marks.update({i: float(features.loc[i, "bid"]) for i in features.index})
        fee = float(self.cfg.get("costs", {}).get("buff_fee_pct", 0.015))
        equity = ledger.equity(marks, fee)
        halted = self.risk.trading_halted(day, equity)
        self.risk.record_equity(day, equity)
        self.risk.set_categories({i: (view.meta[i].category if i in view.meta else "other")
                                  for i in features.index})

        # 4) exits first
        orders += self._exits(view, ledger, journal, features, regime, bus_signals, day)

        # 5) structural-break alarm (Shared §12 / RESEARCH_INDEX #4): a market-
        # wide break pauses positional entries this cycle and triggers an
        # off-cycle model re-fit — don't trust factor weights across a break
        break_fired = False
        idx_rets = market_index_returns(view)
        if len(idx_rets) > 80:
            alarm = cusum_break(idx_rets.to_numpy())
            if alarm.fired:
                break_fired = True
                journal.log("break_alarm", day=day, stat=alarm.stat,
                            direction=alarm.direction)
                self.ranker._last_fit = None  # force refit at next opportunity

        # 6) model bookkeeping: walk-forward observe/refit/predict
        self.ranker.observe(view.day, features)
        if self._targets is not None:
            self.ranker.maybe_refit(view.day, self._targets)
        pred = self.ranker.predict(features)
        if pred is not None:
            self.predictions[view.day] = pred

        # 7) entries (skip entirely when halted, bear, or mid-break)
        self._funnel = {}
        if not halted and not break_fired and regime.regime != Regime.BEAR:
            orders += self._entries(view, ledger, journal, features, pred, regime, day,
                                    marks, equity, bus_signals)
        elif halted:
            journal.log("halt", day=day, reasons=halted)

        journal.cycle(
            day=day, regime=regime.regime.value, equity=equity, cash=ledger.cash,
            deployed_pct=ledger.marked_value(marks) * (1 - fee) / equity if equity > 0 else 0,
            locked_value=ledger.locked_value(day, marks),
            extra={"breadth": regime.breadth, "n_scoreable": len(features),
                   "funnel": self._funnel},
        )
        return orders

    # ----------------------------------------------------------------- exits
    def _exits(
        self,
        view: PanelView,
        ledger: Ledger,
        journal: Journal,
        features: pd.DataFrame,
        regime: RegimeReading,
        bus_signals: list,
        day: date,
    ) -> list[Order]:
        cfg = self.cfg
        br = cfg.get("brackets", {})
        tp_lo, tp_hi = br.get("take_profit_pct", [0.10, 0.15])
        sl_cut = br.get("stop_loss_cut_pct", -0.10)
        sl_liq = br.get("stop_loss_liquidate_pct", -0.18)
        k = float(cfg.get("position_sizing", {}).get("volume_relative_k", 0.35))
        orders: list[Order] = []

        for item in ledger.held_items():
            row = view.today(item)
            if row is None:
                continue
            bid = float(row["buy_price"])
            ask = float(row["sell_price"])
            feat = features.loc[item] if item in features.index else None
            adv = float(feat["volume_avg_20"]) if feat is not None else float(row["volume"])
            # scale out, don't dump: cap daily sell qty by book depth
            day_sell_cap = max(int(k * max(adv, 1.0)), 1)
            meta = view.meta.get(item)
            events = confirmed_events_for_item(
                bus_signals, item, meta.collection if meta else "")
            bearish_event = any(e.direction == "bearish" and e.confidence >= 0.6 for e in events)

            sold_today = 0
            for lot in ledger.unlocked_lots(day, item):
                if sold_today >= day_sell_cap:
                    break
                # Bracket triggers are measured ASK-side (same side we bought on;
                # the crash-course +10-15%/-10% rules are chart/list prices).
                # Measuring at the bid would start every lot ~spread+slippage
                # (~3%) closer to its stop and further from its TP — stops then
                # fire on noise and TPs never trigger. Execution still crosses
                # to the bid; the spread is paid once, not double-counted in
                # the trigger.
                ret = ask / lot.buy_price - 1.0
                reason = None
                sell_qty = lot.qty

                if ret <= sl_liq:
                    reason = "stop_unconditional_liquidation"
                elif ret <= sl_cut:
                    reason = "stop_loss_cut"
                elif bearish_event:
                    reason = "thesis_break_confirmed_event"
                elif ret >= tp_hi:
                    reason = "take_profit_full"
                elif ret >= tp_lo:
                    reason = "take_profit_trim"
                    sell_qty = max(lot.qty // 2, 1)
                elif feat is not None and feat["bb_touch"] == -1 and ret > 0.05:
                    reason = "upper_band_green_bar_trim"
                    sell_qty = max(lot.qty // 2, 1)
                elif (
                    feat is not None
                    and feat["vp_shape_score"] <= -0.7
                    and feat["vp_price_vs_zone"] < 0
                    and ret < 0
                ):
                    reason = "distribution_shape_exit"
                elif regime.regime == Regime.BEAR and ret < -0.05:
                    reason = "bear_regime_cut"

                if reason is None:
                    continue
                sell_qty = min(sell_qty, day_sell_cap - sold_today)
                if sell_qty <= 0:
                    continue
                # stops must FILL in a falling market ("cut immediately, no
                # hoping") — price them aggressively below the bid; ordinary
                # exits rest just under it
                urgency = 0.05 if reason.startswith(("stop", "bear")) else 0.005
                o = Order(
                    item=item, side=Side.SELL, qty=sell_qty,
                    limit_price=bid * (1 - urgency),
                    day=day, reason=reason, lot_id=lot.lot_id,
                )
                if reason.startswith("stop"):
                    self.risk.state.record_stop(item, day)
                # sells carry the same provenance as buys (Shared §12): the
                # feature snapshot that triggered the rule
                sig_snapshot = None
                if feat is not None:
                    sig_snapshot = {
                        "accum": [int(feat["accum_s1"]), int(feat["accum_s2"]), int(feat["accum_s3"])],
                        "vp_shape_score": float(feat["vp_shape_score"]),
                        "pct_b": float(feat["pct_b"]),
                        "bb_touch": int(feat["bb_touch"]),
                    }
                journal.decision(
                    day=day, item=item, action=f"sell_{sell_qty}", rule=reason,
                    regime=regime.regime.value, score=None, signals=sig_snapshot,
                    detail={"lot": lot.lot_id, "ret_at_ask": ret, "buy_price": lot.buy_price},
                )
                orders.append(o)
                sold_today += sell_qty
        return orders

    # --------------------------------------------------------------- entries
    def _entries(
        self,
        view: PanelView,
        ledger: Ledger,
        journal: Journal,
        features: pd.DataFrame,
        pred: pd.Series | None,
        regime: RegimeReading,
        day: date,
        marks: dict[str, float],
        equity: float,
        bus_signals: list | None = None,
    ) -> list[Order]:
        cfg = self.cfg
        sel = cfg.get("selection_filters", {})
        rc = cfg.get("risk_controls", {})
        entry_cfg = cfg.get("entry", {})
        staged = cfg.get("staged_entry", {})
        blocklist = set(rc.get("blocklist", []) or [])
        allowlist = set(rc.get("allowlist", []) or [])

        passing, rejected = apply_hard_filters(
            features, dict(view.meta), sel, blocklist, allowlist)
        for item, reasons in rejected.items():
            if item in ledger.held_items():
                journal.decision(day=day, item=item, action="hold_no_add",
                                 rule=";".join(reasons), regime=regime.regime.value)
        self._funnel = {"scoreable": len(features), "pass_filters": len(passing)}
        if passing.empty:
            return []

        # composite threshold + accumulation-signal entry rule (§3.2).
        # Gate on the STRUCTURAL composite: hard filters + structure say WHAT
        # is buyable; accumulation signals say WHEN (flat-price phases would
        # never clear a momentum-weighted floor).
        min_sig = int(entry_cfg.get("min_accumulation_signals", 2))
        top_pct = float(entry_cfg.get("composite_top_pct", 0.50))
        comp_floor = passing["structural_composite"].quantile(1 - top_pct)
        use_attention = bool(entry_cfg.get("attention_feature_from_bus", True))

        # The >=2-signals gate is HARD: Tier-3 attention AUGMENTS ranking
        # (System B §7 — a stronger entry), it never substitutes for a
        # market-data signal.
        cand = passing[passing["structural_composite"] >= comp_floor]
        cand = cand[cand["accum_count"] >= min_sig]
        self._funnel["above_floor"] = int((passing["structural_composite"] >= comp_floor).sum())
        self._funnel["accum_ge2"] = int((passing["accum_count"] >= min_sig).sum())
        self._funnel["candidates"] = len(cand)
        if cand.empty:
            return []

        # Tier-2 risk overlay (System B §7): a confirmed event touching an item
        # or its collection pauses NEW BUYS in it — never chase the event
        if bus_signals:
            paused: list[str] = []
            for item in cand.index:
                meta = view.meta.get(str(item))
                events = confirmed_events_for_item(
                    bus_signals, str(item), meta.collection if meta else "")
                if any(e.confidence >= 0.6 for e in events):
                    paused.append(str(item))
                    journal.decision(day=day, item=str(item), action="pause_buys",
                                     rule="tier2_confirmed_event",
                                     regime=regime.regime.value)
            if paused:
                cand = cand[~cand.index.isin(paused)]
                if cand.empty:
                    return []

        # rank: blend model prediction (when trained) with the composite;
        # rising-but-early attention adds a bounded rank BONUS
        blend = float(cfg.get("model", {}).get("rank_blend_model_weight", 0.5))
        if pred is not None:
            model_rank = pred.reindex(cand.index).rank(pct=True).fillna(0.5)
            rank = blend * model_rank + (1 - blend) * cand["composite"].rank(pct=True)
        else:
            rank = cand["composite"].rank(pct=True)
        if use_attention:
            rank = rank + 0.1 * cand["attention_early"].clip(0, 2)
        cand = cand.assign(final_rank=rank).sort_values("final_rank", ascending=False)

        max_new = int(entry_cfg.get("max_new_positions_per_cycle", 3))
        batches = int(staged.get("batches_per_item", 4))
        add_support = float(staged.get("add_only_at_support_pct", -0.10))
        discount = float(entry_cfg.get("entry_limit_discount_pct", 0.005))
        total_capital = float(cfg.get("capital", {}).get("total", equity))

        orders: list[Order] = []
        new_positions = 0
        reserved = CycleReservations()  # same-cycle approvals claim cash/caps
        for item, feat in cand.iterrows():
            item = str(item)
            last_od = self.last_order_day.get(item)
            if last_od is not None and (day - last_od).days <= 1:
                continue  # working order still in flight; wait for its settle
            meta = view.meta.get(item)
            category = meta.category if meta else "other"
            held = ledger.position_qty(item) > 0
            price = float(feat["price"])

            if held:
                # --- staged add: only at support, only after prior batch CD ---
                # ladder: add k requires -10%*k from first entry (Shared §6.2 /
                # the crash-course §9 tiered buy levels, e.g. 1750/1550/1100)
                first = ledger.first_entry_price(item)
                last_batch = ledger.last_batch(item)
                n_batches = len(ledger.open_lots(item))
                if n_batches >= batches:
                    continue
                if price > first * (1 + add_support * n_batches):
                    continue  # not at the next support step
                if last_batch is not None and last_batch.locked(day):
                    journal.decision(day=day, item=item, action="skip_add",
                                     rule="cd_not_cleared", regime=regime.regime.value,
                                     score=float(feat["final_rank"]))
                    continue
                batch_index = n_batches
                rule = "staged_add_at_support"
            else:
                if new_positions >= max_new:
                    continue
                batch_index = 0
                rule = "new_position_batch1"

            alloc = self.risk.item_allocation(category, total_capital)
            batch_value = alloc / batches
            limit = price * (1 - discount)  # left-side: resting order slightly below ask
            qty = max(int(batch_value // limit), 0)
            if qty <= 0:
                continue
            order = Order(item=item, side=Side.BUY, qty=qty, limit_price=limit,
                          day=day, reason=rule, batch_index=batch_index)

            decision = self.risk.check_buy(
                order, day=day, regime=regime.regime, category=category,
                equity=equity, marks=marks, ledger=ledger,
                avg_daily_volume=float(feat["volume_avg_20"]),
                garch_vol=float(feat["garch_vol"]), is_add=held,
                halted=[], reserved=reserved,
            )
            journal.decision(
                day=day, item=item,
                action=(f"buy_{decision.qty}" if decision.approved else "veto_buy"),
                rule=rule + ("" if decision.approved else ":" + ";".join(decision.reasons)),
                regime=regime.regime.value, score=float(feat["final_rank"]),
                signals={
                    "accum": [int(feat["accum_s1"]), int(feat["accum_s2"]), int(feat["accum_s3"])],
                    "composite": float(feat["composite"]),
                    "attention_early": float(feat["attention_early"]),
                    "risk_adjustments": decision.reasons,
                },
                detail={"limit": limit, "batch": batch_index},
            )
            if not decision.approved:
                continue
            order.qty = decision.qty
            thesis = (
                f"{rule}|composite={feat['composite']:.2f},signals="
                f"{int(feat['accum_count'])},regime={regime.regime.value}"
            )
            invalidation = "confirmed bearish event on item/collection; or stop at -10%"
            self.theses[order.client_order_id] = (thesis, invalidation)
            self.risk.state.record_entry(item, day)
            self.last_order_day[item] = day
            orders.append(order)
            if not held:
                new_positions += 1
        return orders

    # used by the backtester to attach theses to fills
    def thesis_for(self, order: Order) -> tuple[str, str]:
        return self.theses.get(order.client_order_id, ("", ""))

    def set_targets(self, targets: pd.DataFrame) -> None:
        """Backtest wiring: forward-return targets for walk-forward refits.
        The ranker's embargo ensures only fully-realized windows are used."""
        self._targets = targets
