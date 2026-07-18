"""Shared tiered signal bus (System A §7.5) — System B consumes Tiers 2 & 3.

File-backed JSONL implementation so the two systems stay decoupled. System B
must degrade gracefully when the bus is missing/stale (System B §7): the
NullBus and empty reads make attention features go null, never crash.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schema import BusSignal


class SignalBus:
    """Read/write interface. System A's monitor writes; both systems read."""

    def publish(self, signal: BusSignal) -> None:
        raise NotImplementedError

    def read(
        self,
        tiers: tuple[int, ...] = (1, 2, 3),
        since: datetime | None = None,
        max_age_days: float = 14.0,
        as_of: datetime | None = None,
    ) -> list[BusSignal]:
        """`as_of` is the decision time: signals first seen AFTER it are never
        returned (a replayed/dated cycle must not see the future), and the
        age decay is measured from it, not from wall-clock now."""
        raise NotImplementedError


class NullBus(SignalBus):
    """Graceful-degradation default: no signals, never fails."""

    def publish(self, signal: BusSignal) -> None:
        pass

    def read(self, tiers=(1, 2, 3), since=None, max_age_days=14.0, as_of=None) -> list[BusSignal]:
        return []


class JsonlBus(SignalBus):
    def __init__(self, path: Path):
        self.path = Path(path)

    def publish(self, signal: BusSignal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = signal.__dict__.copy()
        ts = rec.get("first_seen_ts")
        rec["first_seen_ts"] = (ts or datetime.now(timezone.utc)).isoformat()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read(self, tiers=(1, 2, 3), since=None, max_age_days=14.0, as_of=None) -> list[BusSignal]:
        if not self.path.exists():
            return []
        now = as_of if as_of is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        out: list[BusSignal] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("tier") not in tiers:
                        continue
                    ts_raw = rec.get("first_seen_ts")
                    ts = datetime.fromisoformat(ts_raw) if ts_raw else None
                    if ts is not None and ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if since is not None and ts is not None and ts < since:
                        continue
                    if ts is not None and ts > now:
                        continue  # first seen after the decision time — future data
                    if ts is not None and now - ts > timedelta(days=max_age_days):
                        continue  # decay old signals (System A §7.4)
                    rec["first_seen_ts"] = ts
                    known = {k: v for k, v in rec.items() if k in BusSignal.__dataclass_fields__}
                    out.append(BusSignal(**known))
        except OSError:
            return []  # bus down -> degrade, don't crash
        return out


def attention_for_item(signals: list[BusSignal], item: str) -> tuple[float, float]:
    """(attention_score, sentiment) for one item from Tier-3 signals; (0,0) if none."""
    scores = [s for s in signals if s.tier == 3 and item in s.items]
    if not scores:
        return 0.0, 0.0
    latest = max(scores, key=lambda s: s.first_seen_ts or datetime.min.replace(tzinfo=timezone.utc))
    return float(latest.attention_score), float(latest.sentiment)


def confirmed_events_for_item(signals: list[BusSignal], item: str, collection: str = "") -> list[BusSignal]:
    """Tier-2 confirmed events touching an item or its collection (risk overlay)."""
    out = []
    for s in signals:
        if s.tier != 2:
            continue
        if item in s.items or (collection and collection in s.items):
            out.append(s)
    return out
