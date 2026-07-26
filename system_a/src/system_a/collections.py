"""CS2 trade-up collection map (config/trade_up_collections.yaml).

Answers the two questions the trade-up rule needs:
  - which of our tracked Coverts sit in a case WITH a gold output (so a gold
    trade-up change makes them viable fuel → bullish), and
  - what gold TYPE (knife/glove) each maps to.

Only entries flagged covert_verified in the YAML are treated as known; the
rest are gold-type-only until Leon verifies (research is unverified — see the
YAML header). `verified_only=True` (default) uses just the confirmed lists so
the backtest never trades on an unverified red.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CollectionMap:
    # market_hash_name (weapon skin, wear-agnostic) -> (case, gold_type)
    covert_to_case: dict[str, tuple[str, str]]
    cases_with_gold: list[str]
    map_collections_no_gold: list[str]
    verified: bool

    def gold_type_for(self, market_hash_name: str) -> str | None:
        """'knife' | 'glove' if this item is a known gold-case Covert, else None.
        Matches wear-agnostically ('AK-47 | Nightwish (Field-Tested)' →
        'AK-47 | Nightwish')."""
        base = market_hash_name.split(" (")[0]
        entry = self.covert_to_case.get(base)
        return entry[1] if entry else None

    def is_gold_case_covert(self, market_hash_name: str) -> bool:
        return self.gold_type_for(market_hash_name) is not None


def _is_verified(flag) -> bool:
    """covert_verified may be True, 'leon', or 'partial' (some cases in a
    family verified). Treat true/leon as verified; 'partial' as verified for
    the cases it does list (the file only lists confirmed coverts)."""
    return flag is True or str(flag).lower() in ("true", "leon", "partial")


def load_collection_map(path: Path, verified_only: bool = True) -> CollectionMap:
    data = yaml.safe_load(path.read_text())
    covert_to_case: dict[str, tuple[str, str]] = {}
    cases_with_gold: list[str] = []

    # Glove cases: flat covert list, gold_type per case.
    for entry in data.get("glove_cases", []):
        cases_with_gold.append(entry["case"])
        if verified_only and not _is_verified(entry.get("covert_verified")):
            continue
        for covert in entry.get("coverts") or []:
            covert_to_case[covert] = (entry["case"], entry.get("gold_type", "glove"))

    # Knife cases: grouped by model family; coverts is a dict keyed by case.
    for family in data.get("knife_case_families", []):
        cases_with_gold.extend(family.get("cases", []))
        if verified_only and not _is_verified(family.get("covert_verified")):
            continue
        coverts_by_case = family.get("coverts") or {}
        for case_name, coverts in coverts_by_case.items():
            for covert in coverts or []:
                covert_to_case[covert] = (case_name, "knife")

    return CollectionMap(
        covert_to_case=covert_to_case,
        cases_with_gold=sorted(set(cases_with_gold)),
        map_collections_no_gold=data.get("map_collections_no_gold", []),
        verified=bool(data.get("meta", {}).get("independent_corroboration")),
    )
