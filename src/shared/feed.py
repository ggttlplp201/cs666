"""Market-data feeds.

cs2.sh (VERIFIED against the live API — see docs/Shared §2a):
  - Base URL https://api.cs2.sh; auth `Authorization: Bearer <key>`;
    `Accept-Encoding: gzip` is REQUIRED on every /v1 request.
  - POST /v1/prices/latest takes up to 100 items per request (body ≤ 1 MiB);
    a bad item name lands in the response's errors[] instead of failing the
    batch. GET (no body) returns ALL tracked items — never use it casually.
  - Developer tier ($75/mo) exposes ONLY /v1/prices/latest: bid/ask + depth
    counts. NO executed volume, float ranges, or total supply — those need
    Scale ($200/mo) archive/market endpoints. Item.buff_volume_24h is
    therefore None on this feed and consumers must degrade explicitly.
  - ALL prices are normalized to USD (including BUFF, which trades CNY).
    The normalizer converts USD→CNY at the configured rate; nothing
    downstream may assume the raw feed is CNY.
  - `collected_at` is cs2.sh's snapshot time — use it for freshness checks.
    `updated_at` is the marketplace's own timestamp; NOT freshness.

Steam price history (Phase 1 backtest source) lives in
shared/steam_history.py — isolated there because the endpoint is
undocumented and may break without notice.

ReplayFeed serves recorded/synthetic snapshots for backtests and paper demos.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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


CS2SH_BASE_URL = "https://api.cs2.sh"  # verified
CS2SH_BATCH_MAX = 100                  # verified POST limit per request
CS2SH_SOURCES = ("buff", "youpin", "csfloat", "skinport", "steam", "c5game")


def _parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def normalize_cs2sh(name: str, entry: dict, usd_cny_rate: float) -> Item | None:
    """Map one items[market_hash_name] entry onto the schema (verified shape:
    per-source dicts; buff.ask/ask_volume/bid/bid_volume, collected_at).

    Prices arrive in USD and are converted to CNY here. ask_volume is the
    LISTING COUNT and bid_volume the BUY-ORDER COUNT — depth, never total
    supply (存世量) and never executed volume. Returns None when the entry
    has no BUFF quote."""
    buff = entry.get("buff")
    if not buff or buff.get("ask") is None:
        return None
    cross = {
        source: round(float(quote["ask"]), 4)
        for source, quote in entry.items()
        if source in CS2SH_SOURCES and source != "buff"
        and isinstance(quote, dict) and quote.get("ask") is not None
    }
    return Item(
        market_hash_name=name,
        buff_lowest_sell_cny=round(float(buff["ask"]) * usd_cny_rate, 2),
        buff_highest_buy_cny=round(float(buff.get("bid") or 0.0) * usd_cny_rate, 2),
        buff_listing_count=int(buff.get("ask_volume") or 0),
        buff_buy_order_count=int(buff.get("bid_volume") or 0),
        buff_volume_24h=None,  # executed volume is Scale-tier only — docs §2a
        ts=_parse_ts(entry.get("collected_at", time.time())),
        cross_market=cross,   # other sources' asks in USD — divergence checks only
    )


class Cs2shFeed:
    """POST /v1/prices/latest client (Developer tier)."""

    def __init__(self, tracked_items: list[str], usd_cny_rate: float):
        self.tracked_items = tracked_items
        self.usd_cny_rate = usd_cny_rate
        self.last_errors: list[dict] = []   # per-item errors[] from the API

    def fetch(self) -> list[Item]:
        api_key = secret("CS2SH_API_KEY")
        if api_key is None:
            raise FeedUnavailable(
                "CS2SH_API_KEY is a placeholder — live feed disabled. "
                "Use ReplayFeed for paper/backtest runs until the key arrives."
            )
        items: list[Item] = []
        self.last_errors = []
        for start in range(0, len(self.tracked_items), CS2SH_BATCH_MAX):
            batch = self.tracked_items[start:start + CS2SH_BATCH_MAX]
            payload = self._post_latest(api_key, batch)
            items.extend(self.parse_latest(payload, self.usd_cny_rate))
            self.last_errors.extend(payload.get("errors") or [])
        return items

    def _post_latest(self, api_key: str, batch: list[str]) -> dict:
        # NOTE: the request-body key name is the one unverified detail left —
        # confirm against the cs2.sh docs if a 4xx appears here.
        body = json.dumps({"items": batch}).encode()
        request = urllib.request.Request(
            f"{CS2SH_BASE_URL}/v1/prices/latest",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept-Encoding": "gzip",   # REQUIRED by the API
                "Content-Type": "application/json",
                # WAF blocks the default Python-urllib agent (curl passes) —
                # identify honestly as our client instead.
                "User-Agent": "cs2-quant/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return json.loads(raw)

    @staticmethod
    def parse_latest(payload: dict, usd_cny_rate: float) -> list[Item]:
        items = []
        for name, entry in (payload.get("items") or {}).items():
            item = normalize_cs2sh(name, entry, usd_cny_rate)
            if item is not None:
                items.append(item)
        return items


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
