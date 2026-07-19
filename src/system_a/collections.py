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


def load_collection_map(path: Path, verified_only: bool = True) -> CollectionMap:
    data = yaml.safe_load(path.read_text())
    covert_to_case: dict[str, tuple[str, str]] = {}
    cases_with_gold: list[str] = []
    for section in ("glove_cases", "knife_cases"):
        for entry in data.get(section, []):
            cases_with_gold.append(entry["case"])
            if verified_only and not entry.get("covert_verified"):
                continue
            for covert in entry.get("coverts") or []:
                covert_to_case[covert] = (entry["case"], entry["gold"])
    return CollectionMap(
        covert_to_case=covert_to_case,
        cases_with_gold=cases_with_gold,
        map_collections_no_gold=data.get("map_collections_no_gold", []),
        verified=bool(data.get("meta", {}).get("verified_by_leon")),
    )
