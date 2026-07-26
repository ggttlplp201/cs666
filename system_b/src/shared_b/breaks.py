"""Structural-break / drift alarm (Shared §12, System A §4.1a).

Live CUSUM on standardized returns. Nikolaenko showed Bai-Perron breaks land
on balance-update dates; a live CUSUM is the cheap streaming analogue. System B
uses it as a retrain trigger and a "don't trust stale factor weights" alarm.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BreakAlarm:
    fired: bool
    stat: float          # max |CUSUM| over the window
    threshold: float
    direction: int       # +1 upside break, -1 downside, 0 none


def jump_break(
    returns: np.ndarray,
    sigma: float,
    k: float = 6.0,
) -> BreakAlarm:
    """Nerf-type instant repricing detector: |r_t| > k*sigma one-day jump.

    Nikolaenko's 18-Nov-2022 M4A1-S nerf printed a +0.87 daily log return vs
    ~2.3% daily vol (~38 sigma) — trivially caught. Gradual buff-type breaks
    need the CUSUM below instead.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0 or sigma <= 0:
        return BreakAlarm(False, 0.0, k, 0)
    last = r[-1]
    stat = abs(last) / sigma
    return BreakAlarm(stat > k, float(stat), k, int(np.sign(last)) if stat > k else 0)


def cusum_break(
    returns: np.ndarray,
    baseline_window: int = 60,
    test_window: int = 10,
    threshold: float = 5.0,
    k: float = 0.5,
) -> BreakAlarm:
    """One-sided CUSUM pair on returns standardized by the baseline window.

    `k` is the slack (in std units); `threshold` the decision interval h.
    Standard SPC tuning: h ~ 4-5, k ~ 0.5 catches ~1-sigma mean shifts.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < baseline_window + test_window:
        return BreakAlarm(False, 0.0, threshold, 0)
    base = r[:-test_window][-baseline_window:]
    mu, sd = base.mean(), base.std()
    if sd < 1e-10:
        return BreakAlarm(False, 0.0, threshold, 0)
    z = (r[-test_window:] - mu) / sd
    s_hi = s_lo = 0.0
    max_hi = max_lo = 0.0
    for x in z:
        s_hi = max(0.0, s_hi + x - k)
        s_lo = max(0.0, s_lo - x - k)
        max_hi = max(max_hi, s_hi)
        max_lo = max(max_lo, s_lo)
    stat = max(max_hi, max_lo)
    direction = 0
    if stat > threshold:
        direction = 1 if max_hi >= max_lo else -1
    return BreakAlarm(stat > threshold, float(stat), threshold, direction)
