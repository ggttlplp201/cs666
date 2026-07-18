"""Market data panel: normalized per-item daily history + no-lookahead views.

The strategy only ever receives a `PanelView` produced by `panel.up_to(day)`,
which hard-truncates every series at the decision day — look-ahead bias is
prevented structurally, not by convention (System B §9 pitfalls).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from .schema import ItemMeta

# Canonical per-item daily columns (schema §2.3). volume = EXECUTED trades.
PANEL_COLUMNS = [
    "sell_price",
    "buy_price",
    "listing_count",
    "buy_order_count",
    "volume",
    "valid_buy_orders",
]


def _validate_frame(name: str, df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in PANEL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")
    df = df.copy()
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index)).as_unit("us").rename("day")
    df = df.sort_index()
    if df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")]
    return df


@dataclass
class MarketPanel:
    """All items' normalized daily history + structural metadata."""

    frames: dict[str, pd.DataFrame]
    meta: dict[str, ItemMeta] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.frames = {k: _validate_frame(k, v) for k, v in self.frames.items()}

    @property
    def items(self) -> list[str]:
        return list(self.frames)

    def calendar(self) -> pd.DatetimeIndex:
        idx = pd.DatetimeIndex([])
        for df in self.frames.values():
            idx = idx.union(df.index)
        return idx

    def up_to(self, day: date | pd.Timestamp) -> "PanelView":
        return PanelView(self, pd.Timestamp(day))

    # -- persistence: one CSV per item + meta.csv -------------------------------
    def save(self, root: Path) -> None:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for name, df in self.frames.items():
            df.to_csv(root / f"{_slug(name)}.csv", index_label="day")
        if self.meta:
            rows = []
            for m in self.meta.values():
                d = m.__dict__.copy()
                d["source_status"] = m.source_status.value
                rows.append(d)
            pd.DataFrame(rows).to_csv(root / "meta.csv", index=False)

    @classmethod
    def load(cls, root: Path) -> "MarketPanel":
        from .schema import SourceStatus

        root = Path(root)
        meta: dict[str, ItemMeta] = {}
        meta_path = root / "meta.csv"
        if meta_path.exists():
            for _, row in pd.read_csv(meta_path).iterrows():
                d = row.to_dict()
                d["source_status"] = SourceStatus(d.get("source_status", "active"))
                d = {k: v for k, v in d.items() if k in ItemMeta.__dataclass_fields__}
                m = ItemMeta(**d)
                meta[m.market_hash_name] = m
        frames = {}
        slug_to_name = {_slug(n): n for n in meta}
        for p in sorted(root.glob("*.csv")):
            if p.name == "meta.csv":
                continue
            df = pd.read_csv(p, index_col="day", parse_dates=True)
            name = slug_to_name.get(p.stem, p.stem)
            frames[name] = df
        return cls(frames=frames, meta=meta)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


class PanelView:
    """Truncated, read-only view of the panel at a decision day.

    Every accessor returns data with index <= self.day. This is the ONLY
    surface strategies/features may read market data through.
    """

    def __init__(self, panel: MarketPanel, day: pd.Timestamp):
        self._panel = panel
        self.day = day.normalize()

    @property
    def items(self) -> list[str]:
        return self._panel.items

    @property
    def meta(self) -> Mapping[str, ItemMeta]:
        """KNOWN LIMITATION (review 2026-07-17): metadata is a single static
        snapshot with no as-of dimension — a backtest over a collected panel
        scores past days with TODAY'S source_status/supply/case_price. Fine for
        synthetic data (meta is ground truth there); for real panels the
        collector snapshots meta history so a future as-of store can close this.
        Until then, treat supply-outlook factor performance on real history as
        upper-bounded."""
        return self._panel.meta

    def history(self, item: str, window: int | None = None) -> pd.DataFrame:
        df = self._panel.frames.get(item)
        if df is None:
            return pd.DataFrame(columns=PANEL_COLUMNS)
        out = df.loc[: self.day]
        if window is not None:
            out = out.tail(window)
        return out

    def today(self, item: str) -> pd.Series | None:
        """Latest row at/before the decision day; None if item has no data yet
        or its last observation is stale (> 3 days old)."""
        df = self.history(item)
        if df.empty:
            return None
        last = df.index[-1]
        if (self.day - last).days > 3:
            return None
        return df.iloc[-1]

    def active_items(self) -> list[str]:
        return [i for i in self.items if self.today(i) is not None]

    def cross_section(self, column: str, window: int = 1) -> pd.DataFrame:
        """date x item frame of one column over a trailing window (for regime/breadth)."""
        cols = {}
        for i in self.items:
            h = self.history(i)
            if not h.empty:
                cols[i] = h[column].tail(window)
        return pd.DataFrame(cols)


def panel_from_records(records: Iterable, meta: Iterable[ItemMeta] = ()) -> MarketPanel:
    """Build a panel from ItemDay records (e.g. out of a vendor normalizer)."""
    by_item: dict[str, list[dict]] = {}
    for r in records:
        by_item.setdefault(r.market_hash_name, []).append(
            {
                "day": pd.Timestamp(r.day),
                "sell_price": r.sell_price,
                "buy_price": r.buy_price,
                "listing_count": r.listing_count,
                "buy_order_count": r.buy_order_count,
                "volume": r.volume,
                "valid_buy_orders": r.valid_buy_orders,
            }
        )
    frames = {
        k: pd.DataFrame(v).set_index("day").sort_index() for k, v in by_item.items()
    }
    return MarketPanel(frames=frames, meta={m.market_hash_name: m for m in meta})
