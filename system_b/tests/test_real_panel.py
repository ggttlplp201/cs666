"""Real-panel builder: the book/volume join and its no-lookahead guarantees."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from shared_b.real_panel import (
    BOOK_SOURCE,
    VOLUME_SOURCE,
    build_panel,
    contiguous_blocks,
    derive_meta,
)

ITEMS = ["AK-47 | Redline (Field-Tested)", "AWP | Asiimov (Field-Tested)",
         "MP9 | Starlight Protector (Field-Tested)"]


def _ts(d: date) -> float:
    return pd.Timestamp(d).timestamp()


@pytest.fixture()
def db(tmp_path):
    """30 daily Steam volume rows per item; BUFF book only on even days."""
    path = tmp_path / "market.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE snapshots (market_hash_name TEXT, ts REAL, lowest_sell REAL,"
        " highest_buy REAL, listing_count INTEGER, buy_order_count INTEGER,"
        " volume_24h INTEGER, variant TEXT, source TEXT)"
    )
    start = date(2025, 1, 1)
    rows = []
    for i, item in enumerate(ITEMS):
        for n in range(30):
            d = start + timedelta(days=n)
            rows.append((item, _ts(d), 0.0, 0.0, 0, 0, 100 + n, None, VOLUME_SOURCE))
            if n % 2 == 0:  # book is sparser than volume
                rows.append((item, _ts(d), 100.0 + i + n, 95.0 + i + n, 50, 40, None,
                             None, BOOK_SOURCE))
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_contiguous_blocks_splits_on_gaps():
    days = pd.DatetimeIndex(
        ["2025-01-01", "2025-01-02", "2025-01-03", "2025-03-01", "2025-03-02"]
    )
    blocks = contiguous_blocks(days)
    assert [n for _, _, n in blocks] == [3, 2]


def test_contiguous_blocks_empty():
    assert contiguous_blocks(pd.DatetimeIndex([])) == []


def test_max_gap_days_keeps_a_sparse_feed_as_one_block():
    """An every-other-day archive is one window, not N one-day windows —
    strict contiguity would collapse the usable history to a single day."""
    days = pd.DatetimeIndex(["2025-01-01", "2025-01-03", "2025-01-05", "2025-03-01"])
    assert [n for _, _, n in contiguous_blocks(days, max_gap_days=1)] == [1, 1, 1, 1]
    assert [n for _, _, n in contiguous_blocks(days, max_gap_days=7)] == [3, 1]


def test_max_gap_days_does_not_bridge_long_holes():
    """The real archive's multi-month holes must stay separate windows."""
    days = pd.DatetimeIndex(["2025-01-01", "2025-01-02", "2026-01-01"])
    assert [n for _, _, n in contiguous_blocks(days, max_gap_days=7)] == [2, 1]


def test_join_takes_book_from_buff_and_volume_from_steam(db):
    panel, stats = build_panel(db, max_stale_days=3, min_items_per_day=1)
    assert stats["book_source"] == BOOK_SOURCE
    assert stats["volume_source"] == VOLUME_SOURCE
    assert set(panel.items) == set(ITEMS)

    df = panel.frames[ITEMS[0]]
    # book columns come from the BUFF rows, never the zero-filled Steam ones
    assert (df["listing_count"] == 50).all()
    assert (df["buy_order_count"] == 40).all()
    assert (df["sell_price"] > df["buy_price"]).all()
    # volume is real Steam executed trades
    assert (df["volume"] >= 100).all()


def test_valid_buy_orders_is_unknown_sentinel(db):
    """Bids *near market* aren't recoverable from stored depth — must be -1 so
    the hard filter falls back to buy_order_count rather than silently passing."""
    panel, _ = build_panel(db, max_stale_days=3, min_items_per_day=1)
    for df in panel.frames.values():
        assert (df["valid_buy_orders"] == -1).all()


def test_zero_stale_days_disables_carry(db):
    """Observations-only mode must keep exactly the days the book was seen."""
    panel, stats = build_panel(db, max_stale_days=0, min_items_per_day=1)
    assert stats["filled_rows"] == 0
    assert stats["fill_rate"] == 0.0
    for df in panel.frames.values():
        # book existed only on even days of the window
        assert all(d.day % 2 == 1 for d in df.index)


def test_carry_is_bounded_by_max_stale_days(db):
    """A wider carry may add rows, but never more than the limit allows."""
    tight, s_tight = build_panel(db, max_stale_days=1, min_items_per_day=1)
    assert s_tight["filled_rows"] > 0
    for df in tight.frames.values():
        gaps = pd.Series(df.index).diff().dt.days.dropna()
        assert (gaps <= 1).all()


def test_no_book_data_before_first_observation(db):
    """Carrying must never invent a book earlier than the first real one."""
    panel, _ = build_panel(db, max_stale_days=5, min_items_per_day=1)
    with sqlite3.connect(db) as con:
        first = pd.read_sql(
            "SELECT MIN(ts) t FROM snapshots WHERE source = ?", con, params=(BOOK_SOURCE,)
        )["t"][0]
    first_day = pd.Timestamp(first, unit="s").floor("D")
    for df in panel.frames.values():
        assert df.index.min() >= first_day


def test_thin_cross_sections_are_dropped(db):
    """A day the ranker can't rank across is removed, not silently kept."""
    panel, _ = build_panel(db, max_stale_days=0, min_items_per_day=len(ITEMS) + 1)
    assert panel.frames == {}


def test_book_day_label_uses_the_source_calendar(tmp_path):
    """iflow filenames are UTC+8; a 04:00 Beijing snapshot is 20:00 UTC the day
    BEFORE. Labelling it in UTC would date the row a day early — putting
    tomorrow's BUFF book on today's row, which leaks the future into a
    backtest. 99% of the real archive sits in that window."""
    path = tmp_path / "market.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE snapshots (market_hash_name TEXT, ts REAL, lowest_sell REAL,"
        " highest_buy REAL, listing_count INTEGER, buy_order_count INTEGER,"
        " volume_24h INTEGER, variant TEXT, source TEXT)"
    )
    item = ITEMS[0]
    rows = []
    for n in range(4):
        beijing = pd.Timestamp("2026-03-19", tz="Asia/Shanghai") + pd.Timedelta(days=n, hours=4)
        rows.append((item, beijing.timestamp(), 100.0, 95.0, 50, 40, None, None, BOOK_SOURCE))
        utc_day = pd.Timestamp("2026-03-19", tz="UTC") + pd.Timedelta(days=n)
        rows.append((item, utc_day.timestamp(), 0.0, 0.0, 0, 0, 200, None, VOLUME_SOURCE))
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    panel, _ = build_panel(path, max_stale_days=0, min_items_per_day=1)
    days = panel.frames[item].index
    # the 04:00 Beijing snapshot of Mar 19 must be dated Mar 19, not Mar 18
    assert pd.Timestamp("2026-03-19") in days
    assert pd.Timestamp("2026-03-18") not in days


def test_zero_bid_rows_are_dropped(db):
    """highest_buy == 0 is the old iflow schema's 'bid unavailable' sentinel,
    not a real bid — marking or exiting there is meaningless, and the row
    would still clear the depth gate via buy_order_count."""
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE snapshots SET highest_buy = 0.0 WHERE source = ? AND market_hash_name = ?",
            (BOOK_SOURCE, ITEMS[0]),
        )
    panel, stats = build_panel(db, max_stale_days=0, min_items_per_day=1)
    assert ITEMS[0] not in panel.frames
    assert stats["invalid_book_rows_dropped"] > 0
    for df in panel.frames.values():
        assert (df["buy_price"] > 0).all()


def test_crossed_book_rows_are_dropped(db):
    """bid > ask cannot exist at one instant on a real venue — the sides were
    sampled at different times. Buying the ask and selling the bid on such a
    row is a risk-free profit that does not exist. 6.8% of the real archive's
    rows in the usable window are crossed."""
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE snapshots SET highest_buy = lowest_sell + 10 WHERE source = ?"
            " AND market_hash_name = ?",
            (BOOK_SOURCE, ITEMS[1]),
        )
    panel, stats = build_panel(db, max_stale_days=0, min_items_per_day=1)
    assert ITEMS[1] not in panel.frames
    assert stats["invalid_book_rows_dropped"] > 0
    for df in panel.frames.values():
        assert (df["buy_price"] <= df["sell_price"]).all()


def test_stats_describe_the_saved_panel(db):
    """Fill rate must be measured after thin-day pruning — counting dropped
    rows would overstate how much of the saved panel is real."""
    panel, stats = build_panel(db, max_stale_days=3, min_items_per_day=1)
    assert stats["rows"] == sum(len(df) for df in panel.frames.values())
    assert stats["observed_rows"] + stats["filled_rows"] == stats["rows"]


def test_derive_meta_marks_primaries():
    meta = derive_meta(ITEMS)
    assert meta[ITEMS[0]].is_primary is True         # AK-47
    assert meta[ITEMS[1]].is_primary is True         # AWP
    assert meta[ITEMS[2]].is_primary is False        # MP9
    # structural factors stay unknown — the allowlist, not fake data, covers them
    for m in meta.values():
        assert m.supply == 0 and m.case_price_cny == 0.0
        assert m.aesthetics == 0.5


def test_panel_round_trips_through_disk(db, tmp_path):
    panel, _ = build_panel(db, max_stale_days=3, min_items_per_day=1)
    out = tmp_path / "panel"
    panel.save(out)
    from shared_b.data import MarketPanel

    again = MarketPanel.load(out)
    assert set(again.items) == set(panel.items)
    pd.testing.assert_frame_equal(
        again.frames[ITEMS[0]], panel.frames[ITEMS[0]],
        check_dtype=False, check_freq=False,
    )
