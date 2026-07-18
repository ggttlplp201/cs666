"""Hard selection filters (System B §3.1a / Shared §4.3) — a gate BEFORE scoring.

All mandatory; an item failing any is unscoreable this cycle. Returns the
reasons so the journal can explain every exclusion (Shared §12 provenance).
"""

from __future__ import annotations

import pandas as pd

from shared.schema import ItemMeta


def hard_filter_reasons(
    feat: pd.Series,
    meta: ItemMeta | None,
    cfg_sel: dict,
    blocklist: set[str],
) -> list[str]:
    """Empty list = passes. feat is one row of the feature frame."""
    reasons: list[str] = []
    name = str(feat.name)

    if name in blocklist:
        reasons.append("blocklisted")

    supply = meta.supply if meta else 0
    hard_max = cfg_sel.get("supply_hard_exclude_above", 50000)
    lo_broad, hi_broad = cfg_sel.get("supply_broad", [10000, 30000])
    lo_sweet, _ = cfg_sel.get("supply_sweet_spot", [2000, 10000])
    if supply > hard_max:
        reasons.append(f"supply>{hard_max}")
    elif supply <= 0:
        reasons.append("supply_unknown")
    elif not (lo_sweet <= supply <= hi_broad):
        reasons.append("supply_out_of_band")

    case_min = cfg_sel.get("case_price_min_cny", 80)
    if meta is None or meta.case_price_cny < case_min:
        reasons.append(f"case_price<{case_min}")

    min_bids = cfg_sel.get("min_valid_buy_orders", 3)
    vbo = feat.get("valid_buy_orders", -1)
    if vbo is not None and vbo >= 0:
        if vbo < min_bids:
            reasons.append(f"valid_buy_orders<{min_bids}")
    elif feat.get("buy_order_count", 0) < min_bids:
        reasons.append(f"buy_orders<{min_bids}")

    min_trades = cfg_sel.get("min_daily_trades", 10)
    if feat.get("volume_avg_20", 0.0) < min_trades:
        reasons.append(f"avg_volume<{min_trades}")

    if feat.get("pump_flag", 0):
        reasons.append("late_stage_pump_shape")
    if feat.get("attention_late", 0.0) > 0:
        reasons.append("late_parabolic_attention")

    return reasons


def apply_hard_filters(
    features: pd.DataFrame,
    meta_map: dict[str, ItemMeta],
    cfg_sel: dict,
    blocklist: set[str],
    allowlist: set[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Split the cross-section into (passing frame, {item: rejection reasons})."""
    rejected: dict[str, list[str]] = {}
    keep: list[str] = []
    # gates the allowlist may NOT bypass: safety, not structural opinion
    SAFETY = ("blocklisted", "late_stage_pump_shape", "late_parabolic_attention",
              "avg_volume", "valid_buy_orders", "buy_orders")
    for item, row in features.iterrows():
        item = str(item)
        reasons = hard_filter_reasons(row, meta_map.get(item), cfg_sel, blocklist)
        if allowlist and item in allowlist:
            reasons = [r for r in reasons if any(r.startswith(s) for s in SAFETY)]
        if reasons:
            rejected[item] = reasons
        else:
            keep.append(item)
    return features.loc[keep], rejected
