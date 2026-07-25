import os
from pathlib import Path

import pytest

from shared.configuration import Config, secret
from shared.feed import FeedUnavailable, ReplayFeed, item_to_json
from shared.store import SnapshotStore
from shared.synthetic import DAY, ItemSpec, generate_series

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_config_loads_shared_and_overlay():
    cfg = Config.load(REPO_ROOT, system="system_a")
    # BUFF cut the sell fee 2.5% → 1.5% on 2026-04-14 (Builder 2's verified
    # correction, buff.163.com/news/87397)
    assert cfg.require("costs.buff_fee_pct") == 0.015
    assert cfg.require("cooldown.trade_lock_days") == 7
    assert cfg.require("system_a.momentum_chase_max_layers") == 2
    assert cfg.get("nonexistent.path", default=42) == 42


def test_placeholder_secret_is_none(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "PLACEHOLDER")
    assert secret("SOME_API_KEY") is None
    monkeypatch.setenv("SOME_API_KEY", "real-key-123")
    assert secret("SOME_API_KEY") == "real-key-123"


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


class TestSteamLiveFeed:
    def test_parses_priceoverview(self, monkeypatch):
        from shared import feed as feedmod
        from shared.feed import SteamLiveFeed
        # fake scrapling Fetcher returning a priceoverview payload
        class FakeResp:
            def __init__(self, body): self.body = body
            text = ""
        payloads = {
            "AK-47 | Redline (Field-Tested)":
                '{"success":true,"lowest_price":"$42.21","volume":"55","median_price":"$55.12"}',
            "BadItem": '{"success":false}',
        }
        class FakeFetcher:
            @staticmethod
            def get(url, timeout=25):
                import urllib.parse as up
                name = up.parse_qs(up.urlparse(url).query)["market_hash_name"][0]
                return FakeResp(payloads[name].encode())
        import sys, types
        fake_mod = types.ModuleType("scrapling.fetchers")
        fake_mod.Fetcher = FakeFetcher
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_mod)

        f = SteamLiveFeed(["AK-47 | Redline (Field-Tested)", "BadItem"],
                          request_gap_seconds=0)
        items = f.fetch()
        assert len(items) == 1                      # BadItem skipped, not fatal
        assert items[0].buff_lowest_sell_cny == 42.21
        assert items[0].buff_volume_24h == 55
        assert items[0].buff_highest_buy_cny == 0.0  # Steam has no bid
        assert len(f.last_errors) == 1

    def test_degrades_without_scrapling(self, monkeypatch):
        import builtins
        from shared.feed import SteamLiveFeed, FeedUnavailable
        real = builtins.__import__
        def fake(name, *a, **k):
            if name.startswith("scrapling"):
                raise ImportError("no scrapling")
            return real(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", fake)
        with pytest.raises(FeedUnavailable):
            SteamLiveFeed(["X"]).fetch()

    def test_all_items_fail_is_fatal(self, monkeypatch):
        import sys, types
        from shared.feed import SteamLiveFeed, FeedUnavailable
        class FakeResp:
            body = b'{"success":false}'
            text = ""
        class FakeFetcher:
            @staticmethod
            def get(url, timeout=25): return FakeResp()
        m = types.ModuleType("scrapling.fetchers"); m.Fetcher = FakeFetcher
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", m)
        with pytest.raises(FeedUnavailable):
            SteamLiveFeed(["A", "B"], request_gap_seconds=0).fetch()
