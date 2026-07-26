"""Real-history panel builder — turns collected snapshots into a `MarketPanel`.

WHY A JOIN. Neither collected source alone satisfies System B's inputs:

  * ``buff_iflow`` — real BUFF book (lowest ask, highest bid, listing depth,
    bid depth) on the correct venue, but ``volume_24h`` is NULL throughout.
  * ``steam`` — real executed volume every day back to 2013, but no book at
    all: ``lowest_sell == highest_buy`` and both depth columns are 0.

System B's hard filters gate on bid depth *and* on 20-day average trades
(`system_b/filters.py`), and both are safety gates the allowlist may not
bypass — so a single-source panel is rejected wholesale, every item, every
day. The join takes price/book from BUFF and executed volume from Steam.

CAVEAT — carry this into any writeup. Steam executed volume is a *proxy* for
BUFF executed volume, not the same quantity: the venues share the underlying
item demand but differ in fee, audience and settlement. Volume-derived
factors (whale accumulation, volume-without-price) are therefore directional
evidence here, not calibrated ones.

SECOND CAVEAT. The iflow archive is not dense: over its longest contiguous
block it carries a median of ~7 of 20 items per day. Gaps are bridged by a
bounded forward-fill (`--max-stale-days`, default 3) of the *book* only;
volume is never filled. A carried book means flat price + flat listings by
construction, which is itself part of the accumulation pattern — so treat
accumulation signals on filled days with suspicion. Build-time output reports
the exact fill rate.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from .config import REPO_ROOT
from .data import MarketPanel
from .schema import ItemMeta, SourceStatus

BOOK_SOURCE = "buff_iflow"
VOLUME_SOURCE = "steam"

# Each source's rows are stamped as UTC epochs, but they mean days in DIFFERENT
# calendars, so the day label must be derived in the source's own zone:
#   * iflow archive filenames are UTC+8 (shared/iflow_history.py CN_TZ), so a
#     "2026-03-19-04-00" file is 2026-03-18T20:00Z. Flooring that in UTC labels
#     it 2026-03-18 — a day EARLY. 99% of archive rows land in that window, so
#     naive UTC flooring puts tomorrow's BUFF book on today's row: a look-ahead
#     leak, not a cosmetic off-by-one.
#   * Steam rows are already midnight-UTC of their intended day
#     (shared/steam_history.py), so UTC is correct for them.
SOURCE_TZ = {BOOK_SOURCE: "Asia/Shanghai", VOLUME_SOURCE: "UTC"}

# Weapons System B treats as the primary tier (Shared §4.2).
PRIMARY = {"AK-47", "M4A4", "M4A1-S", "USP-S", "Glock-18", "AWP"}
SECONDARY_PRIMARY = {"Galil AR", "FAMAS", "SG 553", "AUG", "SSG 08"}


def _read(db: Path, source: str) -> pd.DataFrame:
    with sqlite3.connect(str(db)) as con:
        df = pd.read_sql(
            "SELECT market_hash_name, ts, lowest_sell, highest_buy, "
            "listing_count, buy_order_count, volume_24h "
            "FROM snapshots WHERE source = ?",
            con,
            params=(source,),
        )
    if df.empty:
        return df
    tz = SOURCE_TZ.get(source, "UTC")
    df["day"] = (
        pd.to_datetime(df["ts"], unit="s", utc=True)
        .dt.tz_convert(tz)
        .dt.tz_localize(None)
        .dt.floor("D")
    )
    # several intraday rows can share a day — keep the last observation
    df = df.sort_values("ts").drop_duplicates(["market_hash_name", "day"], keep="last")
    return df


def _valid_book(df: pd.DataFrame) -> pd.DataFrame:
    """Drop book rows that cannot be traded against.

    Two defects are present in the real archive and both would flatter a
    backtest if left in:

    * ``highest_buy == 0`` is the OLD iflow schema's "BUFF bid unavailable"
      sentinel (shared/iflow_history.py), not a real bid of zero. Marking or
      exiting a position at a zero bid is meaningless, and the row still
      clears the depth gate because `valid_buy_orders` falls back to
      `buy_order_count`.
    * ``highest_buy > lowest_sell`` is a crossed book — impossible at a single
      instant on a real venue, so the two sides were sampled at different
      times. Buying the ask and selling the bid on such a row is a risk-free
      profit that does not exist.
    """
    ok = (df["lowest_sell"] > 0) & (df["highest_buy"] > 0)
    ok &= df["highest_buy"] <= df["lowest_sell"]
    return df[ok]


def contiguous_blocks(
    days: pd.DatetimeIndex, max_gap_days: int = 1
) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Split a day index into runs separated by gaps longer than `max_gap_days`.

    `max_gap_days=1` means strictly consecutive days. Anything larger tolerates
    a sparser archive: an every-other-day feed is one block, not N blocks of
    one day each — which is what strict contiguity would (wrongly) report.
    """
    d = pd.DatetimeIndex(sorted(set(days)))
    if len(d) == 0:
        return []
    brk = (d.to_series().diff().dt.days.fillna(1) > max_gap_days).cumsum()
    out = []
    for _, g in d.to_series().groupby(brk):
        out.append((g.iloc[0], g.iloc[-1], len(g)))
    return out


def derive_meta(names: list[str]) -> dict[str, ItemMeta]:
    """Best-effort structural metadata for collected items.

    `supply`, `case_price_cny` and `aesthetics` are human/vendor-supplied
    (HANDOFF §B) and are NOT available for these items, so they stay at their
    unknown defaults. That makes every item fail the structural hard filters
    (`supply_unknown`, `case_price<80`); the real-data run therefore relies on
    `risk_controls.allowlist` to waive those *structural* gates while leaving
    the safety gates (depth, volume, pump shape) fully enforced.
    """
    meta: dict[str, ItemMeta] = {}
    for name in names:
        weapon = name.split("|", 1)[0].strip() if "|" in name else ""
        meta[name] = ItemMeta(
            market_hash_name=name,
            weapon=weapon,
            category="mid_tier_primary" if weapon in PRIMARY else "small_item",
            source_status=SourceStatus.ACTIVE,
            is_primary=weapon in PRIMARY,
            is_secondary_primary=weapon in SECONDARY_PRIMARY,
            aesthetics=0.5,  # neutral — no human ranking supplied for these items
            notes="collected panel; supply/case_price/aesthetics unknown",
        )
    return meta


def build_panel(
    db_path: Path,
    start: str | None = None,
    end: str | None = None,
    max_stale_days: int = 3,
    min_items_per_day: int = 3,
    max_gap_days: int = 7,
) -> tuple[MarketPanel, dict]:
    """Join book (BUFF) + volume (Steam) into a panel. Returns (panel, stats).

    `max_gap_days` only picks the default window: the longest stretch of book
    history not interrupted by a gap that large. It never bridges the real
    archive's two multi-month holes.
    """
    book = _read(Path(db_path), BOOK_SOURCE)
    vol = _read(Path(db_path), VOLUME_SOURCE)
    if book.empty:
        raise SystemExit(f"no '{BOOK_SOURCE}' rows in {db_path}")
    if vol.empty:
        raise SystemExit(f"no '{VOLUME_SOURCE}' rows in {db_path}")

    n_book_raw = len(book)
    book = _valid_book(book)
    n_book_dropped = n_book_raw - len(book)
    if book.empty:
        raise SystemExit(f"no usable '{BOOK_SOURCE}' rows after book validation")

    # default window: the longest contiguous run of book observations
    if start is None or end is None:
        blocks = contiguous_blocks(pd.DatetimeIndex(book["day"]), max_gap_days=max_gap_days)
        lo, hi, _ = max(blocks, key=lambda b: b[2])
        start = start or str(lo.date())
        end = end or str(hi.date())
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    book = book[(book["day"] >= lo) & (book["day"] <= hi)]

    # only items priced on BOTH venues — Steam supplies the volume column
    items = sorted(set(book["market_hash_name"]) & set(vol["market_hash_name"]))
    calendar = pd.date_range(lo, hi, freq="D")

    frames: dict[str, pd.DataFrame] = {}
    n_real = n_filled = 0
    observed_by_item: dict[str, pd.DatetimeIndex] = {}
    for item in items:
        b = (
            book[book["market_hash_name"] == item]
            .set_index("day")[["lowest_sell", "highest_buy", "listing_count", "buy_order_count"]]
            .sort_index()
        )
        if b.empty:
            continue
        observed = b.index
        # bounded carry of the book across gaps; never invented before first obs.
        # max_stale_days=0 disables carrying entirely (observations only).
        b = b.reindex(calendar)
        if max_stale_days > 0:
            b = b.ffill(limit=max_stale_days)
        b = b[b.index >= observed.min()].dropna()
        if b.empty:
            continue

        v = (
            vol[vol["market_hash_name"] == item]
            .set_index("day")["volume_24h"]
            .sort_index()
            .reindex(b.index)
        )

        df = pd.DataFrame(
            {
                "sell_price": b["lowest_sell"].astype(float),
                "buy_price": b["highest_buy"].astype(float),
                "listing_count": b["listing_count"].astype(int),
                "buy_order_count": b["buy_order_count"].astype(int),
                # Steam executed trades — the volume proxy (see module docstring)
                "volume": v.astype(float),
                # bids *near market* are not recoverable from stored depth;
                # -1 is the schema's documented "unknown" sentinel, which makes
                # the filter fall back to buy_order_count.
                "valid_buy_orders": -1,
            },
            index=b.index,
        ).dropna(subset=["volume"])
        if df.empty:
            continue

        observed_by_item[item] = observed
        frames[item] = df

    # drop days whose cross-section is too thin to rank
    counts: dict[pd.Timestamp, int] = {}
    for df in frames.values():
        for d in df.index:
            counts[d] = counts.get(d, 0) + 1
    thin = {d for d, n in counts.items() if n < min_items_per_day}
    if thin:
        frames = {k: v.drop(index=[d for d in v.index if d in thin]) for k, v in frames.items()}
        frames = {k: v for k, v in frames.items() if not v.empty}

    # stats describe the SAVED panel, so they are computed after pruning —
    # reporting a fill rate for rows that were then dropped would overstate
    # how much of the panel is real.
    for item, df in frames.items():
        obs = observed_by_item[item]
        n_real += int(df.index.isin(obs).sum())
        n_filled += int((~df.index.isin(obs)).sum())
    kept_counts = {d: n for d, n in counts.items() if d not in thin}

    panel = MarketPanel(frames=frames, meta=derive_meta(list(frames)))
    kept = sorted({d for df in frames.values() for d in df.index})
    stats = {
        "window": [str(lo.date()), str(hi.date())],
        "items": len(frames),
        "days": len(kept),
        "rows": int(sum(len(df) for df in frames.values())),
        "observed_rows": n_real,
        "filled_rows": n_filled,
        "invalid_book_rows_dropped": n_book_dropped,
        "fill_rate": round(n_filled / max(n_real + n_filled, 1), 3),
        "median_items_per_day": float(pd.Series(list(kept_counts.values())).median())
        if kept_counts else 0.0,
        "book_source": BOOK_SOURCE,
        "volume_source": VOLUME_SOURCE,
    }
    return panel, stats


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description="Build a real BUFF panel from collected snapshots.")
    # anchored to the system folder, not the cwd — the rest of System B resolves
    # paths through REPO_ROOT, and a cwd-relative default silently reads/writes
    # the wrong var/ when invoked from the repo root.
    ap.add_argument("--db", default=str(REPO_ROOT / "var" / "market.db"))
    ap.add_argument("--out", default=str(REPO_ROOT / "var" / "panel_real"))
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--max-stale-days", type=int, default=3)
    ap.add_argument("--min-items-per-day", type=int, default=3)
    ap.add_argument("--max-gap-days", type=int, default=7,
                    help="gap that ends the auto-selected window (default 7)")
    args = ap.parse_args(argv)

    panel, stats = build_panel(
        Path(args.db),
        start=args.start,
        end=args.end,
        max_stale_days=args.max_stale_days,
        min_items_per_day=args.min_items_per_day,
        max_gap_days=args.max_gap_days,
    )
    out = Path(args.out)
    panel.save(out)
    print(f"panel -> {out.resolve()}")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["fill_rate"] > 0.5:
        print("  WARNING: majority of rows are carried-forward book, not observations")
    return stats


if __name__ == "__main__":
    main()
