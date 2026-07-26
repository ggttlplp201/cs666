"""Resting (passive) limit orders and the handicaps that keep them honest.

A resting fill assumes we held queue position at that price — an assumption a
daily-bar backtest cannot verify — so it is deliberately harder to earn than a
marketable fill. These tests pin each handicap, because every one of them is a
way the backtest could otherwise flatter itself.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from shared_b.data import MarketPanel
from shared_b.execution import PaperBroker
from shared_b.ledger import Ledger
from shared_b.schema import ItemMeta, Order, Side

ITEM = "AK-47 | Redline (Field-Tested)"
D0 = date(2026, 1, 1)


def _panel(asks: list[float], observed: list[int] | None = None,
           volume: int = 1000, depth: int = 500) -> MarketPanel:
    days = pd.date_range(D0, periods=len(asks), freq="D")
    df = pd.DataFrame(
        {
            "sell_price": asks,
            "buy_price": [a * 0.95 for a in asks],
            "listing_count": depth,
            "buy_order_count": depth,
            "volume": volume,
            "valid_buy_orders": -1,
            "is_observed": observed if observed is not None else [1] * len(asks),
        },
        index=days,
    )
    return MarketPanel(frames={ITEM: df}, meta={ITEM: ItemMeta(market_hash_name=ITEM)})


def _order(limit: float, qty: int = 10, day: date = D0) -> Order:
    return Order(item=ITEM, side=Side.BUY, qty=qty, limit_price=limit, day=day)


def _settle_through(broker: PaperBroker, n: int) -> list:
    """Settle days D0+1 .. D0+n, returning all fills."""
    out = []
    for i in range(1, n + 1):
        out.extend(broker.settle(D0 + timedelta(days=i)))
    return out


# ------------------------------------------------------------ default = old
def test_ttl_1_expires_after_one_attempt():
    """The original semantics must be untouched by default: an order that
    misses its first settlement is dropped, not left working."""
    broker = PaperBroker(panel=_panel([100, 100, 90]), order_ttl_days=1)
    broker.place_buy(_order(limit=95))       # below day-1 ask of 100 -> no fill
    assert _settle_through(broker, 1) == []
    assert broker.pending == []              # gone, even though day 3 would fill


# ------------------------------------------------------------------ resting
def test_resting_order_waits_for_its_dip():
    broker = PaperBroker(panel=_panel([100, 100, 90]), order_ttl_days=7)
    broker.place_buy(_order(limit=95))
    fills = _settle_through(broker, 2)
    assert len(fills) == 1
    assert fills[0].fill_price <= 95, "a buy must never pay above its limit"


def test_resting_order_expires_at_its_ttl():
    """It must not wait forever — a stale order is a stale thesis."""
    broker = PaperBroker(panel=_panel([100] * 6 + [50]), order_ttl_days=3)
    broker.place_buy(_order(limit=95))
    fills = _settle_through(broker, 6)
    assert fills == []
    assert broker.pending == []


def test_never_fills_against_a_carried_forward_quote():
    """A carried book is a stale quote, not executable liquidity. Day 2 would
    fill on price alone, but its book was forward-filled."""
    # day 1 ask is above the limit (no fill, order rests); day 2 is cheap but
    # its book was carried; day 3 is cheap and observed.
    panel = _panel([100, 100, 90, 90], observed=[1, 1, 0, 1])
    broker = PaperBroker(panel=panel, order_ttl_days=7, require_observed_book=True)
    broker.place_buy(_order(limit=95))
    broker.settle(D0 + timedelta(days=1))
    carried = broker.settle(D0 + timedelta(days=2))
    assert carried == [], "filled against a carried quote"
    assert broker.pending, "order should still be resting"
    observed = broker.settle(D0 + timedelta(days=3))
    assert len(observed) == 1, "should fill once the book is observed again"


def test_passive_fills_get_a_smaller_slice_of_volume_than_marketable_ones():
    """We are behind an unknown queue, so a resting order cannot claim the
    same share of the day's volume that an aggressive order gets."""
    aggressive = PaperBroker(panel=_panel([90, 90]), order_ttl_days=7,
                             fill_fraction=0.25, passive_fill_fraction=0.10)
    aggressive.place_buy(_order(limit=95, qty=10_000))
    got_aggressive = aggressive.settle(D0 + timedelta(days=1))[0].qty

    passive = PaperBroker(panel=_panel([100, 100, 90]), order_ttl_days=7,
                          fill_fraction=0.25, passive_fill_fraction=0.10)
    passive.place_buy(_order(limit=95, qty=10_000))
    passive.settle(D0 + timedelta(days=1))          # day 1: no fill, rests
    got_passive = passive.settle(D0 + timedelta(days=2))[0].qty

    assert got_passive < got_aggressive


def test_adverse_selection_charge_worsens_the_passive_fill_price():
    """Passive buys fill disproportionately when the price is falling; the
    charge must make the fill worse, never better."""
    def price(charge: float) -> float:
        b = PaperBroker(panel=_panel([100, 100, 90]), order_ttl_days=7,
                        adverse_selection_pct=charge)
        b.place_buy(_order(limit=95))
        b.settle(D0 + timedelta(days=1))
        return b.settle(D0 + timedelta(days=2))[0].fill_price

    assert price(0.02) > price(0.0)


def test_marketable_fills_are_not_charged_adverse_selection():
    """The charge models queue risk — an order that crosses has none."""
    b = PaperBroker(panel=_panel([90, 90]), order_ttl_days=7,
                    adverse_selection_pct=0.02, slippage_pct=0.0)
    b.place_buy(_order(limit=95))
    fill = b.settle(D0 + timedelta(days=1))[0]
    assert fill.fill_price == pytest.approx(90.0)


# --------------------------------------------------------------------- cash
def test_resting_buy_commits_cash_so_one_balance_cannot_back_two_orders():
    led = Ledger(starting_cash=1_000.0)
    led.commit_order_cash("o1", 800.0)
    assert led.committed_cash() == 800.0
    assert led.spendable_cash() == 200.0
    led.release_order_cash("o1")
    assert led.spendable_cash() == 1_000.0


def test_releasing_an_unknown_order_is_harmless():
    led = Ledger(starting_cash=1_000.0)
    assert led.release_order_cash("never-placed") == 0.0
    assert led.spendable_cash() == 1_000.0


def test_spendable_cash_never_goes_negative():
    led = Ledger(starting_cash=100.0)
    led.commit_order_cash("o1", 500.0)
    assert led.spendable_cash() == 0.0
