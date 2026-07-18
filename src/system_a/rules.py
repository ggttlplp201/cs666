"""Rules-table interpretation (System A §4.1 step 2).

Consumes the researched config/rules_table_a.yaml (Leon-authored — "THIS
FILE IS THE ALPHA"): substitute_pairs, event_rules, historical_events, todo.

Hard rules encoded here, per the table and Leon's wiring spec:
  - Confidence gates trading: high/medium pairs+rules are tradeable
    pre-backtest; low = LOG ONLY. Rules in config system_a.disabled_rules
    (map_pool_change until the backtest disambiguates it) are log-only too.
  - calendar_esports_event is VOLUME-ONLY — it can never produce a
    directional candidate, only liquidity timing flags.
  - If both sides of a substitute pair move the same direction, there is NO
    substitution trade.
  - trade_up_pool_change is collection-aware: only reds in collections that
    contain a gold-tier output react, and cheap reds outrank expensive ones.
    The collection→gold map is a known gap (table §4 todo) — until it is
    filled, trade-up mapping emits log-only candidates.
  - The T+7 lock-expiry ECHO of a trade-up event is a scheduled follow-on
    (2025-10-22 → 2025-10-30 second dip); the engine schedules it.

Every candidate carries the rule id, confidence, and evidence string so the
provenance log (Shared §12) records how well-supported each decision was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from shared.schema import Direction, Signal, SignalType

TRADEABLE_CONFIDENCES = ("high", "medium")
VOLUME_ONLY_RULES = ("calendar_esports_event",)


@dataclass(frozen=True)
class SubstitutePair:
    id: str
    a: str
    b: str
    confidence: str
    evidence: str

    def other(self, weapon: str) -> str | None:
        if weapon == self.a:
            return self.b
        if weapon == self.b:
            return self.a
        return None


@dataclass(frozen=True)
class EventRule:
    id: str
    confidence: str
    directional: bool          # False = volume/liquidity only, never a trade
    raw: dict                  # full YAML entry (logic/timing/magnitude/evidence)


@dataclass(frozen=True)
class MappedCandidate:
    market_hash_name: str
    direction: Direction
    rule: str                  # event-rule id or "substitute_pair:<pair_id>"
    confidence: str
    evidence: str
    tradeable: bool            # False ⇒ log-only (low confidence / disabled / data gap)
    priority: float = 0.0      # higher = act first (cheap-red weighting etc.)


class RulesTable:
    def __init__(
        self,
        data: dict,
        disabled_rules: list[str] | None = None,
        disabled_pairs: list[str] | None = None,
    ):
        self.pairs = [
            SubstitutePair(
                id=p["id"], a=p["a"], b=p["b"],
                confidence=p.get("confidence", "low"),
                evidence=" ".join(str(p.get("evidence", "")).split()),
            )
            for p in data.get("substitute_pairs", [])
        ]
        self.event_rules = {
            r["id"]: EventRule(
                id=r["id"],
                confidence=r.get("confidence", "low"),
                directional=r["id"] not in VOLUME_ONLY_RULES,
                raw=r,
            )
            for r in data.get("event_rules", [])
        }
        self.historical_events = data.get("historical_events", [])
        self.todo = data.get("todo", [])
        self.disabled_rules = set(disabled_rules or [])
        # Pairs that failed the event-study backtest — log-only regardless of
        # confidence (config system_a.rules_gating.disabled_pairs).
        self.disabled_pairs = set(disabled_pairs or [])
        self.weapons = sorted(
            {p.a for p in self.pairs} | {p.b for p in self.pairs},
            key=len, reverse=True,   # match "MP5-SD" before "MP5", etc.
        )

    @classmethod
    def load(
        cls,
        path: Path,
        disabled_rules: list[str] | None = None,
        disabled_pairs: list[str] | None = None,
    ) -> "RulesTable":
        return cls(yaml.safe_load(path.read_text()), disabled_rules, disabled_pairs)

    # ------------------------------------------------------------------ #
    def rule_tradeable(self, rule_id: str) -> bool:
        rule = self.event_rules.get(rule_id)
        if rule is None or rule_id in self.disabled_rules:
            return False
        return rule.directional and rule.confidence in TRADEABLE_CONFIDENCES

    def pair_for(self, weapon: str) -> SubstitutePair | None:
        for pair in self.pairs:
            if pair.other(weapon) is not None:
                return pair
        return None

    def weapons_in(self, text: str) -> list[str]:
        """Extract known weapon names from free text (longest-first match)."""
        found = []
        for weapon in self.weapons:
            if re.search(rf"(?<![\w-]){re.escape(weapon)}(?![\w-])", text, re.I):
                found.append(weapon)
        return found

    def items_for_weapon(self, weapon: str, universe: list[str]) -> list[str]:
        return [name for name in universe if name.startswith(f"{weapon} |")]

    # ------------------------------------------------------------------ #
    def map_signal(
        self, signal: Signal, universe: list[str]
    ) -> list[MappedCandidate]:
        """Translate a bus signal into candidates against the tracked universe.

        Weapon-balance path: signal.items carry weapon names (or item names —
        their weapon is extracted). Direction is the change to the named
        weapon; the substitute inverts, gated on pair confidence. A MARKET_BREAK
        names a concrete item and maps to itself only (no pair inversion —
        the break says nothing about cause)."""
        if signal.direction == Direction.UNCLEAR:
            return []
        # Strict dispatch on the attributed event rule: anything that is not
        # weapon-balance (or an untagged/legacy signal) must NOT fall through
        # to the weapon-balance mapping — that would let disabled or
        # volume-only rules trade under a high-confidence rule's flag.
        if signal.type != SignalType.MARKET_BREAK and signal.event_rule not in (
            None, "weapon_balance_change",
        ):
            return []

        if signal.type == SignalType.MARKET_BREAK:
            return [
                MappedCandidate(
                    name, signal.direction, "market_break", "high",
                    "CUSUM structural break on the live feed (System-A §4.1a)",
                    tradeable=True,
                )
                for name in signal.items if name in universe
            ]

        rule = self.event_rules.get("weapon_balance_change")
        rule_ok = self.rule_tradeable("weapon_balance_change")
        evidence = " ".join(str(rule.raw.get("notes", "")).split()) if rule else ""

        # Resolve which known weapons the signal touches, keeping direction.
        weapon_directions: dict[str, Direction] = {}
        for entry in signal.items:
            for weapon in self.weapons_in(entry) or (
                [entry] if entry in self.weapons else []
            ):
                weapon_directions[weapon] = signal.direction

        candidates: list[MappedCandidate] = []
        seen: set[tuple[str, Direction]] = set()

        def add(names, direction, rule_id, confidence, ev, tradeable):
            for name in names:
                key = (name, direction)
                if key not in seen:
                    seen.add(key)
                    candidates.append(
                        MappedCandidate(name, direction, rule_id, confidence,
                                        ev, tradeable)
                    )

        for weapon, direction in weapon_directions.items():
            add(
                self.items_for_weapon(weapon, universe), direction,
                "weapon_balance_change.self",
                rule.confidence if rule else "low", evidence, rule_ok,
            )
            pair = self.pair_for(weapon)
            if pair is None:
                continue
            substitute = pair.other(weapon)
            if substitute in weapon_directions:
                # Substitute changed too: same direction ⇒ no substitution
                # trade (table §2 logic); different direction ⇒ it already
                # has its own self-mapping — either way, no inversion here.
                continue
            inverse = (
                Direction.BULLISH if direction == Direction.BEARISH
                else Direction.BEARISH
            )
            pair_tradeable = (
                rule_ok
                and pair.confidence in TRADEABLE_CONFIDENCES
                and pair.id not in self.disabled_pairs
            )
            add(
                self.items_for_weapon(substitute, universe), inverse,
                f"substitute_pair:{pair.id}", pair.confidence, pair.evidence,
                pair_tradeable,
            )
        return candidates

    def map_trade_up_signal(
        self,
        signal: Signal,
        universe: list[str],
        prices: dict[str, float],
        collections_with_gold: list[str],
    ) -> list[MappedCandidate]:
        """trade_up_pool_change mapping — collection-aware per the table:
        only reds in collections CONTAINING a gold-tier output react, and
        cheap reds outrank expensive ones (priority = 1/price). The
        collection→gold map is a table-§4 todo; while it is empty every
        candidate is log-only (never invent the mapping)."""
        rule = self.event_rules["trade_up_pool_change"]
        gap_open = not collections_with_gold
        tradeable = self.rule_tradeable("trade_up_pool_change") and not gap_open
        evidence = " ".join(str(rule.raw.get("magnitude", "")).split())
        if gap_open:
            evidence = "COLLECTION→GOLD MAP MISSING (rules table §4 todo) — " + evidence
        candidates = []
        for name in signal.items:
            if name not in universe:
                continue
            price = prices.get(name, 0.0)
            candidates.append(
                MappedCandidate(
                    name, signal.direction, "trade_up_pool_change",
                    rule.confidence, evidence, tradeable,
                    priority=(1.0 / price) if price > 0 else 0.0,
                )
            )
        return sorted(candidates, key=lambda c: -c.priority)
