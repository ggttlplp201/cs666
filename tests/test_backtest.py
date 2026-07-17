import pytest

from shared.backtest import Backtester, EventSpec, event_study
from shared.synthetic import ItemSpec, generate_series

FEE = 0.025
KW = dict(wallet_cny=100_000.0, fee_pct=FEE, fill_volume_cap_k=0.35, trade_lock_days=7)


def _flat_series(days=15, price=100.0, volume=30):
    return generate_series(
        [ItemSpec("A", price, daily_vol=0.0, volume_24h=volume)], days=days
    )


def test_no_look_ahead_history():
    seen = []

    def strategy(ctx):
        seen.append(len(ctx.history("A")))

    Backtester(_flat_series(days=5), **KW).run(strategy)
    assert seen == [1, 2, 3, 4, 5]


def test_lock_blocks_sell_until_day7():
    sold_on_day = []

    def strategy(ctx):
        day = len(ctx.history("A")) - 1
        if day == 0:
            assert ctx.buy("A", 2) is not None
        for lot in ctx.ledger.open_lots():
            if ctx.sell_lot(lot):
                sold_on_day.append(day)

    Backtester(_flat_series(days=10), **KW).run(strategy)
    assert sold_on_day == [7]


def test_fills_depth_capped():
    fills = []

    def strategy(ctx):
        if len(ctx.history("A")) == 1:
            fills.append(ctx.buy("A", 100))

    Backtester(_flat_series(days=2, volume=30), **KW).run(strategy)
    assert fills[0].qty == int(0.35 * 30)


def test_round_trip_pnl_net_of_fee_and_spread():
    # Flat price: buy at ask 100, sell at bid 97 after unlock, minus 2.5% fee.
    def strategy(ctx):
        if len(ctx.history("A")) == 1:
            ctx.buy("A", 2)
        for lot in ctx.ledger.open_lots():
            ctx.sell_lot(lot)

    result = Backtester(_flat_series(days=10), **KW).run(strategy)
    expected = 2 * (97.0 - 100.0) - 2 * 97.0 * FEE
    assert result.realized_pnl == pytest.approx(expected)
    assert result.buys == 1 and result.sells == 1
    assert result.fees_paid == pytest.approx(2 * 97.0 * FEE)
    # equity curve ends at wallet start + realized pnl (no open inventory)
    assert result.final_equity == pytest.approx(100_000.0 + expected)


def test_thin_book_defers_lot_exit():
    def strategy(ctx):
        if len(ctx.history("A")) == 1:
            ctx.buy("A", 10)
        for lot in ctx.ledger.open_lots():
            ctx.sell_lot(lot)

    # volume 30 → buy capped at 10; then volume collapses → lot can't exit
    series = generate_series(
        [ItemSpec("A", 100.0, daily_vol=0.0, volume_24h=30,
                  events={1: (0.0, 0.1)})], days=10,
    )
    # events only change one day; force persistent thin book instead:
    for day in range(1, 10):
        snap = series[day]
        series[day] = [
            type(snap[0])(**{**snap[0].__dict__, "buff_volume_24h": 5})
        ]
    result = Backtester(series, **KW).run(strategy)
    assert result.sells == 0  # 10 > 0.35*5 → refused every day, still held


def test_event_study_durable_vs_fading_event():
    # A durable structural repricing keeps drifting after detection; entering
    # at the jump price with zero follow-through cannot beat the ~5.5%
    # round-trip cost (3% spread + 2.5% fee) — that case is asserted below.
    durable = ItemSpec(
        "DUR", 100.0, daily_vol=0.0,
        events={2: (0.30, 5.0), 3: (0.04, 2.0), 4: (0.03, 1.5)},
    )
    fading = ItemSpec(
        "FADE", 100.0, daily_vol=0.0,
        events={2: (0.30, 5.0), 6: (-0.25, 2.0)},
    )  # spike that decays before unlock
    series = generate_series([durable, fading], days=12)
    outcomes = event_study(
        series,
        events=[EventSpec(2, "DUR"), EventSpec(2, "FADE")],
        budget_per_event_cny=1000.0,
        fee_pct=FEE,
        fill_volume_cap_k=0.35,
        trade_lock_days=7,
    )
    by_item = {o.event.item: o for o in outcomes}
    assert by_item["DUR"].traded and by_item["DUR"].net_pnl > 0
    assert by_item["FADE"].traded and by_item["FADE"].net_pnl < 0


def test_event_study_flat_after_jump_loses_round_trip_cost():
    """Entering at the post-jump price with no follow-through loses the
    spread+fee — the §5.1 'pad required_edge' rule made concrete."""
    flat_jump = ItemSpec("X", 100.0, daily_vol=0.0, events={2: (0.30, 5.0)})
    outcomes = event_study(
        generate_series([flat_jump], days=12),
        events=[EventSpec(2, "X")],
        budget_per_event_cny=1000.0,
        fee_pct=FEE,
        fill_volume_cap_k=0.35,
        trade_lock_days=7,
    )
    assert outcomes[0].traded and outcomes[0].net_pnl < 0
