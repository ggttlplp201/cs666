"""SQLite snapshot store.

Persists normalized Item snapshots so detectors have history to subtract
against (docs/System-A §2.3). SQLite keeps paper mode dependency-free; the
schema is flat enough to lift into Timescale/Postgres later.

Every row carries a `source` tag ("buff" for the live cs2.sh feed, "steam"
for the Phase-1 Steam price-history backfill — docs Shared §2a). Reads
default to source="buff" so Steam data can never silently leak into live
BUFF signals; backtest code opts into source="steam" explicitly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shared.schema import Item

DEFAULT_SOURCE = "buff"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    market_hash_name TEXT NOT NULL,
    ts REAL NOT NULL,
    lowest_sell REAL NOT NULL,
    highest_buy REAL NOT NULL,
    listing_count INTEGER NOT NULL,
    buy_order_count INTEGER NOT NULL,
    volume_24h INTEGER,
    variant TEXT,
    source TEXT NOT NULL DEFAULT 'buff',
    PRIMARY KEY (market_hash_name, ts, source)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);
"""


class SnapshotStore:
    def __init__(self, path: Path | str = ":memory:"):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)

    def insert(self, items: list[Item], source: str = DEFAULT_SOURCE) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    i.market_hash_name, i.ts, i.buff_lowest_sell_cny,
                    i.buff_highest_buy_cny, i.buff_listing_count,
                    i.buff_buy_order_count, i.buff_volume_24h, i.variant, source,
                )
                for i in items
            ],
        )
        self.conn.commit()

    def series(
        self, name: str, since_ts: float = 0.0, source: str = DEFAULT_SOURCE
    ) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots WHERE market_hash_name = ? AND ts >= ?"
            " AND source = ? ORDER BY ts",
            (name, since_ts, source),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def latest(self, source: str = DEFAULT_SOURCE) -> dict[str, Item]:
        rows = self.conn.execute(
            """SELECT s.* FROM snapshots s
               JOIN (SELECT market_hash_name, MAX(ts) AS mts FROM snapshots
                     WHERE source = ? GROUP BY market_hash_name) m
               ON s.market_hash_name = m.market_hash_name AND s.ts = m.mts
               WHERE s.source = ?""",
            (source, source),
        ).fetchall()
        return {r[0]: _row_to_item(r) for r in rows}

    def last_ts(self, source: str = DEFAULT_SOURCE) -> float | None:
        row = self.conn.execute(
            "SELECT MAX(ts) FROM snapshots WHERE source = ?", (source,)
        ).fetchone()
        return row[0]

    def is_stale(
        self, now_ts: float, max_age_seconds: float, source: str = DEFAULT_SOURCE
    ) -> bool:
        """True when there is no data or the newest snapshot is too old —
        callers must pause trading (config: data.pause_trading_on_stale_or_divergent)."""
        last = self.last_ts(source)
        return last is None or (now_ts - last) > max_age_seconds

    def gap_report(
        self, source: str, expected_seconds: float, tolerance_factor: float = 2.5
    ) -> list[tuple[float, float, float]]:
        """Data-sanity check: holes in the snapshot cadence. Returns
        (gap_start_ts, gap_end_ts, gap_seconds) for every inter-snapshot gap
        exceeding tolerance_factor × expected_seconds. A series with silent
        holes is worse than no series — callers must surface these."""
        rows = self.conn.execute(
            "SELECT DISTINCT ts FROM snapshots WHERE source = ? ORDER BY ts",
            (source,),
        ).fetchall()
        gaps = []
        threshold = tolerance_factor * expected_seconds
        for (prev,), (curr,) in zip(rows, rows[1:]):
            if curr - prev > threshold:
                gaps.append((prev, curr, curr - prev))
        return gaps

    def counts_by_source(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) FROM snapshots GROUP BY source"
        ).fetchall()
        return dict(rows)


def _row_to_item(row: tuple) -> Item:
    return Item(
        market_hash_name=row[0],
        ts=row[1],
        buff_lowest_sell_cny=row[2],
        buff_highest_buy_cny=row[3],
        buff_listing_count=row[4],
        buff_buy_order_count=row[5],
        buff_volume_24h=row[6],
        variant=row[7],
    )
