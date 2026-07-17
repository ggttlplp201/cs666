"""Market-data feeds.

Cs2shFeed is the live BUFF feed (docs/System-A §2.2). Its field mapping is an
ASSUMPTION until Leon confirms cs2.sh's actual response shape (HANDOFF §0) —
edit `CS2SH_FIELD_MAP` when the real docs arrive. With a PLACEHOLDER key the
feed reports itself unavailable and callers must pause trading rather than
run on stale/no data (Shared §12 / config data.pause_trading_on_stale_or_divergent).

ReplayFeed serves recorded/synthetic snapshots for backtests and paper demos.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Iterator, Protocol

from shared.configuration import secret
from shared.schema import Item


class FeedUnavailable(RuntimeError):
    pass


class Feed(Protocol):
    def fetch(self) -> list[Item]:
        """Return the current snapshot for all tracked items."""
        ...


# Assumed cs2.sh JSON field names → our schema. TODO(Leon): confirm real names.
CS2SH_FIELD_MAP = {
    "market_hash_name": "market_hash_name",
    "buff_lowest_sell_cny": "sell_min_price",
    "buff_highest_buy_cny": "buy_max_price",
    "buff_listing_count": "sell_num",
    "buff_buy_order_count": "buy_num",
    "buff_volume_24h": "transacted_num_24h",
}


def normalize_cs2sh(raw: dict, ts: float | None = None) -> Item:
    """Map one cs2.sh item payload onto the Shared §2.3 schema."""
    return Item(
        market_hash_name=str(raw[CS2SH_FIELD_MAP["market_hash_name"]]),
        buff_lowest_sell_cny=float(raw[CS2SH_FIELD_MAP["buff_lowest_sell_cny"]]),
        buff_highest_buy_cny=float(raw[CS2SH_FIELD_MAP["buff_highest_buy_cny"]]),
        buff_listing_count=int(raw[CS2SH_FIELD_MAP["buff_listing_count"]]),
        buff_buy_order_count=int(raw[CS2SH_FIELD_MAP["buff_buy_order_count"]]),
        buff_volume_24h=int(raw[CS2SH_FIELD_MAP["buff_volume_24h"]]),
        ts=ts if ts is not None else time.time(),
        variant=raw.get("variant"),
    )


class Cs2shFeed:
    """Live cs2.sh client. Endpoint/auth details pending — see module docstring."""

    BASE_URL = "https://api.cs2.sh/v1"  # TODO(Leon): confirm real base URL

    def __init__(self, tracked_items: list[str]):
        self.tracked_items = tracked_items

    def fetch(self) -> list[Item]:
        api_key = secret("CS2SH_API_KEY")
        if api_key is None:
            raise FeedUnavailable(
                "CS2SH_API_KEY is a placeholder — live feed disabled. "
                "Use ReplayFeed for paper/backtest runs until the key arrives."
            )
        request = urllib.request.Request(
            f"{self.BASE_URL}/items?names={','.join(self.tracked_items)}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            payload = json.load(resp)
        return [normalize_cs2sh(raw) for raw in payload["items"]]


class ReplayFeed:
    """Replays snapshots from a JSONL file: one Item dict per line, ordered by ts.

    Each fetch() returns the next distinct-ts snapshot group, so a paper run
    or backtest steps through history one snapshot at a time.
    """

    def __init__(self, path: Path):
        self._groups = _group_by_ts(path)
        self._cursor = 0

    def fetch(self) -> list[Item]:
        if self._cursor >= len(self._groups):
            raise FeedUnavailable("replay exhausted")
        items = self._groups[self._cursor]
        self._cursor += 1
        return items

    def __iter__(self) -> Iterator[list[Item]]:
        while self._cursor < len(self._groups):
            yield self.fetch()


def _group_by_ts(path: Path) -> list[list[Item]]:
    by_ts: dict[float, list[Item]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        float_range = raw.get("float_range")
        item = Item(
            market_hash_name=raw["market_hash_name"],
            buff_lowest_sell_cny=raw["buff_lowest_sell_cny"],
            buff_highest_buy_cny=raw["buff_highest_buy_cny"],
            buff_listing_count=raw["buff_listing_count"],
            buff_buy_order_count=raw["buff_buy_order_count"],
            buff_volume_24h=raw["buff_volume_24h"],
            ts=raw["ts"],
            variant=raw.get("variant"),
            float_range=tuple(float_range) if float_range else None,
            cross_market=raw.get("cross_market", {}),
        )
        by_ts.setdefault(item.ts, []).append(item)
    return [by_ts[ts] for ts in sorted(by_ts)]


def item_to_json(item: Item) -> str:
    d = {
        "market_hash_name": item.market_hash_name,
        "buff_lowest_sell_cny": item.buff_lowest_sell_cny,
        "buff_highest_buy_cny": item.buff_highest_buy_cny,
        "buff_listing_count": item.buff_listing_count,
        "buff_buy_order_count": item.buff_buy_order_count,
        "buff_volume_24h": item.buff_volume_24h,
        "ts": item.ts,
    }
    if item.variant:
        d["variant"] = item.variant
    if item.float_range:
        d["float_range"] = list(item.float_range)
    if item.cross_market:
        d["cross_market"] = item.cross_market
    return json.dumps(d)
