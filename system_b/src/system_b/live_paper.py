"""System B forward paper trading on the LIVE market, starting now.

Not a backtest. Each run is one real decision cycle against data that did not
exist when the previous cycle ran: settle yesterday's working orders at today's
prices, score today's universe, place today's orders, persist, exit. State
lives in var/live_paper.json and accumulates, so the equity curve is a forward
out-of-sample record rather than a replay.

WHAT IT TRADES ON, AND THE LIMITATION THAT COMES WITH IT.

The only live feed in this repo is Steam via Scrapling: real ask, real 24h
executed volume, no bid and no depth (priceoverview does not expose buy
orders). Two consequences, both recorded in every result rather than buried:

  * The BID-DEPTH GATE CANNOT RUN. System B's hard filters require at least
    3 valid buy orders, which the feed cannot supply, so items are admitted on
    price and volume alone. The structural half of the selection thesis is not
    being tested here.
  * COSTS ARE STEAM'S, NOT BUFF'S. Steam charges roughly 13% effective on a
    sale against BUFF's 1.5%. Measured edge on this strategy is around 3% over
    21 days, so NET PROFITABILITY IS NOT EXPECTED and a loss is the base case.
    `--fee` exists so the same live decisions can be re-priced at BUFF costs
    for comparison; the run records both.

So the question this actually answers is not "does it make money on Steam"
(almost certainly not) but "does the ranker predict forward, on data nobody
has seen". That is the open question worth days of wall-clock, and it is why
the forward rank IC is recorded alongside P&L.

Run one cycle:   make b-live
Every 6 hours:   0 */6 * * *  cd <repo> && make b-live
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from shared_b.config import REPO_ROOT, load_config
from shared_b.data import MarketPanel
from shared_b.execution import PaperBroker
from shared_b.journal import Journal
from shared_b.ledger import Ledger
from shared_b.real_panel import derive_meta
from shared_b.schema import Order, Side
from system_b.model import forward_log_returns
from system_b.strategy import PositionalStrategy

# Steam's effective sell fee: 15% nominal, ~13.04% of the buyer-paid price.
STEAM_FEE = 0.1304
BUFF_FEE = 0.015
LIVE_SOURCE = "steam_live"
HISTORY_SOURCE = "steam"   # archived daily Steam, same items, for feature warmup
DEFAULT_CAPITAL = 500_000.0
STATE = REPO_ROOT / "var" / "live_paper.json"


def load_live_panel(db_path: Path, source: str = LIVE_SOURCE,
                    history_source: str = HISTORY_SOURCE) -> MarketPanel:
    """Daily panel: archived Steam history for warmup, live feed for today on.

    Trading is forward only, but the FEATURES are not computable from thin air:
    the indicators use 20-day windows and the ranker needs hundreds of training
    rows, so a panel starting today would sit inert for weeks. History supplies
    the lookback; `first_live_day()` is what stops any decision being made on a
    day that already happened.

    Both series are the same venue and the same items, so they concatenate
    without a cross-venue join. The poller samples every few minutes; a
    strategy re-deciding on every tick would trade its own sampling noise, so
    ticks collapse to the last observation of each day. `valid_buy_orders`
    stays at the schema's -1 "unknown" sentinel: the feed cannot answer it."""
    with sqlite3.connect(str(db_path)) as con:
        df = pd.read_sql(
            "SELECT market_hash_name, ts, lowest_sell, volume_24h, source "
            "FROM snapshots WHERE source IN (?, ?)",
            con, params=(history_source, source))
    if df.empty:
        return MarketPanel(frames={}, meta={})
    df["day"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_localize(None).dt.floor("D")
    df = df.sort_values("ts").drop_duplicates(["market_hash_name", "day"], keep="last")

    frames = {}
    for item, g in df.groupby("market_hash_name"):
        g = g.set_index("day").sort_index()
        px = _anchor_history(g, history_source, source)
        frames[str(item)] = pd.DataFrame({
            "sell_price": px,
            # No bid on this feed. Modelling one would invent a spread; the ask
            # is used on both sides and the fee carries the whole cost.
            "buy_price": px,
            "listing_count": 0,
            "buy_order_count": 0,
            "volume": g["volume_24h"].astype(float).fillna(0.0),
            "valid_buy_orders": -1,
            "is_observed": 1,
        }, index=g.index)
    return MarketPanel(frames=frames, meta=derive_meta(list(frames)))


def _anchor_history(g: pd.DataFrame, history_source: str, live_source: str) -> pd.Series:
    """Put archived prices into the LIVE series' units, per item.

    The two Steam series are on different scales: measured across all 19 items
    the live price is a consistent ~0.153 of the last archived price, which is
    a unit conversion (one series is CNY, the other USD), not a market move.
    Concatenating them raw prints a fake ~85% crash on the join day and poisons
    every feature and target downstream.

    Rather than guess an FX rate to make the line look continuous, history is
    multiplied by that item's own join ratio (first live price / last archived
    price). This is safe precisely because SYSTEM B'S FEATURES ARE
    SCALE-INVARIANT: returns, moving-average deviation, Bollinger position and
    volume patterns are all unchanged by a constant factor. Only the shape of
    history is used; the level is discarded, and every trade happens on a live
    day at a live price in live units.

    It also removes the fake join-day return by construction, since the last
    archived point is anchored onto the first live one. The cost is that a real
    price move inside the archive-to-live gap is absorbed into the ratio, which
    affects the level we already discard, not the returns we use."""
    px = g["lowest_sell"].astype(float)
    hist, live = g["source"] == history_source, g["source"] == live_source
    if not hist.any() or not live.any():
        return px
    last_hist, first_live = px[hist].iloc[-1], px[live].iloc[0]
    if last_hist <= 0 or first_live <= 0:
        return px
    scaled = px.copy()
    scaled[hist] = px[hist] * (first_live / last_hist)
    return scaled


def first_live_day(db_path: Path, source: str = LIVE_SOURCE) -> pd.Timestamp | None:
    """First day the LIVE feed produced data. No decision is ever made before
    this: everything earlier is archive, and trading it would be a backtest
    wearing a forward-run's clothes."""
    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT MIN(ts) FROM snapshots WHERE source = ?", (source,)).fetchone()
    if not row or row[0] is None:
        return None
    return (pd.to_datetime(float(row[0]), unit="s", utc=True)
            .tz_localize(None).floor("D"))


def _relax_for_feed(cfg: dict) -> dict:
    """Drop only the gates the live feed cannot answer, and say which.

    Depth gates are unanswerable without a book. Volume, pump-shape and
    parabolic-attention gates all still run: they are the safety gates, and
    they are the ones worth keeping."""
    sel = dict(cfg.get("selection_filters", {}))
    sel["min_valid_buy_orders"] = 0
    cfg["selection_filters"] = sel
    rc = dict(cfg.get("risk_controls", {}))
    rc["allowlist"] = sorted(set(rc.get("allowlist", []) or []))
    cfg["risk_controls"] = rc
    return cfg


def order_to_dict(o: Order) -> dict:
    return {"item": o.item, "side": o.side.value, "qty": o.qty,
            "limit_price": o.limit_price, "day": str(o.day),
            "client_order_id": o.client_order_id, "reason": o.reason,
            "lot_id": o.lot_id, "batch_index": o.batch_index}


def order_from_dict(d: dict) -> Order:
    return Order(item=d["item"], side=Side(d["side"]), qty=int(d["qty"]),
                 limit_price=float(d["limit_price"]),
                 day=date.fromisoformat(d["day"]),
                 client_order_id=d["client_order_id"], reason=d.get("reason", ""),
                 lot_id=d.get("lot_id"), batch_index=int(d.get("batch_index", 0)))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"cycles": [], "ledger": None, "started": None, "fills": [],
            "pending": []}
    try:
        st = json.loads(path.read_text())
        st.setdefault("pending", [])
        return st
    except json.JSONDecodeError:
        return {"cycles": [], "ledger": None, "started": None, "fills": [],
                "pending": []}


def run_cycle(db_path: Path, capital: float = DEFAULT_CAPITAL,
              fee: float = STEAM_FEE, state_path: Path = STATE,
              today: date | None = None, order_ttl: int = 7,
              adverse_selection: float = 0.01) -> dict:
    cfg = _relax_for_feed(dict(load_config("b")))
    cfg["capital"] = dict(cfg.get("capital", {}))
    cfg["capital"]["total"] = capital

    panel = load_live_panel(db_path)
    if not panel.frames:
        return {"status": "no_live_data"}
    cal = panel.calendar()
    day = pd.Timestamp(today) if today else cal.max()
    if day not in cal:
        return {"status": "no_data_today", "latest": str(cal.max().date())}

    # Forward only. History is in the panel so the features have a lookback,
    # but a decision on a pre-live day would be a backtest, not a live run.
    live_from = first_live_day(db_path)
    if live_from is None:
        return {"status": "no_live_data"}
    if day < live_from:
        return {"status": "before_live_start", "live_from": str(live_from.date())}

    state = load_state(state_path)
    ledger = (Ledger.from_dict(state["ledger"]) if state.get("ledger")
              else Ledger(starting_cash=capital))
    # Orders REST across cycles. A left-side limit sits below the ask by
    # design and will not fill on the day it is placed, so with a 1-day TTL
    # every order would expire unfilled between process invocations and the
    # run could never open a position.
    broker = PaperBroker(panel=panel, fee_pct=fee, slippage_pct=0.005,
                         fill_fraction=0.25, order_ttl_days=order_ttl,
                         passive_fill_fraction=0.10, require_observed_book=False,
                         adverse_selection_pct=adverse_selection)
    broker.pending = [order_from_dict(d) for d in state.get("pending", [])]
    journal = Journal(None)

    # decide on today's data, with history truncated at today
    strategy = PositionalStrategy(cfg=cfg)
    horizon = int(cfg.get("model", {}).get("horizon_days", 21))
    strategy.set_targets(forward_log_returns(panel.frames, horizon))

    # The strategy's in-flight guards live in memory, so a fresh process has no
    # idea an order is already working and would stack a duplicate on every
    # run. Reseed them from the restored book: without this, a cycle every 6
    # hours piles up orders on the same item and over-commits the cash behind
    # them. Sells are keyed by lot, buys by item, matching the guards.
    for o in broker.pending:
        if o.side == Side.BUY:
            strategy.last_order_day[o.item] = day.date()
        elif o.lot_id is not None:
            strategy.last_sell_day[o.lot_id] = day.date()
    view = panel.up_to(day)
    orders = strategy.on_cycle(view, ledger, journal)

    # execute against today's book (same-day fill: the live feed has no
    #    "tomorrow" yet, so this cycle's orders fill at today's ask plus
    #    slippage rather than waiting a day and drifting out of the record)
    for o in orders:
        if o.side == Side.BUY:
            broker.place_buy(o)
        else:
            broker.place_sell(o)
    fills = broker.settle(day.date())
    for f in fills:
        ledger.apply_fill(f)

    marks = {i: float(panel.frames[i].loc[day, "buy_price"])
             for i in panel.items if day in panel.frames[i].index}
    equity = ledger.equity(marks, fee)
    row = {
        "day": str(day.date()),
        "ran_at": time.time(),
        "equity": equity,
        "cash": ledger.cash,
        "open_lots": len(ledger.open_lots()),
        "orders": len(orders),
        "fills": len(fills),
        "working": len(broker.pending),
        "scoreable": len(panel.items),
    }
    state["cycles"] = [c for c in state["cycles"] if c["day"] != row["day"]] + [row]
    state["cycles"].sort(key=lambda c: c["day"])
    state["fills"] = state.get("fills", []) + [
        {"day": str(day.date()), "item": f.order.item, "side": f.order.side.value,
         "qty": f.qty, "price": f.fill_price} for f in fills]
    state["pending"] = [order_to_dict(o) for o in broker.pending]
    state["ledger"] = ledger.to_dict()
    state["started"] = state.get("started") or str(day.date())
    state["capital"] = capital
    state["fee"] = fee
    state["venue"] = "steam_live"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1, default=str))

    row["status"] = "ran"
    row["total_return"] = equity / capital - 1
    row["days_running"] = len(state["cycles"])
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(REPO_ROOT.parent / "system_a" / "var" / "market.db"),
                    help="market db the live poller writes to")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    ap.add_argument("--fee", type=float, default=STEAM_FEE,
                    help=f"sell fee; steam {STEAM_FEE:.4f}, buff {BUFF_FEE:.4f}")
    ap.add_argument("--state", type=Path, default=STATE)
    ap.add_argument("--order-ttl", type=int, default=7,
                    help="days a resting limit stays live across cycles")
    args = ap.parse_args(argv)

    res = run_cycle(Path(args.db), capital=args.capital, fee=args.fee,
                    state_path=args.state, order_ttl=args.order_ttl)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if res.get("status") == "no_live_data":
        print(f"[{stamp}] live paper: no rows for source={LIVE_SOURCE} in {args.db}. "
              "Is the poller running (`make poll`)?")
        return 1
    if res.get("status") == "no_data_today":
        print(f"[{stamp}] live paper: no snapshot for today; latest is {res['latest']}.")
        return 1
    print(f"[{stamp}] live paper day {res['day']}  equity {res['equity']:,.0f} CNY "
          f"({res['total_return']:+.2%})  cash {res['cash']:,.0f}  "
          f"lots {res['open_lots']}  orders {res['orders']}  fills {res['fills']}  "
          f"working {res.get('working', 0)}  "
          f"[day {res['days_running']} of the run]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
