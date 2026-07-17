"""Tiered signal bus (docs/System-A §7.5).

Pull-based: the monitor (and break detector) publish; each system polls the
tiers it consumes on its own decision cadence. Signals dedup by (type, item
set) keeping the highest-confidence version, and expire after a decay window.
"""

from __future__ import annotations

from shared.schema import Signal


class SignalBus:
    def __init__(self) -> None:
        self._signals: dict[str, Signal] = {}

    def publish(self, signal: Signal) -> None:
        key = signal.key()
        existing = self._signals.get(key)
        if existing is None or signal.confidence >= existing.confidence:
            self._signals[key] = signal

    def active(self, tiers: list[int], now_ts: float, max_age_hours: float) -> list[Signal]:
        cutoff = now_ts - max_age_hours * 3600.0
        return [
            s for s in self._signals.values()
            if s.tier in tiers and s.first_seen_ts >= cutoff
        ]

    def prune(self, now_ts: float, max_age_hours: float) -> None:
        cutoff = now_ts - max_age_hours * 3600.0
        self._signals = {
            k: s for k, s in self._signals.items() if s.first_seen_ts >= cutoff
        }
