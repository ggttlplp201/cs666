"""Backtest integrity: no-lookahead views, next-day fills, fees, idempotency,
model embargo, GARCH/CUSUM sanity, bus degradation, e2e smoke."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from shared.breaks import cusum_break, jump_break
from shared.data import MarketPanel
from shared.execution import PaperBroker
from shared.garch import fit_garch
from shared.schema import Order, Side
from shared.signal_bus import JsonlBus, NullBus
from shared.synthetic import generate


@pytest.fixture(scope="module")
def market():
    return generate(n_items=12, n_days=320, seed=11)


def test_panel_view_truncates_future(market):
    panel = market.panel
    item = panel.items[0]
    cutoff = panel.frames[item].index[100]
    view = panel.up_to(cutoff)
    h = view.history(item)
    assert h.index.max() <= cutoff
    # cross-section too
    xs = view.cross_section("sell_price", window=10_000)
    assert xs.index.max() <= cutoff


def test_view_today_stale_returns_none(market):
    panel = market.panel
    item = panel.items[0]
    last = panel.frames[item].index[-1]
    stale_view = panel.up_to(last + pd.Timedelta(days=10))
    assert stale_view.today(item) is None


def test_paper_broker_fills_next_day_at_ask_plus_slippage(market):
    panel = market.panel
    item = panel.items[0]
    df = panel.frames[item]
    decision_ts, fill_ts = df.index[50], df.index[51]
    ask = float(df.loc[fill_ts, "sell_price"])
    broker = PaperBroker(panel=panel, slippage_pct=0.005)
    o = Order(item=item, side=Side.BUY, qty=1, limit_price=ask * 1.1,
              day=decision_ts.date())
    broker.place_buy(o)
    fills = broker.settle(fill_ts.date())
    assert len(fills) == 1
    assert fills[0].fill_price == pytest.approx(ask * 1.005)
    assert fills[0].fee == 0.0  # no buyer fee on BUFF


def test_paper_broker_limit_not_reached_expires(market):
    panel = market.panel
    item = panel.items[0]
    df = panel.frames[item]
    fill_ts = df.index[51]
    ask = float(df.loc[fill_ts, "sell_price"])
    broker = PaperBroker(panel=panel)
    o = Order(item=item, side=Side.BUY, qty=1, limit_price=ask * 0.5,
              day=df.index[50].date())
    broker.place_buy(o)
    assert broker.settle(fill_ts.date()) == []
    assert broker.pending == []  # expired, not carried


def test_paper_broker_sell_fee_and_volume_cap(market):
    panel = market.panel
    item = panel.items[0]
    df = panel.frames[item]
    fill_ts = df.index[60]
    bid = float(df.loc[fill_ts, "buy_price"])
    vol = int(df.loc[fill_ts, "volume"])
    broker = PaperBroker(panel=panel, fee_pct=0.015, fill_fraction=0.25)
    o = Order(item=item, side=Side.SELL, qty=10_000, limit_price=bid * 0.5,
              day=df.index[59].date(), lot_id="L")
    broker.place_sell(o)
    fills = broker.settle(fill_ts.date())
    assert len(fills) == 1
    f = fills[0]
    assert f.qty <= max(int(vol * 0.25), 1)      # cannot dump into thin book
    assert f.fee == pytest.approx(f.qty * f.fill_price * 0.015)


def test_paper_broker_idempotent_order_ids(market):
    panel = market.panel
    item = panel.items[0]
    df = panel.frames[item]
    broker = PaperBroker(panel=panel)
    o = Order(item=item, side=Side.BUY, qty=1,
              limit_price=float(df.iloc[51]["sell_price"]) * 2, day=df.index[50].date())
    broker.place_buy(o)
    broker.place_buy(o)  # same client_order_id resubmitted
    assert len(broker.settle(df.index[51].date())) == 1


def test_walk_forward_embargo():
    """Ranker must never train on targets whose window overlaps 'today'."""
    from system_b.model import WalkForwardRanker

    idx = pd.date_range("2025-01-01", periods=120, freq="D")
    feats = pd.DataFrame({c: np.random.default_rng(0).normal(size=120) for c in ["f1"]},
                         index=[f"i{k}" for k in range(120)])
    ranker = WalkForwardRanker(model_type="ridge", horizon=21, refit_every=1,
                               min_train_rows=1, feature_cols=["f1"])
    for k, day in enumerate(idx):
        row = pd.DataFrame({"f1": [float(k)]}, index=[f"i{k}"])
        ranker.observe(day, row)
    targets = pd.DataFrame(np.ones((120, 120)), index=idx,
                           columns=[f"i{k}" for k in range(120)])
    day = idx[-1]
    X, y = ranker._training_matrix(day, targets)
    # last usable feature day must be <= day - horizon - 1
    assert X["f1"].max() <= float(120 - 1 - 22)


def test_garch_recovers_persistence():
    rng = np.random.default_rng(5)
    n = 1500
    omega, alpha, beta = 1e-5, 0.1, 0.85
    r = np.zeros(n)
    s2 = omega / (1 - alpha - beta)
    for t in range(1, n):
        s2 = omega + alpha * r[t - 1] ** 2 + beta * s2
        r[t] = np.sqrt(s2) * rng.standard_normal()
    fit = fit_garch(r)
    assert fit is not None
    assert 0.75 <= fit.persistence <= 1.0
    assert fit.forecast_sigma(1) > 0


def test_cusum_and_jump_detectors():
    rng = np.random.default_rng(6)
    calm = rng.normal(0, 0.01, 200)
    assert not cusum_break(calm).fired
    shifted = np.concatenate([calm, rng.normal(0.03, 0.01, 10)])  # 3-sigma drift
    assert cusum_break(shifted).fired
    jumped = np.concatenate([calm, [0.5]])
    assert jump_break(jumped, sigma=0.01).fired
    assert not jump_break(calm, sigma=0.01).fired


def test_bus_degrades_gracefully(tmp_path):
    assert NullBus().read() == []
    bus = JsonlBus(tmp_path / "missing.jsonl")
    assert bus.read() == []          # missing file -> empty, no crash
    p = tmp_path / "bus.jsonl"
    p.write_text('{"tier": 2, "type": garbage\n')
    assert JsonlBus(p).read() == []  # corrupt line skipped


def test_synthetic_panel_roundtrip(tmp_path, market):
    market.panel.save(tmp_path / "panel")
    loaded = MarketPanel.load(tmp_path / "panel")
    assert set(loaded.items) == set(market.panel.items)
    item = market.panel.items[0]
    pd.testing.assert_frame_equal(
        loaded.frames[item], market.panel.frames[item], check_freq=False)
    assert loaded.meta[item].category == market.panel.meta[item].category
