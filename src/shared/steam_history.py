"""Phase-1 FREE backtest data: Steam Community Market price history.

Why (docs Shared §2a): the cs2.sh Developer tier gives BUFF bid/ask + depth
but NO executed volume. Steam's undocumented pricehistory endpoint returns
DAILY MEDIAN PRICE plus QUANTITY SOLD (real executed volume) back to 2013 —
the exact dataset Pettersson built his 640k-observation study on
(research/papers/paper2.pdf, Appendix A2). We use it to validate the
strategy premise before paying for any feed.

CAVEATS (also in Shared §2a):
  - This is STEAM data, not BUFF. Steam trades ~30–40% above BUFF. Valid for
    premise validation (do balance updates cause tradeable repricings? does
    MA-deviation momentum predict net of fees?) — NOT a live-trading input
    and NOT a BUFF price substitute. Prices stay in USD; rows are ingested
    with source="steam" so they can never mix with BUFF rows (store reads
    default to source="buff").
  - Daily median hides intraday moves; the volume is Steam's, not BUFF's.
  - The endpoint is undocumented and can vanish/change without notice —
    everything Steam-specific is isolated in this module.

Endpoint: GET https://steamcommunity.com/market/pricehistory/
              ?appid=730&market_hash_name=<url-encoded>
Auth: `steamLoginSecure` cookie from a logged-in session (STEAM_LOGIN_SECURE
in .env — never hardcoded, never in docs/config). success:false or non-JSON
means the cookie is missing/expired.

CLI:  PYTHONPATH=src python -m shared.steam_history
      (knobs default from config/shared.yaml data.steam_history; cache is
      resumable — already-downloaded items are skipped, so interrupt freely)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shared.configuration import Config, secret
from shared.schema import Item
from shared.store import SnapshotStore

URL = "https://steamcommunity.com/market/pricehistory/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_RETRIES = 5


class SteamAuthError(RuntimeError):
    """401 / success:false — the steamLoginSecure cookie is missing or expired."""


@dataclass(frozen=True)
class DailyRow:
    date: str            # "Jul 18 2026" — first 11 chars of Steam's date string
    median_price: float  # USD (Steam venue price — NOT BUFF)
    volume: int          # quantity sold that day — REAL executed volume


def normalize_rows(payload: dict) -> list[DailyRow]:
    """Steam rows: ["Jul 18 2026 01: +0", 29.97, "142"]."""
    if not payload.get("success"):
        raise SteamAuthError(
            "Steam responded success:false — not logged in / bad cookie."
        )
    return [
        DailyRow(str(row[0])[:11], float(row[1]), int(str(row[2])))
        for row in payload.get("prices") or []
    ]


def rows_to_items(market_hash_name: str, rows: list[DailyRow]) -> list[Item]:
    """Map daily rows onto the Shared §2.3 schema for source="steam" storage.

    Steam gives one median price and no book, so bid==ask==median (USD —
    the *_cny field names are venue-legacy; the source tag disambiguates)
    and depth counts are 0. volume_24h carries the real quantity sold."""
    items = []
    for row in rows:
        ts = datetime.strptime(row.date, "%b %d %Y").replace(
            tzinfo=timezone.utc
        ).timestamp()
        items.append(
            Item(
                market_hash_name=market_hash_name,
                buff_lowest_sell_cny=row.median_price,
                buff_highest_buy_cny=row.median_price,
                buff_listing_count=0,
                buff_buy_order_count=0,
                buff_volume_24h=row.volume,
                ts=ts,
            )
        )
    return items


class SteamHistoryFetcher:
    def __init__(
        self,
        cache_dir: Path,
        request_gap_seconds: float = 4.0,
        backoff_start_seconds: float = 60.0,
    ):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_gap_seconds = request_gap_seconds
        self.backoff_start_seconds = backoff_start_seconds
        self._last_request = 0.0

    def cache_path(self, name: str) -> Path:
        return self.cache_dir / f"{urllib.parse.quote(name, safe='')}.json"

    def is_cached(self, name: str) -> bool:
        return self.cache_path(name).exists()

    def fetch_item(self, name: str) -> dict:
        """Return the raw payload, from cache when present (resumable runs)."""
        cached = self.cache_path(name)
        if cached.exists():
            return json.loads(cached.read_text())
        payload = self._request(name)
        normalize_rows(payload)          # validate before caching — never cache
        cached.write_text(json.dumps(payload))  # a not-logged-in response
        return payload

    def _request(self, name: str) -> dict:
        cookie = secret("STEAM_LOGIN_SECURE")
        if cookie is None:
            raise SteamAuthError(
                "STEAM_LOGIN_SECURE is unset/placeholder — export the "
                "steamLoginSecure cookie from a logged-in browser session "
                "into .env first."
            )
        wait = self.request_gap_seconds - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode({"appid": 730, "market_hash_name": name})
        request = urllib.request.Request(
            f"{URL}?{query}",
            headers={
                "Cookie": f"steamLoginSecure={cookie}",
                "User-Agent": USER_AGENT,
            },
        )
        backoff = self.backoff_start_seconds
        for attempt in range(MAX_RETRIES):
            try:
                self._last_request = time.time()
                with urllib.request.urlopen(request, timeout=30) as resp:
                    body = resp.read()
                try:
                    return json.loads(body)
                except json.JSONDecodeError as e:
                    raise SteamAuthError(
                        "Steam returned non-JSON (login page?) — cookie "
                        "expired or invalid."
                    ) from e
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise SteamAuthError(
                        "Steam returned 401 — the steamLoginSecure cookie has "
                        "expired. Re-export it from a logged-in session."
                    ) from e
                if e.code == 429 and attempt < MAX_RETRIES - 1:
                    print(f"    429 rate-limited; backing off {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        raise RuntimeError(f"steam pricehistory: {MAX_RETRIES} retries exhausted for {name}")


def run(
    items_file: Path,
    cache_dir: Path,
    db_path: Path,
    request_gap_seconds: float,
    backoff_start_seconds: float,
    source_tag: str,
) -> int:
    names = [
        line.strip() for line in items_file.read_text().splitlines() if line.strip()
    ]
    fetcher = SteamHistoryFetcher(cache_dir, request_gap_seconds, backoff_start_seconds)
    store = SnapshotStore(db_path)
    total_days = 0
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    for i, name in enumerate(names, 1):
        cached = " (cached)" if fetcher.is_cached(name) else ""
        try:
            rows = normalize_rows(fetcher.fetch_item(name))
        except SteamAuthError:
            raise   # auth problems abort the whole run — nothing else can succeed
        except Exception as e:  # one bad item must not kill a resumable batch
            print(f"[{i}/{len(names)}] {name}: ERROR {e}")
            failed.append((name, str(e)))
            continue
        store.insert(rows_to_items(name, rows), source=source_tag)
        total_days += len(rows)
        span = f"{rows[0].date} → {rows[-1].date}" if rows else "NO DATA"
        print(f"[{i}/{len(names)}] {name}: {len(rows)} days{cached}  [{span}]")
        if not rows:
            empty.append(name)

    print(f"\nitems fetched:   {len(names) - len(failed)}/{len(names)}")
    print(f"day-records:     {total_days} (source='{source_tag}' in {db_path})")
    if empty:
        print(f"no data:         {', '.join(empty)}")
    if failed:
        print(f"failed:          {', '.join(n for n, _ in failed)}")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config.load(repo_root)
    defaults = config.require("data.steam_history")
    parser = argparse.ArgumentParser(description="Steam price-history fetcher (Phase 1)")
    parser.add_argument("--items", type=Path,
                        default=repo_root / defaults["items_file"])
    parser.add_argument("--cache", type=Path,
                        default=repo_root / defaults["cache_dir"])
    parser.add_argument("--db", type=Path,
                        default=repo_root / config.require("data.snapshot_poller")["db_path"])
    parser.add_argument("--gap", type=float, default=defaults["request_gap_seconds"])
    args = parser.parse_args(argv)
    try:
        return run(
            args.items, args.cache, args.db, args.gap,
            defaults["backoff_start_seconds"], defaults["source_tag"],
        )
    except SteamAuthError as e:
        print(f"AUTH FAILURE: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
