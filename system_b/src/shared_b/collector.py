"""Daily snapshot collector — the forward self-collection path.

Vendor research (2026-07-17) found NO provider selling historical BUFF163
executed volume or listing counts; accumulation-signal history must accrue
forward from our own daily snapshots. Run this once per day (cron) as soon as
a cs2.sh key exists; every day it runs, the factor model gets one day of real
signal history.

    python -m shared.collector --data-dir data/panel
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import REPO_ROOT, load_config
from .data import MarketPanel, PANEL_COLUMNS
from .schema import ItemDay


def append_snapshot(data_dir: Path, records: list[ItemDay]) -> int:
    """Append one day of normalized records to the CSV panel (idempotent per day)."""
    panel = MarketPanel.load(data_dir) if (data_dir / "meta.csv").exists() or any(
        data_dir.glob("*.csv")) else MarketPanel(frames={})
    n = 0
    for r in records:
        row = pd.DataFrame(
            [{
                "sell_price": r.sell_price,
                "buy_price": r.buy_price,
                "listing_count": r.listing_count,
                "buy_order_count": r.buy_order_count,
                "volume": r.volume,
                "valid_buy_orders": r.valid_buy_orders,
            }],
            index=pd.DatetimeIndex([pd.Timestamp(r.day)], name="day"),
        )
        old = panel.frames.get(r.market_hash_name)
        merged = row if old is None else pd.concat([old, row])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        panel.frames[r.market_hash_name] = merged[PANEL_COLUMNS]
        n += 1
    panel.save(data_dir)
    return n


def snapshot_meta(data_dir: Path, meta_map: dict) -> None:
    """Append today's structural metadata to meta_history.jsonl — the as-of
    record that future backtests need (PanelView.meta is otherwise a static
    end-of-history snapshot; see data.PanelView.meta)."""
    import json

    path = Path(data_dir) / "meta_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().date().isoformat()
    with open(path, "a", encoding="utf-8") as f:
        for m in meta_map.values():
            rec = m.__dict__.copy()
            rec["source_status"] = m.source_status.value
            rec["snapshot_day"] = stamp
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=str, default=str(REPO_ROOT / "data" / "panel"))
    args = ap.parse_args(argv)

    from system_b.universe import load_universe
    from .vendors.cs2sh import Cs2ShClient

    cfg = load_config("b")
    uni = load_universe(REPO_ROOT / cfg.at("universe.universe_path", "config/universe_b.yaml"))
    items = list(uni)
    if not items:
        raise SystemExit("universe is empty — fill config/universe_b.yaml first")
    client = Cs2ShClient()
    records = client.snapshot(items)
    n = append_snapshot(Path(args.data_dir), records)
    snapshot_meta(Path(args.data_dir), uni)
    print(f"{datetime.now().isoformat()} collected {n}/{len(items)} items -> {args.data_dir}")


if __name__ == "__main__":
    main()
