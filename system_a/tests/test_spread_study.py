import pytest

from shared.schema import Item
from shared.store import SnapshotStore
from system_a.spread_study import cross_spread_net, spread_stats


def _buff_item(name, ask, bid, listings=100, bids=10, ts=0.0):
    return Item(
        market_hash_name=name, buff_lowest_sell_cny=ask, buff_highest_buy_cny=bid,
        buff_listing_count=listings, buff_buy_order_count=bids,
        buff_volume_24h=None, ts=ts,
    )


def test_cross_spread_net_math():
    # flat market, 3% spread, 2.5% fee → lose ~spread+fee
    assert cross_spread_net(0.0, 0.03, 0.025) == pytest.approx(
        (0.985 / 1.015) * 0.975 - 1
    )
    # gross must exceed spread+fee to go positive
    assert cross_spread_net(0.06, 0.03, 0.025) > 0
    assert cross_spread_net(0.05, 0.03, 0.025) < 0
    # zero frictions → net == gross
    assert cross_spread_net(0.10, 0.0, 0.0) == pytest.approx(0.10)


def test_spread_stats_per_item():
    store = SnapshotStore()
    for i, (ask, bid) in enumerate([(100.0, 97.0), (102.0, 98.94), (101.0, 97.97)]):
        store.insert([_buff_item("A", ask, bid, ts=float(i))], source="buff")
    store.insert([_buff_item("B", 50.0, 49.5, ts=0.0)], source="buff")
    stats = {s.item: s for s in spread_stats(store)}
    assert stats["A"].n == 3
    assert stats["A"].median == pytest.approx(0.03, abs=1e-3)
    assert stats["B"].median == pytest.approx(0.01, abs=1e-3)
    # steam rows never contaminate spread measurement
    store.insert([_buff_item("C", 10.0, 10.0)], source="steam")
    assert "C" not in {s.item for s in spread_stats(store)}
