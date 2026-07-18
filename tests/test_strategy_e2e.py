"""Strategy end-to-end on a small synthetic market + vendor normalizer tests."""

from datetime import date

import pandas as pd
import pytest

from shared_b.backtest import run_backtest
from shared_b.config import load_config
from shared_b.journal import Journal
from shared_b.schema import Regime
from shared_b.synthetic import generate
from shared_b.vendors.cs2sh import normalize_archive_day, normalize_latest
from system_b.model import forward_log_returns
from system_b.strategy import PositionalStrategy


@pytest.fixture(scope="module")
def bt_result():
    cfg = dict(load_config("b"))
    cfg["model"]["type"] = "ridge"          # fast, deterministic for tests
    market = generate(n_items=25, n_days=380, seed=13)
    strategy = PositionalStrategy(cfg=cfg)
    strategy.set_targets(forward_log_returns(market.panel.frames, 21))
    res = run_backtest(
        panel=market.panel, strategy=strategy, starting_cash=100_000,
        fee_pct=0.015, journal=Journal(None), thesis_lookup=strategy.thesis_for,
    )
    return market, strategy, res


def test_backtest_runs_and_trades(bt_result):
    market, strategy, res = bt_result
    assert len(res.equity_curve) > 250
    assert res.ledger.fills, "no fills at all — entry pipeline is dead"
    # every buy fill happened the day AFTER its decision day
    for f in res.ledger.fills:
        assert (f.fill_day - f.order.day).days >= 1


def test_t7_never_violated(bt_result):
    _, _, res = bt_result
    for lot in res.ledger.lots:
        if lot.sell_day is not None:
            assert lot.sell_day >= lot.unlock_day, "sold inside the T+7 lock"


def test_no_buys_logged_in_bear_cycles(bt_result):
    _, _, res = bt_result
    bear_days = {r["day"] for r in res.journal.records
                 if r["kind"] == "cycle" and r["regime"] == "bear"}
    buys = [r for r in res.journal.records
            if r["kind"] == "decision" and r["action"].startswith("buy_")
            and r["day"] in bear_days]
    assert buys == []


def test_stop_loss_enforced_at_exit(bt_result):
    """Closed losers must have been exited by a rule, and no open unlocked lot
    should be sitting far below its stop at the end."""
    market, _, res = bt_result
    closed_losers = [l for l in res.ledger.lots
                     if not l.open and l.sell_price is not None
                     and l.sell_price / l.buy_price - 1 < -0.05]
    for l in closed_losers:
        assert l.exit_reason != "", "loser closed without a rule attribution"
    last_day = res.equity_curve.index[-1].date()
    view = market.panel.up_to(res.equity_curve.index[-1])
    for lot in res.ledger.unlocked_lots(last_day):
        row = view.today(lot.item)
        if row is None:
            continue
        ret = float(row["buy_price"]) / lot.buy_price - 1
        # -18pt liquidation bracket plus slack for one decision+fill lag
        assert ret > -0.30, f"unlocked lot at {ret:.0%} was never cut"


def test_journal_has_provenance(bt_result):
    _, _, res = bt_result
    decisions = [r for r in res.journal.records if r["kind"] == "decision"
                 and r["action"].startswith("buy_")]
    assert decisions, "no buy decisions journaled"
    for d in decisions:
        assert d["rule"]
        assert "accum" in d.get("signals", {})


def test_max_locked_capital_respected(bt_result):
    _, _, res = bt_result
    # spot check: locked value never exceeded ~70% of equity by much
    for r in res.journal.records:
        if r["kind"] == "cycle" and r["equity"] > 0:
            assert r["locked_value"] / r["equity"] < 0.80


# ---------------------------------------------------------------- cs2.sh map
def test_cs2sh_normalize_latest_maps_documented_fields():
    payload = {
        "items": {
            "AWP | Asiimov (Field-Tested)": {
                "market_hash_name": "AWP | Asiimov (Field-Tested)",
                "buff": {
                    "updated_at": "2026-02-28T18:20:00Z",
                    "collected_at": "2026-02-28T18:36:28.82Z",
                    "ask": 128.31, "ask_volume": 1889,
                    "bid": 125.25, "bid_volume": 90,
                },
            },
            "NULL ITEM": {"buff": None},
        }
    }
    recs = normalize_latest(payload, date(2026, 2, 28), usd_cny=7.1,
                            volume_by_item={"AWP | Asiimov (Field-Tested)": 42})
    assert len(recs) == 1
    r = recs[0]
    assert r.sell_price == pytest.approx(128.31 * 7.1)
    assert r.buy_price == pytest.approx(125.25 * 7.1)
    assert r.listing_count == 1889          # ask_volume = LISTINGS, not trades
    assert r.buy_order_count == 90
    assert r.volume == 42                   # proxy injected separately
    assert r.valid_buy_orders == -1         # unknown from this vendor


def test_cs2sh_normalize_archive_day():
    payload = {
        "items": {
            "X": [{"aggregate": {"ask": 113.78, "ask_volume": 5470, "bid": 112.26,
                                 "bid_volume": 243, "hourly_volume": 5,
                                 "total_supply": 100799, "sample_count": 24}}]
        }
    }
    volumes, supplies = normalize_archive_day(payload)
    assert volumes["X"] == 120              # 5/hour * 24
    assert supplies["X"] == 100799
