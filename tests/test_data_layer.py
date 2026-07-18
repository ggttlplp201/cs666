import os
from pathlib import Path

import pytest

from shared.configuration import Config, secret
from shared.feed import Cs2shFeed, FeedUnavailable, ReplayFeed, item_to_json, normalize_cs2sh
from shared.store import SnapshotStore
from shared.synthetic import DAY, ItemSpec, generate_series

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_config_loads_shared_and_overlay():
    cfg = Config.load(REPO_ROOT, system="system_a")
    assert cfg.require("costs.buff_fee_pct") == 0.025
    assert cfg.require("cooldown.trade_lock_days") == 7
    assert cfg.require("system_a.momentum_chase_max_layers") == 2
    assert cfg.get("nonexistent.path", default=42) == 42


def test_placeholder_secret_is_none(monkeypatch):
    monkeypatch.setenv("CS2SH_API_KEY", "PLACEHOLDER")
    assert secret("CS2SH_API_KEY") is None
    monkeypatch.setenv("CS2SH_API_KEY", "real-key-123")
    assert secret("CS2SH_API_KEY") == "real-key-123"


def test_live_feed_unavailable_on_placeholder(monkeypatch):
    monkeypatch.setenv("CS2SH_API_KEY", "PLACEHOLDER")
    with pytest.raises(FeedUnavailable):
        Cs2shFeed(["AK-47 | Test (Field-Tested)"], usd_cny_rate=7.25).fetch()


def test_normalize_cs2sh_verified_shape_and_fx():
    # Verified response shape: items[name][source]; USD prices → CNY at fx rate.
    entry = {
        "buff": {"ask": 140.0, "ask_volume": 55, "bid": 135.2, "bid_volume": 4},
        "steam": {"ask": 190.0},
        "youpin": {"ask": 142.5},
        "collected_at": "2026-07-18T04:00:00Z",
        "updated_at": "2026-07-18T03:59:00Z",
    }
    item = normalize_cs2sh("M4A4 | Test (Factory New)", entry, usd_cny_rate=7.25)
    assert item.buff_lowest_sell_cny == pytest.approx(140.0 * 7.25)
    assert item.buff_highest_buy_cny == pytest.approx(135.2 * 7.25)
    assert item.buff_listing_count == 55      # listing count, NOT supply
    assert item.buff_buy_order_count == 4
    assert item.buff_volume_24h is None       # Developer tier: no executed volume
    # freshness comes from collected_at, not updated_at
    from datetime import datetime, timezone
    assert item.ts == datetime(2026, 7, 18, 4, tzinfo=timezone.utc).timestamp()
    assert item.cross_market == {"steam": 190.0, "youpin": 142.5}  # USD, as-is


def test_parse_latest_skips_buffless_and_reads_errors():
    payload = {
        "items": {
            "A": {"buff": {"ask": 10.0, "ask_volume": 1, "bid": 9.0, "bid_volume": 1},
                  "collected_at": 100.0},
            "B": {"steam": {"ask": 5.0}, "collected_at": 100.0},  # no BUFF quote
        },
        "errors": [{"item": "Not A Real Skin", "error": "unknown item"}],
    }
    items = Cs2shFeed.parse_latest(payload, usd_cny_rate=7.0)
    assert [i.market_hash_name for i in items] == ["A"]
    assert items[0].buff_lowest_sell_cny == pytest.approx(70.0)


def test_replay_feed_round_trip(tmp_path):
    series = generate_series([ItemSpec("A", 100.0), ItemSpec("B", 500.0)], days=3)
    path = tmp_path / "snapshots.jsonl"
    path.write_text(
        "\n".join(item_to_json(i) for snap in series for i in snap)
    )
    feed = ReplayFeed(path)
    snaps = list(feed)
    assert len(snaps) == 3
    assert {i.market_hash_name for i in snaps[0]} == {"A", "B"}
    with pytest.raises(FeedUnavailable):
        feed.fetch()


def test_synthetic_series_deterministic_with_events():
    spec = ItemSpec("X", 100.0, daily_vol=0.0, events={2: (0.30, 5.0)})
    s1 = generate_series([spec], days=4, seed=1)
    s2 = generate_series([ItemSpec("X", 100.0, daily_vol=0.0, events={2: (0.30, 5.0)})], days=4, seed=1)
    assert [x[0].buff_lowest_sell_cny for x in s1] == [x[0].buff_lowest_sell_cny for x in s2]
    # jump day: +30% price, 5x volume
    assert s1[2][0].buff_lowest_sell_cny == pytest.approx(130.0, rel=1e-6)
    assert s1[2][0].buff_volume_24h == 150
    assert s1[1][0].buff_volume_24h == 30


def test_store_series_latest_staleness():
    store = SnapshotStore()
    series = generate_series([ItemSpec("A", 100.0)], days=5)
    for snap in series:
        store.insert(snap)
    hist = store.series("A")
    assert len(hist) == 5
    assert hist == sorted(hist, key=lambda i: i.ts)
    latest = store.latest()
    assert latest["A"].ts == hist[-1].ts
    assert not store.is_stale(hist[-1].ts + 60, max_age_seconds=3600)
    assert store.is_stale(hist[-1].ts + 2 * DAY, max_age_seconds=3600)
    assert SnapshotStore().is_stale(0.0, 1.0)  # empty store is stale
