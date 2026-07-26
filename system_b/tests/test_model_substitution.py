"""Model-assisted entry: a top-quantile forecast may substitute for ONE
accumulation signal.

Context (measured on the real BUFF panel): the >=2-signals gate empties the
candidate funnel on 64% of days and the survivors never exceed
`max_new_positions_per_cycle`, so the walk-forward ranker only ever re-orders
a list too short to re-order. Its cross-sectional edge cannot reach an order.
These tests pin the widening so it stays bounded and opt-in.
"""

from __future__ import annotations

import pandas as pd
import pytest

from shared_b.backtest import run_backtest
from shared_b.config import load_config
from shared_b.journal import Journal
from shared_b.synthetic import generate
from system_b.model import forward_log_returns
from system_b.strategy import PositionalStrategy


_MARKET = None
_CACHE: dict[tuple, object] = {}


def _market():
    """One synthetic market for the whole module — generation dominates runtime."""
    global _MARKET
    if _MARKET is None:
        _MARKET = generate(n_items=14, n_days=200, seed=13)
    return _MARKET


def _run(cfg: dict):
    """Backtests are pure in (config, market), so memoize — these tests differ
    only by the substitution knobs and would otherwise re-run the same work."""
    sub = cfg["entry"]["model_signal_substitution"]
    key = (sub["enabled"], sub["substitute_min_signals"], sub["model_top_quantile"])
    if key not in _CACHE:
        market = _market()
        strategy = PositionalStrategy(cfg=cfg)
        strategy.set_targets(forward_log_returns(market.panel.frames, 21))
        _CACHE[key] = run_backtest(
            panel=market.panel, strategy=strategy, starting_cash=100_000,
            fee_pct=0.015, journal=Journal(None), thesis_lookup=strategy.thesis_for,
        )
    return _CACHE[key]


def _cfg(enabled: bool, **over) -> dict:
    cfg = dict(load_config("b"))
    cfg["model"] = dict(cfg["model"]); cfg["model"]["type"] = "ridge"
    cfg["entry"] = dict(cfg["entry"])
    sub = {"enabled": enabled, "substitute_min_signals": 1, "model_top_quantile": 0.10}
    sub.update(over)
    cfg["entry"]["model_signal_substitution"] = sub
    return cfg


def test_shipped_config_keeps_substitution_off():
    """It widens the entry gate, so it must never switch on by accident."""
    cfg = dict(load_config("b"))
    sub = cfg.get("entry", {}).get("model_signal_substitution", {})
    assert sub.get("enabled") is False


def test_disabled_admits_nothing():
    res = _run(_cfg(enabled=False))
    admitted = [r for r in res.journal.records
                if r.get("kind") == "decision" and r.get("action") == "model_admitted"]
    assert admitted == []


def test_enabled_never_shrinks_the_candidate_set():
    """Substitution may only ADD candidates — it must not displace items that
    already cleared the full >=2-signal gate."""
    off = _run(_cfg(enabled=False))
    on = _run(_cfg(enabled=True))

    def funnel(res):
        return {r["day"]: r["funnel"] for r in res.journal.records
                if r.get("kind") == "cycle" and r.get("funnel")}

    f_off, f_on = funnel(off), funnel(on)
    shared = set(f_off) & set(f_on)
    assert shared, "no comparable cycles"
    for day in shared:
        assert f_on[day]["candidates"] >= f_off[day]["candidates"], day


def test_admitted_items_still_carry_a_real_market_signal():
    """The forecast buys ONE signal, never the whole gate: an admitted item
    must still show at least `substitute_min_signals` accumulation signal."""
    res = _run(_cfg(enabled=True))
    admitted = [r for r in res.journal.records
                if r.get("kind") == "decision" and r.get("action") == "model_admitted"]
    for r in admitted:
        assert "substitutes_one_signal" in r["rule"]
    # zero-signal items must never be admitted
    zero_sig = [r for r in admitted if r.get("signals", {}).get("accum") == [0, 0, 0]]
    assert zero_sig == []


def test_requiring_all_signals_disables_the_substitution():
    """substitute_min_signals == min_accumulation_signals leaves no gap to
    fill, so nothing can be admitted."""
    cfg = _cfg(enabled=True, substitute_min_signals=int(
        load_config("b").at("entry.min_accumulation_signals", 2)))
    res = _run(cfg)
    admitted = [r for r in res.journal.records
                if r.get("kind") == "decision" and r.get("action") == "model_admitted"]
    assert admitted == []


def test_tighter_quantile_admits_no_more_than_a_looser_one():
    loose = _run(_cfg(enabled=True, model_top_quantile=0.50))
    tight = _run(_cfg(enabled=True, model_top_quantile=0.02))

    def n_admitted(res):
        return sum(1 for r in res.journal.records
                   if r.get("kind") == "decision" and r.get("action") == "model_admitted")

    assert n_admitted(tight) <= n_admitted(loose)


def test_substitution_widens_the_funnel_it_was_built_to_widen():
    """Guards against the change being inert: enabling it must actually admit
    candidates that the >=2-signal gate rejected."""
    off, on = _run(_cfg(enabled=False)), _run(_cfg(enabled=True))

    def totals(res):
        cyc = [r["funnel"] for r in res.journal.records
               if r.get("kind") == "cycle" and r.get("funnel")]
        return sum(f.get("candidates", 0) for f in cyc), sum(
            f.get("model_admitted", 0) for f in cyc)

    cand_off, adm_off = totals(off)
    cand_on, adm_on = totals(on)
    assert adm_off == 0
    assert adm_on > 0, "substitution enabled but admitted nothing — inert"
    assert cand_on > cand_off


def test_substitution_does_not_break_t7_or_fill_lag():
    """The widened gate must not weaken the honesty guarantees."""
    res = _run(_cfg(enabled=True))
    if not res.ledger.fills:
        pytest.skip("no fills on this small market — guarantee covered by "
                    "test_strategy_e2e on the full-size market")
    for f in res.ledger.fills:
        assert (f.fill_day - f.order.day).days >= 1
    for lot in res.ledger.lots:
        if lot.sell_day is not None:
            assert lot.sell_day >= lot.unlock_day
