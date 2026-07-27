"""Forward paper trading on the live feed.

This runs unattended for days, one short process per cycle, so the failure
modes that matter are the ones that only appear across restarts: state that
does not survive, orders that stack, and history that silently corrupts the
features. Each is pinned here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd
import pytest

from system_b.live_paper import (
    HISTORY_SOURCE, LIVE_SOURCE, _anchor_history, first_live_day,
    load_live_panel, load_state, order_from_dict, order_to_dict,
)
from shared_b.schema import Order, Side

ITEMS = ["AK-47 | Redline (Field-Tested)", "AWP | Asiimov (Field-Tested)"]


def _db(tmp_path, hist_price=300.0, live_price=45.0, hist_days=40, live_days=3):
    """Archive at one scale, live feed at another, exactly like production."""
    path = tmp_path / "market.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE snapshots (market_hash_name TEXT, ts REAL, lowest_sell REAL,"
        " highest_buy REAL, listing_count INTEGER, buy_order_count INTEGER,"
        " volume_24h INTEGER, variant TEXT, source TEXT)")
    rows = []
    base = pd.Timestamp("2026-06-01")
    for item in ITEMS:
        for n in range(hist_days):
            ts = (base + pd.Timedelta(days=n)).timestamp()
            rows.append((item, ts, hist_price, 0, 0, 0, 50, None, HISTORY_SOURCE))
        for n in range(live_days):
            ts = (base + pd.Timedelta(days=hist_days + n)).timestamp()
            rows.append((item, ts, live_price, 0, 0, 0, 50, None, LIVE_SOURCE))
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()
    return path


# --------------------------------------------------- the splice must be safe
def test_history_is_anchored_into_live_units():
    """The two Steam series sit on different scales (one CNY, one USD).
    Concatenating raw prints a fake ~85% crash on the join day, which poisons
    every feature and target downstream."""
    g = pd.DataFrame({
        "lowest_sell": [300.0, 300.0, 45.0, 44.0],
        "source": [HISTORY_SOURCE, HISTORY_SOURCE, LIVE_SOURCE, LIVE_SOURCE]})
    out = _anchor_history(g, HISTORY_SOURCE, LIVE_SOURCE)
    assert out.iloc[1] == pytest.approx(45.0), "history must land on live units"
    assert out.iloc[2] == 45.0, "live prices must never be rescaled"


def test_anchoring_removes_the_fake_join_return(tmp_path):
    panel = load_live_panel(_db(tmp_path))
    px = panel.frames[ITEMS[0]]["sell_price"]
    assert px.pct_change().abs().max() < 0.5, "a fake crash survived the join"


def test_anchoring_preserves_shape_because_features_are_scale_invariant():
    """Rescaling is only defensible if it changes no signal. Returns must be
    identical before and after."""
    g = pd.DataFrame({
        "lowest_sell": [100.0, 110.0, 99.0, 45.0],
        "source": [HISTORY_SOURCE] * 3 + [LIVE_SOURCE]})
    raw = g["lowest_sell"].pct_change().dropna().iloc[:2]
    out = _anchor_history(g, HISTORY_SOURCE, LIVE_SOURCE).pct_change().dropna().iloc[:2]
    pd.testing.assert_series_equal(raw, out, check_names=False)


def test_a_live_only_series_is_left_alone(tmp_path):
    g = pd.DataFrame({"lowest_sell": [45.0, 44.0], "source": [LIVE_SOURCE] * 2})
    out = _anchor_history(g, HISTORY_SOURCE, LIVE_SOURCE)
    assert list(out) == [45.0, 44.0]


# ------------------------------------------------------------- forward only
def test_first_live_day_marks_where_trading_may_begin(tmp_path):
    path = _db(tmp_path)
    live = first_live_day(path)
    panel = load_live_panel(path)
    assert live is not None
    assert live > panel.calendar().min(), "history must precede the live window"
    assert live in panel.calendar()


def test_no_live_rows_means_no_live_day(tmp_path):
    path = _db(tmp_path, live_days=0)
    assert first_live_day(path) is None


# ------------------------------------------------------- state across runs
def test_orders_round_trip_through_disk():
    """An order that cannot be rebuilt after a restart is an order that
    silently expires between cycles."""
    o = Order(item=ITEMS[0], side=Side.BUY, qty=3, limit_price=19.5,
              day=date(2026, 7, 27), reason="new_position_batch1")
    back = order_from_dict(order_to_dict(o))
    assert (back.item, back.side, back.qty, back.day) == (o.item, o.side, o.qty, o.day)
    assert back.limit_price == pytest.approx(o.limit_price)
    assert back.client_order_id == o.client_order_id, "id must survive, or the "\
        "broker's idempotency check cannot recognise the order"


def test_missing_state_starts_clean(tmp_path):
    st = load_state(tmp_path / "nope.json")
    assert st["cycles"] == [] and st["ledger"] is None and st["pending"] == []


def test_corrupt_state_does_not_stop_the_run(tmp_path):
    """A dead cron job is a run that produced nothing for days."""
    p = tmp_path / "live_paper.json"
    p.write_text("{ truncated")
    st = load_state(p)
    assert st["cycles"] == [] and st["pending"] == []


def test_state_without_pending_key_is_tolerated(tmp_path):
    """State written by an earlier version must still load."""
    p = tmp_path / "live_paper.json"
    p.write_text(json.dumps({"cycles": [], "ledger": None, "started": None,
                             "fills": []}))
    assert load_state(p)["pending"] == []
