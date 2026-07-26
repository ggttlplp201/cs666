import json
from datetime import datetime, timezone

import pytest

from shared.steam_history import (
    DailyRow, SteamAuthError, SteamHistoryFetcher, normalize_rows, rows_to_items,
)
from shared.store import SnapshotStore

PAYLOAD = {
    "success": True,
    "price_prefix": "$",
    "prices": [
        ["Jul 16 2026 01: +0", 29.10, "120"],
        ["Jul 17 2026 01: +0", 29.55, "98"],
        ["Jul 18 2026 01: +0", 29.97, "142"],
    ],
}


def test_normalize_rows_parses_date_price_volume():
    rows = normalize_rows(PAYLOAD)
    assert rows[0] == DailyRow("Jul 16 2026", 29.10, 120)
    assert rows[-1].volume == 142   # quantity sold = REAL executed volume


def test_success_false_raises_auth_error():
    with pytest.raises(SteamAuthError, match="not logged in"):
        normalize_rows({"success": False})


def test_rows_to_items_utc_midnight_and_volume():
    items = rows_to_items("AK-47 | Redline (Field-Tested)", normalize_rows(PAYLOAD))
    assert items[0].ts == datetime(2026, 7, 16, tzinfo=timezone.utc).timestamp()
    assert items[0].buff_volume_24h == 120
    assert items[0].buff_lowest_sell_cny == 29.10  # stays USD (steam source)


def test_steam_rows_never_leak_into_buff_reads(tmp_path):
    store = SnapshotStore(tmp_path / "m.db")
    store.insert(rows_to_items("X", normalize_rows(PAYLOAD)), source="steam")
    assert store.series("X") == []                       # default source=buff
    assert len(store.series("X", source="steam")) == 3
    assert store.counts_by_source() == {"steam": 3}


def test_fetcher_uses_cache_without_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("STEAM_LOGIN_SECURE", "PLACEHOLDER")
    fetcher = SteamHistoryFetcher(tmp_path)
    name = "AK-47 | Redline (Field-Tested)"
    fetcher.cache_path(name).write_text(json.dumps(PAYLOAD))
    assert fetcher.is_cached(name)
    payload = fetcher.fetch_item(name)   # resumable: no network, no cookie needed
    assert len(normalize_rows(payload)) == 3


def test_fetcher_hard_fails_without_cookie_on_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("STEAM_LOGIN_SECURE", "PLACEHOLDER")
    fetcher = SteamHistoryFetcher(tmp_path)
    with pytest.raises(SteamAuthError, match="STEAM_LOGIN_SECURE"):
        fetcher.fetch_item("AWP | Asiimov (Field-Tested)")
