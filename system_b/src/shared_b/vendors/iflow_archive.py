"""iflow archive → System B CSV panel: real BUFF daily history for the backtest.

Bridges Builder 1's iflow.work archive access (``shared.iflow_history`` —
list_files / fetch_file / file_ts, sharing the same ``var/iflow_archive``
download cache) into the CSV ``MarketPanel`` that
``python -m system_b.run_backtest --data-dir`` loads.

    PYTHONPATH=src python -m shared_b.vendors.iflow_archive --data-dir data/panel_iflow

VERIFIED PROPERTIES (probed 2026-07-27 against 9 sample files, 2022→2026):

- **Currency is CNY, not USD** — ``shared/iflow_history.py``'s "USD-normalized"
  note is wrong (harmless there: event returns and spread %% are scale-free).
  Anchors: AK-47 Redline FT 72.0 @2022-06 (USD then ≈ $12 → ¥72 ✓), AWP
  Asiimov FT 1018 @2023-06 (≈ $140 → ¥1000 ✓), Mecha MW bid 707 @2026-05 vs
  the course note "~¥780 as of 2026-07". Prices land in the panel as CNY —
  do NOT apply an FX conversion on top.
- **OLD era (2022-2023) has no BUFF bid** — sells/marks would be fiction, so
  the panel DEFAULTS to the NEW era (2024-01-01+), which carries
  buff_sell/buff_buy price, 10-level price ladders, counts, steam_volume.
  OLD-schema records parse to None here by design.
- **Coverage flickers**: the NEW-era tracker follows only ~2.5-3.5k CS2 items
  per snapshot and universe items drop in and out day to day. Missing days
  stay missing (no fill-forward); PanelView.today() treats >3d-old as stale.
- **volume is a STEAM proxy**: the archive has NO BUFF executed-trade count.
  ``volume`` = steam_volume.volume (Steam 24h sold) — same caveat class as
  the retired cs2.sh aggregate proxy. Thresholds calibrated for BUFF volume
  (selection_filters.min_daily_trades) read TIGHT against it; check the
  avg_volume rejection share in the run journal before trusting a thin run.
- **valid_buy_orders** = entries of the buff_buy 10-level ladder priced
  within ``valid_bid_band_pct`` of the best ask (docs Shared §4.3: "bids
  near market, not lowballs"; the worked example is a 2-5%% band → default
  0.05). Ladder is truncated at 10, so the count saturates there — the
  ≥3-valid-bids filter is unaffected.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from shared.iflow_history import fetch_file, file_ts, list_files

from ..config import REPO_ROOT, load_config
from ..data import MarketPanel, PANEL_COLUMNS

# NEW-schema snapshots (buff_sell/buff_buy dicts, -00-15/-12-15 filenames)
# begin 2024-02-13; before that the archive is OLD-schema (no BUFF bid).
DEFAULT_START = "2024-02-13"


def parse_new_record(record: dict, valid_bid_band_pct: float = 0.05) -> dict | None:
    """NEW-schema (2024+) record → one panel row dict; None if unusable.

    Requires both a BUFF ask and bid: without an ask there is no price series,
    without a bid there is no honest mark/exit. OLD-schema records (no
    buff_sell dict) return None — see module docstring.
    """
    buff_sell = record.get("buff_sell")
    buff_buy = record.get("buff_buy")
    if not isinstance(buff_sell, dict) or not isinstance(buff_buy, dict):
        return None
    ask, bid = buff_sell.get("price"), buff_buy.get("price")
    if not ask or not bid or ask <= 0 or bid <= 0:
        return None
    ladder = [p for p in (buff_buy.get("orders") or []) if isinstance(p, (int, float))]
    valid = sum(1 for p in ladder if p >= ask * (1 - valid_bid_band_pct))
    steam = record.get("steam_volume") or {}
    vol = steam.get("volume")
    return {
        "sell_price": float(ask),
        "buy_price": float(bid),
        "listing_count": int(buff_sell.get("count") or 0),
        "buy_order_count": int(buff_buy.get("count") or 0),
        "volume": int(vol) if vol is not None else 0,   # STEAM 24h proxy; 0 = unknown
        "valid_buy_orders": int(valid),
    }


def ingest_snapshot(
    path: Path,
    universe: set[str],
    rows: dict[tuple[str, date], dict],
    valid_bid_band_pct: float,
) -> int:
    """Merge one archive zip into ``rows`` keyed (item, day).

    Called in chronological file order, so with 2 snapshots/day the later
    parseable record wins (closer to end-of-day book state).

    The day is the CN calendar day straight from the filename (UTC+8) —
    BUFF's venue-local day, so both same-day snapshots merge into one bar."""
    day = datetime.strptime(path.name.removesuffix(".zip"), "%Y-%m-%d-%H-%M").date()
    n = 0
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in f:
                record = json.loads(line)
                if record.get("appid") != 730:
                    continue
                name = record.get("hash_name")
                if name not in universe:
                    continue
                parsed = parse_new_record(record, valid_bid_band_pct)
                if parsed is None:
                    continue
                rows[(name, day)] = parsed
                n += 1
    return n


def build_panel(rows: dict[tuple[str, date], dict], meta: dict) -> MarketPanel:
    by_item: dict[str, dict[pd.Timestamp, dict]] = {}
    for (name, day), row in rows.items():
        by_item.setdefault(name, {})[pd.Timestamp(day)] = row
    frames = {
        name: pd.DataFrame.from_dict(days, orient="index")[PANEL_COLUMNS].sort_index()
        for name, days in by_item.items()
    }
    return MarketPanel(frames=frames, meta=meta)


def coverage_report(panel: MarketPanel) -> pd.DataFrame:
    recs = []
    for name, df in panel.frames.items():
        span_days = (df.index[-1] - df.index[0]).days + 1 if len(df) else 0
        recs.append({
            "item": name,
            "rows": len(df),
            "first": str(df.index[0].date()) if len(df) else None,
            "last": str(df.index[-1].date()) if len(df) else None,
            "presence": round(len(df) / span_days, 3) if span_days else 0.0,
            "vol_median": float(df["volume"].median()) if len(df) else None,
            "zero_vol_share": round(float((df["volume"] == 0).mean()), 3) if len(df) else None,
        })
    return pd.DataFrame(recs).sort_values("rows", ascending=False)


def main(argv: list[str] | None = None) -> int:
    cfg = load_config("b")
    arc = cfg.at("data.iflow_archive")
    if not arc:
        raise SystemExit("config data.iflow_archive missing from config/shared.yaml")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data" / "panel_iflow")
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=None)
    ap.add_argument("--files-per-day", type=int, default=2,
                    help="archive holds 2 snapshots/day; 2 = use both (later wins)")
    ap.add_argument("--valid-bid-band-pct", type=float, default=0.05)
    ap.add_argument("--universe", type=str, default=None,
                    help="universe yaml to build the panel for, relative to the "
                         "repo root (default: config's universe.universe_path). "
                         "Use config/universe_b_draft.yaml for the 97-item screen.")
    args = ap.parse_args(argv)

    from system_b.universe import load_universe

    uni_path = args.universe or cfg.at("universe.universe_path", "config/universe_b.yaml")
    uni = load_universe(REPO_ROOT / uni_path)
    if not uni:
        raise SystemExit(f"universe is empty — fill {uni_path} first")
    universe = set(uni)
    print(f"universe: {len(universe)} items from {uni_path}")

    if pd.Timestamp(args.start) < pd.Timestamp(DEFAULT_START):
        print(f"WARNING: --start {args.start} predates {DEFAULT_START} (NEW-schema era); the OLD "
              "archive era has no BUFF bid and yields no rows here.")

    start_ts = pd.Timestamp(args.start, tz="UTC").timestamp()
    end_ts = (pd.Timestamp(args.end, tz="UTC").timestamp() + 86400
              if args.end else float("inf"))

    files = []
    per_day: dict[str, int] = {}
    for name in sorted(list_files(arc["base_url"], arc["dir_name"])):
        try:
            ts = file_ts(name)
        except ValueError:
            continue
        if not (start_ts <= ts < end_ts):
            continue
        day = name[:10]
        if per_day.get(day, 0) >= args.files_per_day:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        files.append(name)
    print(f"{len(files)} archive files in range {args.start}..{args.end or 'latest'} "
          f"({args.files_per_day}/day)")

    cache_dir = REPO_ROOT / arc["cache_dir"]
    gap = float(arc.get("request_gap_seconds", 1.0))
    rows: dict[tuple[str, date], dict] = {}
    last_request = [0.0]
    failed: list[str] = []
    for i, name in enumerate(sorted(files), 1):
        try:
            path = fetch_file(arc["base_url"], arc["dir_name"], name, cache_dir,
                              gap, last_request)
            ingest_snapshot(path, universe, rows, args.valid_bid_band_pct)
        except Exception as e:                                    # noqa: BLE001
            failed.append(name)
            print(f"[{i}/{len(files)}] {name}: ERROR {e}")
        if i % 50 == 0 or i == len(files):
            print(f"[{i}/{len(files)}] {name}: {len(rows)} item-day rows so far")

    panel = build_panel(rows, meta=uni)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    panel.save(args.data_dir)
    report = coverage_report(panel)
    # .txt, not .csv: MarketPanel.load treats every *.csv in the dir as an item
    report.to_csv(args.data_dir / "coverage.txt", index=False)
    print(f"\npanel -> {args.data_dir}  ({len(panel.frames)}/{len(universe)} "
          f"universe items have data; volume = STEAM 24h proxy, prices CNY)")
    print(report.to_string(index=False))
    missing = sorted(universe - set(panel.frames))
    if missing:
        print(f"\nno archive data at all for: {missing}")
    if failed:
        print(f"\nfailed files ({len(failed)}): {', '.join(failed[:8])}"
              f"{'...' if len(failed) > 8 else ''}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
