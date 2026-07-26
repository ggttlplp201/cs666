"""Market-data structural-break detector (System A §4.1a).

Two-sided CUSUM on standardized returns per item. Nikolaenko showed breaks
land on balance-update dates, so an alarm is a market-data-based update
detector that doesn't depend on catching a social leak. Alarms are emitted
as Tier-2 MARKET_BREAK signals and also flag the Shared §12 retrain alarm.
"""

from __future__ import annotations

import math

from shared.schema import Direction, Item, Signal, SignalType


class CusumDetector:
    def __init__(
        self,
        std_window: int,
        drift_k: float,
        threshold_h: float,
        emitted_confidence: float,
    ):
        self.std_window = std_window
        self.drift_k = drift_k
        self.threshold_h = threshold_h
        self.emitted_confidence = emitted_confidence
        self._pos: dict[str, float] = {}
        self._neg: dict[str, float] = {}

    def update(self, name: str, history: list[Item]) -> Signal | None:
        """Feed the latest history for one item; returns an alarm Signal on a
        break, resetting that item's statistic."""
        prices = [i.buff_lowest_sell_cny for i in history]
        if len(prices) < self.std_window + 2:
            return None
        returns = [
            math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))
        ]
        window = returns[-(self.std_window + 1):-1]
        mean = sum(window) / len(window)
        std = math.sqrt(sum((r - mean) ** 2 for r in window) / len(window))
        if std == 0:
            std = 1e-9
        z = (returns[-1] - mean) / std

        pos = max(0.0, self._pos.get(name, 0.0) + z - self.drift_k)
        neg = max(0.0, self._neg.get(name, 0.0) - z - self.drift_k)
        self._pos[name], self._neg[name] = pos, neg

        if pos > self.threshold_h or neg > self.threshold_h:
            direction = Direction.BULLISH if pos > self.threshold_h else Direction.BEARISH
            self._pos[name] = self._neg[name] = 0.0
            return Signal(
                tier=2,
                type=SignalType.MARKET_BREAK,
                items=(name,),
                direction=direction,
                confidence=self.emitted_confidence,
                first_seen_ts=history[-1].ts,
                sources=("cusum",),
            )
        return None
