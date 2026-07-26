"""Position management for held lots (Shared §3 indicators + §6.3 TP/SL).

Implements the shared library's hold/exit "market rhymes" as individually
toggleable, individually attributed rules. Used by BOTH the live engine
(per-cycle assessment of each held lot) and the exit-policy study — one code
path, no duplication of System B's implementation.

Precedence, resolving the documented tension between rhymes and risk rules:
  1. HARD STOP (Shared §6.3) — a risk rule; overrides everything, including
     "don't sell in a crash".
  2. big_crash_no_sell — suppresses TA exits that day (wait for the bounce).
  3. EXIT rules, first match wins (order below).
  4. thesis invalidation — predicted repricing absent by day N ⇒ exit.
  5. HOLD rules — attribution only (which rhyme justified holding).

Volume-dependent rules (shakeout, distribution, green-bar) evaluate only
when executed volume exists in the window; on the volume-less cs2.sh
Developer tier they SKIP and are attributed as unavailable — never proxied
from listings (Shared §2a).
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.schema import Item


@dataclass(frozen=True)
class Assessment:
    action: str                  # "exit" | "hold" | "suppressed_exit"
    rule: str                    # the rule that decided (provenance attribution)
    detail: str = ""
    unavailable_rules: tuple[str, ...] = ()   # volume rules skipped for data


def _returns(series: list[Item], n: int) -> list[float]:
    prices = [b.buff_lowest_sell_cny for b in series[-(n + 1):]]
    return [
        prices[i + 1] / prices[i] - 1 for i in range(len(prices) - 1)
    ]


def _volume_ratio(series: list[Item], baseline_window: int) -> float | None:
    if len(series) < 3:
        return None
    latest = series[-1].buff_volume_24h
    baseline = [
        b.buff_volume_24h for b in series[-(baseline_window + 1):-1]
        if b.buff_volume_24h is not None
    ]
    if latest is None or not baseline:
        return None
    mean = sum(baseline) / len(baseline)
    return latest / mean if mean else None


def _pct_b(series: list[Item], window: int, num_std: float) -> float | None:
    from shared.indicators import bollinger
    prices = [b.buff_lowest_sell_cny for b in series]
    if len(prices) < window:
        return None
    return bollinger(prices, window, num_std).pct_b


def assess(
    series: list[Item],
    entry_price: float,
    held_days: float,
    pm: dict,                    # config system_a.position_management
    indicators_cfg: dict,        # config indicators (bollinger/baseline params)
    brackets: dict,              # config brackets (§6.3)
) -> Assessment:
    if len(series) < 2:
        return Assessment("hold", "insufficient_history")
    t = pm["thresholds"]
    rules, hold_rules = pm["rules"], pm["hold_rules"]
    price = series[-1].buff_lowest_sell_cny
    ret = price / entry_price - 1
    r1 = _returns(series, 1)[-1] if len(series) >= 2 else 0.0
    r3 = _returns(series, 3)
    volume_ratio = _volume_ratio(series, indicators_cfg["volume_baseline_window"])
    pct_b = _pct_b(
        series, indicators_cfg["bollinger_window"],
        indicators_cfg["bollinger_num_std"],
    )
    unavailable = tuple(
        r for r in ("sharp_drop_low_volume", "slow_decline_high_volume",
                    "upper_band_green", "upper_band_no_green")
        if volume_ratio is None
    )
    green_bar = (
        volume_ratio is not None and r1 > 0
        and volume_ratio >= t["green_bar_volume_ratio"]
    )

    # 1. Hard stop — risk rule beats every rhyme.
    if rules.get("stop_loss") and ret <= brackets["stop_loss_cut_pct"]:
        return Assessment("exit", "stop_loss", f"ret {ret:+.1%}", unavailable)

    # 2. Crash suppression (never suppresses the stop).
    if hold_rules.get("big_crash_no_sell") and r1 <= t["crash_1d_pct"]:
        return Assessment(
            "suppressed_exit", "big_crash_no_sell", f"1d {r1:+.1%}", unavailable
        )

    # 3. EXIT rules, first match wins.
    if rules.get("surge_euphoria") and (
        r1 >= t["surge_1d_pct"] or sum(r3) >= t["surge_3d_pct"]
    ):
        return Assessment(
            "exit", "surge_euphoria", f"1d {r1:+.1%} 3d {sum(r3):+.1%}", unavailable
        )
    if rules.get("large_consecutive_rises") and len(r3) >= 2 and all(
        r >= t["large_rise_day_pct"] for r in r3[-2:]
    ):
        return Assessment("exit", "large_consecutive_rises", "", unavailable)
    if rules.get("upper_band_green") and pct_b is not None \
            and pct_b >= t["upper_band_pct_b"] and green_bar:
        return Assessment(
            "exit", "upper_band_green", f"pct_b {pct_b:.2f}", unavailable
        )
    if rules.get("slow_decline_high_volume") and len(r3) == 3 \
            and volume_ratio is not None:
        lo, hi = t["slow_decline_3d_band"]
        if lo <= sum(r3) <= hi and all(r <= 0 for r in r3) \
                and volume_ratio >= t["green_bar_volume_ratio"]:
            return Assessment(
                "exit", "slow_decline_high_volume", f"3d {sum(r3):+.1%}",
                unavailable,
            )
    if rules.get("take_profit") and ret >= brackets["take_profit_pct"][0]:
        return Assessment("exit", "take_profit", f"ret {ret:+.1%}", unavailable)

    # 4. Thesis invalidation: the repricing simply hasn't happened.
    if held_days >= pm["thesis_invalidation_days"] and \
            ret <= pm["thesis_min_progress_pct"]:
        return Assessment(
            "exit", "thesis_invalidation",
            f"day {held_days:.0f}, ret {ret:+.1%}", unavailable,
        )

    # 5. HOLD attribution.
    if hold_rules.get("sharp_drop_low_volume") and volume_ratio is not None \
            and r1 <= t["sharp_drop_pct"] and volume_ratio <= t["low_volume_ratio"]:
        return Assessment("hold", "sharp_drop_low_volume", "shakeout", unavailable)
    if hold_rules.get("upper_band_no_green") and pct_b is not None \
            and pct_b >= t["upper_band_pct_b"] and not green_bar:
        return Assessment("hold", "upper_band_no_green", "riding band", unavailable)
    if hold_rules.get("consecutive_small_rises") and len(r3) == 3 and all(
        0 < r < t["small_rise_max_pct"] for r in r3
    ):
        return Assessment("hold", "consecutive_small_rises", "", unavailable)
    return Assessment("hold", "no_signal", "", unavailable)
