import pytest

from shared.iflow_history import file_ts, parse_record, select_event_window_files
from shared.store import SnapshotStore


def test_file_ts_is_utc8():
    # 2026-03-19 00:15 UTC+8 == 2026-03-18 16:15 UTC
    from datetime import datetime, timezone
    assert file_ts("2026-03-19-00-15.zip") == datetime(
        2026, 3, 18, 16, 15, tzinfo=timezone.utc
    ).timestamp()


def test_parse_new_schema():
    record = {
        "appid": 730, "hash_name": "X",
        "buff_sell": {"price": 90.0, "orders": [90.0, 92.4], "count": 91},
        "buff_buy": {"price": 92.0, "orders": [92.0, 90.0], "count": 187},
    }
    assert parse_record(record) == (90.0, 92.0, 91, 187)


def test_parse_old_schema_no_bid():
    record = {
        "appid": 730, "hash_name": "X",
        "buff_sell_list": [[21.78, 392.0, 1.0], [21.69, 288.0, 1.0]],
        "buff_sell_num": 2509, "buff_buy_num": 144,
    }
    ask, bid, listings, bids = parse_record(record)
    assert ask == 21.69          # min of ladder
    assert bid == 0.0            # old schema: BUFF bid unavailable
    assert (listings, bids) == (2509, 144)


def test_parse_unusable_records():
    assert parse_record({"buff_sell": {"price": None}}) is None
    assert parse_record({"buff_sell_list": []}) is None
    assert parse_record({"hash_name": "X"}) is None


def test_event_window_selection_thins_to_daily():
    files = [f"2025-10-{d:02d}-{h}-00.zip" for d in range(1, 32)
             for h in ("04", "16")]
    picked = select_event_window_files(
        files, ["2025-10-22"], pre_days=3, post_days=3, files_per_day=1
    )
    # Filenames are UTC+8: day 19's 04-00 file is Oct 18 20:00 UTC (outside
    # the window), so day 19 is represented by its 16-00 file; day 25's
    # 04-00 (Oct 24 20:00 UTC) is in, its 16-00 is out.
    assert picked == ["2025-10-19-16-00.zip"] + [
        f"2025-10-{d:02d}-04-00.zip" for d in range(20, 26)
    ]
    assert len({p[:10] for p in picked}) == len(picked)   # one per day


def test_iflow_rows_isolated_from_other_sources():
    store = SnapshotStore()
    from shared.schema import Item
    store.insert([Item("X", 90.0, 88.0, 10, 5, None, 0.0)], source="buff_iflow")
    assert store.series("X") == []                          # default buff
    assert store.series("X", source="steam") == []
    assert len(store.series("X", source="buff_iflow")) == 1
