from shared.schema import Item
from system_a.event_study import DAY
from system_a.trade_up_control import _hold_return, _summ


def test_summ():
    assert "n=0" in _summ([])
    assert "mean +10%" in _summ([0.1, 0.1])


def test_hold_return_frictions():
    # flat price, 4% spread, 2.5% fee, 60d hold → lose spread+fee
    series = [Item("X", 100.0, 100.0, 0, 0, None, i * DAY) for i in range(80)]
    r = _hold_return(series, 0.0, spread=0.04)
    # entry 100*1.02, exit 100*0.98*0.975 → clearly negative
    assert r is not None and r < 0

    # +50% price move clears the frictions
    series2 = [Item("X", (150.0 if i >= 30 else 100.0), 100.0, 0, 0, None, i * DAY)
               for i in range(80)]
    assert _hold_return(series2, 0.0, spread=0.04) > 0.3
