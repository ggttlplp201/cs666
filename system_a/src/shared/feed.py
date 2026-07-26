"""Market-data feeds.

Live: SteamLiveFeed (free, via Scrapling). Historical BUFF: iflow archive
(shared/iflow_history.py, source=buff_iflow). Phase-1 backtest history: Steam
pricehistory (shared/steam_history.py). ReplayFeed serves recorded/synthetic
snapshots for backtests and paper demos.

(cs2.sh was dropped 2026-07-25 — the demo key expired/401'd and a paid key was
never worth it once Steam-via-Scrapling covered the live-liveness need. See
docs/Shared §2a. If a real BUFF live API is ever added, model it as a new feed
class here alongside SteamLiveFeed.)
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Iterator, Protocol

from shared.schema import Item


class FeedUnavailable(RuntimeError):
    pass


class Feed(Protocol):
    def fetch(self) -> list[Item]:
        """Return the current snapshot for all tracked items."""
        ...


class SteamLiveFeed:
    """FREE live price feed via Steam Market priceoverview, fetched through
    Scrapling (its fingerprint spoofing clears Steam's 429 that plain requests
    hit). No key, no cookie. Returns lowest_price (ask), median_price, and 24h
    volume — real executed volume, the field cs2.sh's Developer tier lacks.

    ⚠ This is STEAM, not BUFF: prices run ~30-40% above BUFF and there is no
    bid/ask spread (priceoverview gives no buy order). Store rows as
    source="steam_live" so they never mix with BUFF. Used to keep the live
    dashboard fed for free when no BUFF key is available; not a BUFF substitute
    for trading economics. Prices stay USD (the *_cny field names are
    venue-legacy; the source tag disambiguates).

    Polls one item at a time with a polite gap; a failed item is skipped, not
    fatal (partial snapshot beats none). Requires scrapling[fetchers].
    """

    URL = "https://steamcommunity.com/market/priceoverview/"

    def __init__(self, tracked_items: list[str], request_gap_seconds: float = 4.0):
        self.tracked_items = tracked_items
        self.request_gap_seconds = request_gap_seconds
        self.last_errors: list[dict] = []

    def fetch(self) -> list[Item]:
        try:
            from scrapling.fetchers import Fetcher
        except Exception as e:
            raise FeedUnavailable(
                "scrapling not installed — `pip install scrapling[fetchers]` "
                "for the free Steam live feed"
            ) from e
        ts = time.time()
        items, self.last_errors = [], []
        for i, name in enumerate(self.tracked_items):
            if i:
                time.sleep(self.request_gap_seconds)
            try:
                item = self._fetch_one(Fetcher, name, ts)
                if item is not None:
                    items.append(item)
                else:
                    self.last_errors.append({"item": name, "error": "no price"})
            except Exception as e:  # one bad item never kills the snapshot
                self.last_errors.append({"item": name, "error": str(e)[:80]})
        if not items:
            raise FeedUnavailable("steam live: every item failed (rate-limited?)")
        return items

    def _fetch_one(self, Fetcher, name: str, ts: float) -> Item | None:
        query = urllib.parse.urlencode(
            {"appid": 730, "currency": 1, "market_hash_name": name}
        )
        resp = Fetcher.get(f"{self.URL}?{query}", timeout=25)
        raw = getattr(resp, "body", None) or resp.text
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
        if not data.get("success") or not data.get("lowest_price"):
            return None
        ask = _parse_usd(data["lowest_price"])
        volume = int(str(data.get("volume", "0")).replace(",", "") or 0)
        return Item(
            market_hash_name=name,
            buff_lowest_sell_cny=ask,          # USD — Steam ask (see docstring)
            buff_highest_buy_cny=0.0,          # Steam priceoverview has no bid
            buff_listing_count=0,              # not exposed by this endpoint
            buff_buy_order_count=0,
            buff_volume_24h=volume,            # real executed volume (Steam)
            ts=ts,
        )


def _parse_usd(s: str) -> float:
    return float(str(s).replace("$", "").replace(",", "").strip())


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
            buff_volume_24h=raw.get("buff_volume_24h"),
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
