"""shared_b.vendors.iflow_archive — archive records → System B panel (no network)."""

import json
import zipfile

import pandas as pd
import pytest

from shared_b.data import MarketPanel, PANEL_COLUMNS
from shared_b.schema import ItemMeta
from shared_b.vendors.iflow_archive import (
    build_panel,
    coverage_report,
    ingest_snapshot,
    parse_new_record,
)

ITEM = "M4A1-S | Mecha Industries (Minimal Wear)"


def new_record(ask=375.0, bid=370.0, bid_orders=None, steam_vol=8, name=ITEM):
    return {
        "appid": 730,
        "hash_name": name,
        "buff_sell": {"price": ask, "orders": [ask, ask + 5] if ask else [], "count": 91},
        "buff_buy": {"price": bid, "orders": bid_orders or [bid], "count": 87},
        "steam_volume": {"volume": steam_vol, "median_price": 527.59},
    }


def old_record(name=ITEM):
    return {
        "appid": 730,
        "hash_name": name,
        "buff_sell_list": [[183, 513.0, 1.0], [185, 170.0, 1.0]],
        "buff_sell_num": 637,
        "buff_buy_num": 39,
        "buy_order_list": [[260.52, 1]],   # STEAM bids — never BUFF
    }


# ---------------------------------------------------------------- parse

def test_parse_new_record_maps_fields():
    row = parse_new_record(new_record())
    assert row == {
        "sell_price": 375.0,
        "buy_price": 370.0,
        "listing_count": 91,
        "buy_order_count": 87,
        "volume": 8,
        "valid_buy_orders": 1,
    }


def test_valid_buy_orders_band_excludes_lowballs():
    # ask 100, 5% band -> valid iff bid ladder entry >= 95
    ladder = [99.0, 96.0, 95.0, 94.9, 60.0, 1.0]
    row = parse_new_record(new_record(ask=100.0, bid=99.0, bid_orders=ladder))
    assert row["valid_buy_orders"] == 3


def test_parse_rejects_missing_ask_or_bid():
    assert parse_new_record(new_record(ask=None)) is None      # 2026-05 Mecha case
    assert parse_new_record(new_record(bid=None)) is None
    assert parse_new_record(new_record(ask=0.0)) is None


def test_parse_rejects_old_schema():
    # OLD era has no BUFF bid — must not fabricate one
    assert parse_new_record(old_record()) is None


def test_missing_steam_volume_degrades_to_zero():
    rec = new_record()
    del rec["steam_volume"]
    assert parse_new_record(rec)["volume"] == 0   # 0 = unknown -> no fills


# ---------------------------------------------------------------- ingest/merge

def make_zip(tmp_path, fname, records):
    p = tmp_path / fname
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("dump.jsonl", "\n".join(json.dumps(r) for r in records))
    return p


def test_later_snapshot_wins_same_day(tmp_path):
    rows = {}
    z1 = make_zip(tmp_path, "2024-06-01-00-15.zip", [new_record(ask=375.0)])
    z2 = make_zip(tmp_path, "2024-06-01-12-15.zip", [new_record(ask=380.0)])
    ingest_snapshot(z1, {ITEM}, rows, 0.05)
    ingest_snapshot(z2, {ITEM}, rows, 0.05)
    assert len(rows) == 1
    assert next(iter(rows.values()))["sell_price"] == 380.0


def test_unparseable_later_snapshot_keeps_earlier(tmp_path):
    rows = {}
    ingest_snapshot(make_zip(tmp_path, "2024-06-01-00-15.zip", [new_record(ask=375.0)]),
                    {ITEM}, rows, 0.05)
    ingest_snapshot(make_zip(tmp_path, "2024-06-01-12-15.zip", [new_record(ask=None)]),
                    {ITEM}, rows, 0.05)
    assert next(iter(rows.values()))["sell_price"] == 375.0


def test_ingest_filters_universe_and_appid(tmp_path):
    rows = {}
    recs = [new_record(), new_record(name="AK-47 | Redline (Field-Tested)"),
            {**new_record(), "appid": 570}]
    n = ingest_snapshot(make_zip(tmp_path, "2024-06-02-00-15.zip", recs),
                        {ITEM}, rows, 0.05)
    # day = CN calendar day from the filename (venue-local, not UTC)
    assert n == 1 and list(rows) == [(ITEM, pd.Timestamp("2024-06-02").date())]


# ---------------------------------------------------------------- panel out

def test_build_save_load_roundtrip(tmp_path):
    rows = {}
    for fname, ask in [("2024-06-01-12-15.zip", 375.0), ("2024-06-02-12-15.zip", 380.0),
                       ("2024-06-03-12-15.zip", 377.0)]:
        ingest_snapshot(make_zip(tmp_path, fname, [new_record(ask=ask)]), {ITEM}, rows, 0.05)
    meta = {ITEM: ItemMeta(market_hash_name=ITEM, supply=18000, case_price_cny=90)}
    panel = build_panel(rows, meta)
    assert list(panel.frames[ITEM].columns) == PANEL_COLUMNS
    assert len(panel.frames[ITEM]) == 3

    out = tmp_path / "panel"
    panel.save(out)
    loaded = MarketPanel.load(out)
    assert loaded.items == [ITEM]
    assert loaded.meta[ITEM].supply == 18000
    pd.testing.assert_frame_equal(
        loaded.frames[ITEM].astype(float), panel.frames[ITEM].astype(float),
        check_freq=False,
    )
    # gaps stay gaps: a stale view yields no "today" row
    view = loaded.up_to(pd.Timestamp("2024-06-10"))
    assert view.today(ITEM) is None


def test_coverage_report_flags_zero_volume(tmp_path):
    rows = {}
    ingest_snapshot(make_zip(tmp_path, "2024-06-01-12-15.zip",
                             [new_record(steam_vol=0)]), {ITEM}, rows, 0.05)
    panel = build_panel(rows, {})
    rep = coverage_report(panel)
    assert rep.iloc[0]["zero_vol_share"] == 1.0
