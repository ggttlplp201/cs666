"""iflow.work Datadump ingestion — FREE historical BUFF bid/ask + ladders.

Source (docs Shared §2a; verified live 2026-07-19): the retired 挂刀 tracker
iflow.work open-sourced its snapshot archive. Priority Archive: keyless,
twice-daily snapshots 2022-04-18 → 2026-05-20, ~3-12k items each, JSON Lines
in zips. Prices are USD-normalized on both eras (verified: BUFF/Steam ratios
~0.78-0.93, consistent with 挂刀 economics).

Two schema eras, both handled:
  OLD (≈2022-2023): buff_sell_list [[price, float*1000, ?], ...],
      buff_sell_num / buff_buy_num counts, no BUFF bid price
      (buy_order_list is STEAM bids — never used as BUFF).
  NEW (≈2024+): buff_sell/buff_buy {price, orders[], count} + meta_info,
      metrics, steam_volume.

Rows land in the snapshot store as source="buff_iflow" (USD — the *_cny
field names are venue-legacy; the source tag disambiguates, same convention
as source="steam"). Old-era rows have highest_buy=0.0 meaning "BUFF bid
unavailable", which spread_stats already filters out.

CLI (event-window mode: pulls files around every rules-table historical
event and ingests the tracked universe):
    PYTHONPATH=src python -m shared.iflow_history
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared.configuration import Config
from shared.schema import Item
from shared.store import SnapshotStore

USER_AGENT = "cs2-quant/0.1"
CN_TZ = timezone(timedelta(hours=8))   # archive filenames are UTC+8 (doc)


def file_ts(file_name: str) -> float:
    """'2026-03-19-00-15.zip' (UTC+8) → unix UTC."""
    stem = file_name.removesuffix(".zip")
    return datetime.strptime(stem, "%Y-%m-%d-%H-%M").replace(tzinfo=CN_TZ).timestamp()


def list_files(base_url: str, dir_name: str) -> list[str]:
    request = urllib.request.Request(
        f"{base_url}/list?dir_name={urllib.parse.quote(dir_name)}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        payload = json.load(resp)
    if not payload.get("success"):
        raise RuntimeError(f"iflow list failed: {payload}")
    return payload["files"]


def select_event_window_files(
    files: list[str],
    event_dates: list[str],
    pre_days: float,
    post_days: float,
    files_per_day: int = 1,
) -> list[str]:
    """Files within [event-pre, event+post] for any event, thinned to
    files_per_day (archive holds two per day)."""
    windows = []
    for date in event_dates:
        ts = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        windows.append((ts - pre_days * 86400, ts + post_days * 86400))
    picked: list[str] = []
    by_day: dict[str, int] = {}
    for name in files:
        try:
            ts = file_ts(name)
        except ValueError:
            continue
        if not any(lo <= ts <= hi for lo, hi in windows):
            continue
        day = name[:10]
        if by_day.get(day, 0) >= files_per_day:
            continue
        by_day[day] = by_day.get(day, 0) + 1
        picked.append(name)
    return picked


def fetch_file(
    base_url: str, dir_name: str, file_name: str, cache_dir: Path,
    gap_seconds: float, last_request: list[float],
) -> Path:
    cached = cache_dir / file_name
    if cached.exists() and cached.stat().st_size > 1000:
        return cached
    wait = gap_seconds - (time.time() - last_request[0])
    if wait > 0:
        time.sleep(wait)
    query = urllib.parse.urlencode({"dir_name": dir_name, "file_name": file_name})
    request = urllib.request.Request(
        f"{base_url}/download?{query}", headers={"User-Agent": USER_AGENT}
    )
    last_request[0] = time.time()
    with urllib.request.urlopen(request, timeout=120) as resp:
        data = resp.read()
    if len(data) < 1000 and b"success" in data[:200]:
        raise RuntimeError(f"iflow download failed for {file_name}: {data[:200]!r}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return cached


def parse_record(record: dict) -> tuple[float, float, int, int] | None:
    """(ask, bid, listing_count, buy_order_count) in USD, either schema.
    Returns None when the record has no usable BUFF ask."""
    buff_sell = record.get("buff_sell")
    if isinstance(buff_sell, dict):                      # NEW schema
        ask = buff_sell.get("price")
        if ask is None:
            return None
        buff_buy = record.get("buff_buy") or {}
        return (
            float(ask),
            float(buff_buy.get("price") or 0.0),
            int(buff_sell.get("count") or 0),
            int(buff_buy.get("count") or 0),
        )
    sell_list = record.get("buff_sell_list")             # OLD schema
    if sell_list:
        prices = [row[0] for row in sell_list if row and row[0] is not None]
        if not prices:
            return None
        return (
            float(min(prices)),
            0.0,   # OLD schema has no BUFF bid price — 0.0 = unavailable
            int(record.get("buff_sell_num") or 0),
            int(record.get("buff_buy_num") or 0),
        )
    return None


def ingest_file(path: Path, universe: set[str], store: SnapshotStore,
                source_tag: str) -> int:
    ts = file_ts(path.name)
    items = []
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in f:
                record = json.loads(line)
                if record.get("appid") != 730:
                    continue
                name = record.get("hash_name")
                if name not in universe:
                    continue
                parsed = parse_record(record)
                if parsed is None:
                    continue
                ask, bid, listings, bids = parsed
                items.append(
                    Item(
                        market_hash_name=name,
                        buff_lowest_sell_cny=ask,    # USD — see module docstring
                        buff_highest_buy_cny=bid,
                        buff_listing_count=listings,
                        buff_buy_order_count=bids,
                        buff_volume_24h=None,        # BUFF executed volume: not in archive
                        ts=ts,
                    )
                )
    if items:
        store.insert(items, source=source_tag)
    return len(items)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config.load(repo_root, system="system_a")   # rules table path lives there
    cfg = config.require("data.iflow_archive")
    parser = argparse.ArgumentParser(description="iflow archive ingestion (event windows)")
    parser.add_argument("--db", type=Path,
                        default=repo_root / config.require("data.snapshot_poller")["db_path"])
    parser.add_argument("--pre", type=float, default=cfg["event_window_pre_days"])
    parser.add_argument("--post", type=float, default=cfg["event_window_post_days"])
    args = parser.parse_args(argv)

    import yaml
    rules = yaml.safe_load(
        (repo_root / config.require("system_a.rules_table_path")).read_text()
    )
    event_dates = [
        str(e["date"]) for e in rules.get("historical_events", [])
        if "ACTIVE" not in str(e.get("status", ""))
    ]
    seed = repo_root / config.require("data.steam_history")["items_file"]
    universe = {l.strip() for l in seed.read_text().splitlines() if l.strip()}
    store = SnapshotStore(args.db)
    cache_dir = repo_root / cfg["cache_dir"]

    files = list_files(cfg["base_url"], cfg["dir_name"])
    picked = select_event_window_files(
        files, event_dates, args.pre, args.post, cfg["files_per_day"]
    )
    print(f"{len(files)} archive files; {len(picked)} in event windows "
          f"({len(event_dates)} events, -{args.pre:.0f}d..+{args.post:.0f}d)")
    last_request = [0.0]
    total_rows, failed = 0, []
    for i, name in enumerate(picked, 1):
        try:
            path = fetch_file(
                cfg["base_url"], cfg["dir_name"], name, cache_dir,
                cfg["request_gap_seconds"], last_request,
            )
            rows = ingest_file(path, universe, store, cfg["source_tag"])
            total_rows += rows
            if i % 25 == 0 or i == len(picked):
                print(f"[{i}/{len(picked)}] {name}: +{rows} rows "
                      f"(total {total_rows})")
        except Exception as e:
            failed.append(name)
            print(f"[{i}/{len(picked)}] {name}: ERROR {e}")
    print(f"\ningested {total_rows} rows as source='{cfg['source_tag']}'"
          f" into {args.db}")
    if failed:
        print(f"failed files ({len(failed)}): {', '.join(failed[:8])}"
              f"{'...' if len(failed) > 8 else ''}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
