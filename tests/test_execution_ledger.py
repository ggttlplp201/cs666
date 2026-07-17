import pytest

from shared.execution import PaperBackend, reconcile
from shared.ledger import DAY, Ledger
from shared.provenance import ProvenanceLog
from shared.schema import Fill, Item, Order, OrderSide

FEE = 0.025


def _item(name="A", ask=100.0, bid=97.0, volume=30, ts=0.0):
    return Item(
        market_hash_name=name, buff_lowest_sell_cny=ask, buff_highest_buy_cny=bid,
        buff_listing_count=100, buff_buy_order_count=5, buff_volume_24h=volume, ts=ts,
    )


def _buy_fill(name="A", qty=2, price=100.0, ts=0.0):
    return Fill(
        client_order_id=f"b-{name}-{ts}", side=OrderSide.BUY, market_hash_name=name,
        qty=qty, price_cny=price, fee_cny=0.0, ts=ts,
    )


def _sell_fill(name="A", qty=2, price=110.0, ts=8 * DAY):
    return Fill(
        client_order_id=f"s-{name}-{ts}", side=OrderSide.SELL, market_hash_name=name,
        qty=qty, price_cny=price, fee_cny=qty * price * FEE, ts=ts,
    )


class TestLedger:
    def test_t7_lock_blocks_early_sell(self):
        ledger = Ledger(trade_lock_days=7)
        lot = ledger.record_buy(_buy_fill(ts=0.0))
        assert lot.unlock_ts == 7 * DAY
        assert lot.is_locked(6.9 * DAY)
        assert not lot.is_locked(7.0 * DAY)
        with pytest.raises(ValueError, match="locked"):
            ledger.record_sell(lot.lot_id, _sell_fill(ts=6 * DAY))
        sold = ledger.record_sell(lot.lot_id, _sell_fill(ts=8 * DAY))
        assert not sold.is_open

    def test_sellable_and_locked_capital(self):
        ledger = Ledger(trade_lock_days=7)
        ledger.record_buy(_buy_fill(ts=0.0, qty=2, price=100.0))
        ledger.record_buy(_buy_fill(ts=5 * DAY, qty=3, price=200.0))
        now = 8 * DAY  # first lot unlocked, second still locked
        assert [l.qty for l in ledger.sellable_lots("A", now)] == [2]
        assert ledger.locked_capital(now) == 3 * 200.0
        assert ledger.deployed_capital() == 2 * 100.0 + 3 * 200.0
        assert ledger.position_qty("A") == 5

    def test_realized_and_marked_pnl_net_of_fees(self):
        ledger = Ledger(trade_lock_days=7)
        lot = ledger.record_buy(_buy_fill(qty=2, price=100.0, ts=0.0))
        ledger.record_sell(lot.lot_id, _sell_fill(qty=2, price=110.0, ts=8 * DAY))
        # realized: 2*(110-100) - fee(2*110*0.025) = 20 - 5.5
        assert ledger.realized_pnl() == pytest.approx(14.5)
        open_lot = ledger.record_buy(_buy_fill(qty=1, price=100.0, ts=9 * DAY))
        marked = ledger.marked_pnl({"A": 110.0}, fee_pct=FEE)
        assert marked == pytest.approx(110.0 * 0.975 - 100.0)
        assert open_lot.is_open

    def test_double_sell_and_partial_sell_refused(self):
        ledger = Ledger(trade_lock_days=7)
        lot = ledger.record_buy(_buy_fill(qty=2, ts=0.0))
        ledger.record_sell(lot.lot_id, _sell_fill(qty=2, ts=8 * DAY))
        with pytest.raises(ValueError, match="already sold"):
            ledger.record_sell(lot.lot_id, _sell_fill(qty=2, ts=9 * DAY))
        lot2 = ledger.record_buy(_buy_fill(qty=4, ts=0.0))
        with pytest.raises(ValueError, match="partial"):
            ledger.record_sell(lot2.lot_id, _sell_fill(qty=1, ts=9 * DAY))


class TestPaperBackend:
    def _backend(self, wallet=10_000.0, k=0.35):
        backend = PaperBackend(wallet_cny=wallet, fee_pct=FEE, fill_volume_cap_k=k)
        backend.set_market({"A": _item(volume=30)})
        return backend

    def test_buy_fills_at_ask_no_buyer_fee(self):
        backend = self._backend()
        fill = backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 100.0))
        assert fill is not None and fill.qty == 2 and fill.price_cny == 100.0
        assert backend.get_wallet() == 10_000.0 - 200.0
        assert backend.get_inventory() == {"A": 2}

    def test_buy_below_ask_rests_unfilled(self):
        backend = self._backend()
        assert backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 99.0)) is None
        assert backend.get_wallet() == 10_000.0

    def test_depth_cap_limits_fill(self):
        backend = self._backend(k=0.1)  # 0.1 * 30 volume = 3 units max
        fill = backend.place_buy(Order("o1", OrderSide.BUY, "A", 10, 100.0))
        assert fill.qty == 3

    def test_insufficient_wallet_refuses(self):
        backend = self._backend(wallet=150.0)
        assert backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 100.0)) is None

    def test_idempotent_order_ids(self):
        backend = self._backend()
        first = backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 100.0))
        second = backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 100.0))
        assert first is second
        assert backend.get_inventory() == {"A": 2}

    def test_sell_fills_at_bid_with_fee(self):
        backend = self._backend()
        backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 100.0))
        wallet_before = backend.get_wallet()
        fill = backend.place_sell(Order("o2", OrderSide.SELL, "A", 2, 95.0))
        assert fill.price_cny == 97.0
        assert fill.fee_cny == pytest.approx(2 * 97.0 * FEE)
        assert backend.get_wallet() == pytest.approx(wallet_before + 2 * 97.0 * 0.975)
        assert backend.get_inventory() == {}

    def test_sell_without_inventory_refused(self):
        backend = self._backend()
        assert backend.place_sell(Order("o1", OrderSide.SELL, "A", 1, 90.0)) is None

    def test_reconcile_flags_divergence(self):
        backend = self._backend()
        backend.place_buy(Order("o1", OrderSide.BUY, "A", 2, 100.0))
        assert reconcile(backend, {"A": 2}) == []
        problems = reconcile(backend, {"A": 3})
        assert len(problems) == 1 and "A" in problems[0]


def test_provenance_round_trip(tmp_path):
    log = ProvenanceLog(tmp_path / "decisions.jsonl")
    log.record(
        ts=1.0, action="buy_placed", item="A", rule="update_reaction",
        regime="sideways", signals=[{"type": "update_leak", "confidence": 0.8}],
        inputs={"ask": 100.0}, score=0.8, order_id="o1",
    )
    log.record(
        ts=2.0, action="buy_refused", item="B", rule="liquidity_floor",
        regime="sideways", signals=[], inputs={"volume_24h": 3},
    )
    records = log.read_all()
    assert len(records) == 2
    assert records[0]["action"] == "buy_placed"
    assert records[1]["rule"] == "liquidity_floor"


class TestLotSplit:
    def test_split_preserves_basis_and_unlock(self):
        ledger = Ledger(trade_lock_days=7)
        lot = ledger.record_buy(_buy_fill(qty=10, price=100.0, ts=0.0))
        piece = ledger.split_lot(lot.lot_id, 4)
        remainder = ledger.get(lot.lot_id)
        assert piece.qty == 4 and remainder.qty == 6
        assert piece.buy_price == remainder.buy_price == 100.0
        assert piece.unlock_ts == remainder.unlock_ts == 7 * DAY
        assert ledger.position_qty("A") == 10

    def test_split_validation(self):
        ledger = Ledger(trade_lock_days=7)
        lot = ledger.record_buy(_buy_fill(qty=3, ts=0.0))
        with pytest.raises(ValueError, match="invalid"):
            ledger.split_lot(lot.lot_id, 3)
        with pytest.raises(ValueError, match="invalid"):
            ledger.split_lot(lot.lot_id, 0)
        ledger.record_sell(lot.lot_id, _sell_fill(qty=3, ts=8 * DAY))
        with pytest.raises(ValueError, match="already sold"):
            ledger.split_lot(lot.lot_id, 1)
