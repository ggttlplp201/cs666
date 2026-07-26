"""cs2.sh adapter — field mapping verified against the vendor's open docs
(https://cs2.sh/llms-full.txt, dated 2026-06-15; fetched 2026-07-17).

Verified mapping (BUFF source object in /v1/prices/latest):
    ask         -> sell_price        (lowest BUFF ask, USD — vendor converts CNY!)
    bid         -> buy_price         (highest BUFF buy order, USD)
    ask_volume  -> listing_count     (order-book state, NOT executed trades)
    bid_volume  -> buy_order_count
    collected_at-> freshness check

KNOWN GAPS (research notes §3):
- There is NO BUFF executed-trade field. `volume` is proxied from
  POST /v1/archive/history `aggregate.hourly_volume` (cross-platform approx,
  updates 1-2x/day) — mark it as a PROXY in any signal work.
- `valid_buy_orders` (bids near market) is not derivable from counts alone ->
  emitted as -1 (unknown); filters fall back to buy_order_count.
- Prices are USD; BUFF trades in CNY. Convert with a configured FX rate and
  treat FX noise as a data caveat until a CNY-native source exists.
- `aggregate.total_supply` (archive) feeds ItemMeta.supply refreshes.

Auth: Authorization: Bearer $CS2SH_API_KEY + Accept-Encoding: gzip (REQUIRED).
Rate limit 10 rps; POST bodies max 100 items.
"""

from __future__ import annotations

import gzip
import json
import urllib.request
from datetime import date, datetime
from typing import Any, Iterable

from ..config import env_secret
from ..schema import ItemDay

BASE_URL = "https://api.cs2.sh"


def _request(path: str, api_key: str, body: dict | None = None) -> Any:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


# ---------------------------------------------------------------------------
# pure normalizers (unit-testable without a key)
# ---------------------------------------------------------------------------

def normalize_latest(
    payload: dict,
    day: date,
    usd_cny: float = 7.1,
    volume_by_item: dict[str, int] | None = None,
) -> list[ItemDay]:
    """Map a /v1/prices/latest response to ItemDay records.

    `volume_by_item` is the executed-volume proxy from the archive endpoint
    (see fetch_daily_volume_proxy); items without it get volume=-0 and should
    be treated as volume-unknown upstream.
    """
    out: list[ItemDay] = []
    items = payload.get("items", payload)  # docs show top-level items map
    for name, entry in items.items():
        buff = (entry or {}).get("buff")
        if not buff or buff.get("ask") is None:
            continue
        vol = (volume_by_item or {}).get(name, 0)
        out.append(
            ItemDay(
                market_hash_name=name,
                day=day,
                sell_price=float(buff["ask"]) * usd_cny,
                buy_price=float(buff.get("bid") or 0.0) * usd_cny,
                listing_count=int(buff.get("ask_volume") or 0),
                buy_order_count=int(buff.get("bid_volume") or 0),
                volume=int(vol),
                valid_buy_orders=-1,  # not derivable from counts; unknown
            )
        )
    return out


def normalize_archive_day(payload: dict) -> tuple[dict[str, int], dict[str, int]]:
    """From /v1/archive/history (aggregate source, 1d interval): per item,
    (daily volume proxy, total_supply). Docs: only `aggregate` carries
    hourly_volume and total_supply."""
    volumes: dict[str, int] = {}
    supplies: dict[str, int] = {}
    for name, series in (payload.get("items") or {}).items():
        buckets = series if isinstance(series, list) else series.get("buckets", [])
        if not buckets:
            continue
        last = buckets[-1]
        agg = last.get("aggregate") or {}
        hv = agg.get("hourly_volume")
        if hv is not None:
            # 1d bucket: hourly rate * 24 as the daily proxy (semantics
            # unconfirmed by docs — flagged in research notes)
            volumes[name] = int(round(float(hv) * 24))
        ts = agg.get("total_supply")
        if ts is not None:
            supplies[name] = int(ts)
    return volumes, supplies


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class Cs2ShClient:
    def __init__(self, api_key: str | None = None, usd_cny: float = 7.1):
        self.api_key = api_key or env_secret("CS2SH_API_KEY")
        self.usd_cny = usd_cny
        if not self.api_key:
            raise RuntimeError("CS2SH_API_KEY not set (see .env.example)")

    def latest(self, items: Iterable[str] | None = None) -> dict:
        if items is None:
            return _request("/v1/prices/latest", self.api_key)
        return _request("/v1/prices/latest", self.api_key, {"items": list(items)})

    def archive_day(self, items: list[str], start: str, end: str) -> dict:
        return _request(
            "/v1/archive/history",
            self.api_key,
            {"items": items[:100], "start": start, "end": end,
             "sources": ["aggregate"], "interval": "1d"},
        )

    def snapshot(self, items: list[str], day: date | None = None) -> list[ItemDay]:
        """One normalized daily snapshot for the collector."""
        day = day or datetime.utcnow().date()
        payload = self.latest(items)
        iso = day.isoformat()
        volumes: dict[str, int] = {}
        try:
            arch = self.archive_day(items, iso, iso)
            volumes, _ = normalize_archive_day(arch)
        except Exception:
            pass  # archive is 1-2x/day; missing volume proxy is survivable
        return normalize_latest(payload, day, self.usd_cny, volumes)
