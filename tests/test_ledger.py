"""Ledger: T+7 lock, T+7 sale settlement, partial exits, P&L accounting."""

from datetime import date, timedelta

import pytest

from shared.ledger import Ledger
from shared.schema import Fill, Order, Side


def _buy(item="X", qty=4, px=100.0, day=date(2026, 1, 1), fee=0.0):
    o = Order(item=item, side=Side.BUY, qty=qty, limit_price=px, day=day)
    return Fill(order=o, fill_day=day, fill_price=px, qty=qty, fee=fee)


def _sell(lot, qty, px, day, fee_pct=0.015, reason="tp"):
    o = Order(item=lot.item, side=Side.SELL, qty=qty, limit_price=px, day=day,
              reason=reason, lot_id=lot.lot_id)
    return Fill(order=o, fill_day=day, fill_price=px, qty=qty, fee=qty * px * fee_pct)


def test_buy_creates_locked_lot():
    led = Ledger(starting_cash=10_000)
    lot = led.apply_fill(_buy())
    assert led.cash == 10_000 - 400
    assert lot.unlock_day == date(2026, 1, 8)
    assert lot.locked(date(2026, 1, 7))
    assert not lot.locked(date(2026, 1, 8))
    assert led.unlocked_lots(date(2026, 1, 5)) == []
    assert led.unlocked_lots(date(2026, 1, 8)) == [lot]


def test_sell_before_unlock_rejected():
    led = Ledger(starting_cash=10_000)
    lot = led.apply_fill(_buy())
    with pytest.raises(ValueError, match="locked"):
        led.apply_fill(_sell(lot, 4, 120.0, date(2026, 1, 5)))


def test_sale_proceeds_settle_t_plus_7():
    led = Ledger(starting_cash=10_000)
    lot = led.apply_fill(_buy())
    sell_day = date(2026, 1, 10)
    led.apply_fill(_sell(lot, 4, 120.0, sell_day))
    proceeds = 4 * 120.0 * (1 - 0.015)
    # cash NOT credited yet — receivable until settlement clears
    assert led.cash == pytest.approx(10_000 - 400)
    assert led.receivables() == pytest.approx(proceeds)
    assert led.settle_cash(sell_day + timedelta(days=6)) == 0
    assert led.settle_cash(sell_day + timedelta(days=7)) == pytest.approx(proceeds)
    assert led.cash == pytest.approx(10_000 - 400 + proceeds)
    assert led.receivables() == 0


def test_partial_sell_splits_lot_and_keeps_lock_history():
    led = Ledger(starting_cash=10_000)
    lot = led.apply_fill(_buy(qty=4))
    day = date(2026, 1, 9)
    closed = led.apply_fill(_sell(lot, 3, 110.0, day))
    assert closed.qty == 3 and not closed.open
    rest = led.open_lots("X")
    assert len(rest) == 1 and rest[0].qty == 1
    assert rest[0].unlock_day == lot.unlock_day
    # realized on the closed piece only
    assert led.realized_pnl() == pytest.approx(3 * 10.0 - 3 * 110.0 * 0.015)


def test_equity_includes_receivables_and_marks():
    led = Ledger(starting_cash=1_000)
    lot = led.apply_fill(_buy(qty=2, px=100.0))
    led.apply_fill(_sell(lot, 1, 120.0, date(2026, 1, 9)))
    marks = {"X": 130.0}
    eq = led.equity(marks, fee_pct=0.015)
    expected = led.cash + 1 * 120.0 * (1 - 0.015) + 1 * 130.0 * (1 - 0.015)
    assert eq == pytest.approx(expected)


def test_roundtrip_serialization():
    led = Ledger(starting_cash=5_000)
    lot = led.apply_fill(_buy(qty=2))
    led.apply_fill(_sell(lot, 1, 111.0, date(2026, 1, 9)))
    led2 = Ledger.from_dict(led.to_dict())
    assert led2.cash == pytest.approx(led.cash)
    assert led2.receivables() == pytest.approx(led.receivables())
    assert len(led2.open_lots()) == len(led.open_lots())
    assert led2.realized_pnl() == pytest.approx(led.realized_pnl())
