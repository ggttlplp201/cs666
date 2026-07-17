"""Rules-table item mapping (System A §4.1 step 2).

Translates a bus Signal about an entity (weapon, collection, or a concrete
item) into buy candidates and avoid/exit marks, using the cause→effect maps
in config/rules_table_a.yaml. Content is Leon-owned; this module only
interprets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from shared.schema import Direction, Signal, SignalType


@dataclass(frozen=True)
class MappedCandidate:
    market_hash_name: str
    direction: Direction     # bullish → buy candidate; bearish → avoid/exit
    rule: str                # provenance: which rule produced this


class RulesTable:
    def __init__(self, data: dict):
        self.substitute_pairs: list[tuple[str, str]] = [
            (a, b) for a, b in data.get("substitute_pairs", [])
        ]
        self.item_map: dict[str, list[str]] = data.get("item_map", {})

    @classmethod
    def load(cls, path: Path) -> "RulesTable":
        return cls(yaml.safe_load(path.read_text()))

    def substitute_of(self, entity: str) -> str | None:
        for a, b in self.substitute_pairs:
            if entity == a:
                return b
            if entity == b:
                return a
        return None

    def _resolve_entity(self, name: str) -> str | None:
        """Map a signal item string to an item_map key (weapon/collection)."""
        if name in self.item_map:
            return name
        for entity, items in self.item_map.items():
            if name in items or name.startswith(entity):
                return entity
        return None

    def map_signal(self, signal: Signal) -> list[MappedCandidate]:
        """Apply the substitute-pair effect template to a balance-change
        signal. Direction UNCLEAR maps to nothing — the engine waits for a
        clearer read rather than guessing."""
        if signal.direction == Direction.UNCLEAR:
            return []
        candidates: list[MappedCandidate] = []
        seen: set[tuple[str, Direction]] = set()

        def add(items: list[str], direction: Direction, rule: str) -> None:
            for item in items:
                key = (item, direction)
                if key not in seen:
                    seen.add(key)
                    candidates.append(MappedCandidate(item, direction, rule))

        for name in signal.items:
            entity = self._resolve_entity(name)
            if entity is None:
                continue
            add(self.item_map[entity], signal.direction, "rules_table.self")
            substitute = self.substitute_of(entity)
            if substitute and signal.type in (
                SignalType.UPDATE_LEAK,
                SignalType.OFFICIAL_ANNOUNCEMENT,
                SignalType.CONFIRMED_UPDATE,
            ):
                inverse = (
                    Direction.BULLISH
                    if signal.direction == Direction.BEARISH
                    else Direction.BEARISH
                )
                add(
                    self.item_map.get(substitute, []),
                    inverse,
                    "rules_table.substitute_pair",
                )
        return candidates
