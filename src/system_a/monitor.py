"""Social-media monitor agent (System A §7) — SHARED infrastructure.

Pipeline: sources → filter (allowlist + CS2 keywords) → classify → corroborate
across distinct sources → emit tiered Signals onto the shared bus.

Social content is UNTRUSTED input (§7.4): nothing here triggers a trade —
the reactive engine still requires market corroboration + liquidity checks.

Live sources need keys that are currently placeholders, so the only working
source is FileReplaySource (JSONL), which doubles as the test/backtest path.
The LLM classifier (AnthropicClassifier) activates when ANTHROPIC_API_KEY
lands; KeywordClassifier is the deterministic fallback and the paper-mode
default so behavior is reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from shared.bus import SignalBus
from shared.configuration import secret
from shared.schema import Direction, Signal, SignalType


@dataclass(frozen=True)
class RawPost:
    source: str          # account handle
    platform: str        # x | weibo | xiaohongshu | buff_forum | official_blog
    text: str
    ts: float


@dataclass(frozen=True)
class Classification:
    type: SignalType
    items: tuple[str, ...]
    direction: Direction
    confidence: float    # 0..1 before corroboration weighting


class SocialSource(Protocol):
    def poll(self) -> list[RawPost]: ...


class FileReplaySource:
    """JSONL of {source, platform, text, ts} — replay/testing path."""

    def __init__(self, path: Path):
        self.path = path
        self._consumed = False

    def poll(self) -> list[RawPost]:
        if self._consumed or not self.path.exists():
            return []
        self._consumed = True
        return [
            RawPost(**json.loads(line))
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]


class XApiSource:
    """X/Twitter polling — inactive until X_BEARER_TOKEN is supplied."""

    def __init__(self, handles: list[str]):
        self.handles = handles

    def poll(self) -> list[RawPost]:
        if secret("X_BEARER_TOKEN") is None:
            return []  # degrade gracefully; monitor keeps running on other sources
        raise NotImplementedError(
            "TODO(Leon): X API ingestion — endpoint choice depends on the API tier purchased"
        )


class Classifier(Protocol):
    def classify(self, post: RawPost, known_items: list[str]) -> Classification | None: ...


_BULLISH = re.compile(r"\b(buff(ed)?|discontinu\w+|removed from|retired|rework)\b", re.I)
_BEARISH = re.compile(r"\b(nerf(ed)?|re-?release|returning|armory|unbox\w*)\b", re.I)
_LEAK = re.compile(r"\b(leak\w*|datamin\w*|rumor|unannounced|upcoming)\b", re.I)
_OFFICIAL = re.compile(r"\b(release notes|update is live|patch notes|shipped)\b", re.I)
_HYPE = re.compile(r"\b(to the moon|100%|guaranteed|easy money|all[- ]?in|pump\w*)\b", re.I)
_CS2 = re.compile(r"\b(cs2|counter-?strike|skin|case|knife|glove|valve)\b", re.I)


class KeywordClassifier:
    """Deterministic rule-based classifier — paper-mode default."""

    def classify(self, post: RawPost, known_items: list[str]) -> Classification | None:
        text = post.text
        if not _CS2.search(text):
            return None
        items = tuple(
            name for name in known_items
            if _item_pattern(name).search(text)
        )
        if _HYPE.search(text):
            return Classification(SignalType.HYPE, items, Direction.BEARISH, 0.5)
        if not items:
            return None
        direction = Direction.UNCLEAR
        if _BULLISH.search(text):
            direction = Direction.BULLISH
        elif _BEARISH.search(text):
            direction = Direction.BEARISH
        if _OFFICIAL.search(text):
            return Classification(
                SignalType.OFFICIAL_ANNOUNCEMENT, items, direction, 0.9
            )
        if _LEAK.search(text):
            return Classification(SignalType.UPDATE_LEAK, items, direction, 0.6)
        return None


def _item_pattern(name: str) -> re.Pattern:
    # "M4A4 | Desolate Space (Field-Tested)" should match on weapon or skin name.
    base = name.split("(")[0]
    parts = [re.escape(p.strip()) for p in base.split("|") if p.strip()]
    return re.compile("|".join(parts), re.I)


class AnthropicClassifier:
    """LLM classification (§7.3) — activates when ANTHROPIC_API_KEY lands.

    Falls back to None (post skipped) when the key is a placeholder; callers
    should then be running KeywordClassifier instead.
    """

    MODEL = "claude-haiku-4-5-20251001"  # cheap, high-volume tagging tier
    URL = "https://api.anthropic.com/v1/messages"

    def classify(self, post: RawPost, known_items: list[str]) -> Classification | None:
        api_key = secret("ANTHROPIC_API_KEY")
        if api_key is None:
            return None
        prompt = (
            "Classify this social post about the CS2 skin market. Respond with ONLY "
            'JSON: {"type": "update_leak|official_announcement|hype|noise", '
            '"items": [<affected market_hash_names from the provided list>], '
            '"direction": "bullish|bearish|unclear", "confidence": 0.0-1.0}\n'
            f"Known items: {json.dumps(known_items)}\n"
            f"Post ({post.platform} @{post.source}): {post.text}"
        )
        body = json.dumps({
            "model": self.MODEL,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        request = urllib.request.Request(
            self.URL, data=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            payload = json.load(resp)
        parsed = json.loads(payload["content"][0]["text"])
        if parsed["type"] == "noise":
            return None
        return Classification(
            type=SignalType(parsed["type"]),
            items=tuple(parsed.get("items", [])),
            direction=Direction(parsed.get("direction", "unclear")),
            confidence=float(parsed.get("confidence", 0.5)),
        )


def load_allowlist(path: Path) -> dict[str, float]:
    data = yaml.safe_load(path.read_text())
    return {a["handle"]: float(a.get("weight", 0.5)) for a in data["accounts"]}


class MonitorAgent:
    def __init__(
        self,
        sources: list[SocialSource],
        classifier: Classifier,
        bus: SignalBus,
        allowlist: dict[str, float],
        known_items: list[str],
        corroboration_min_sources: int,
    ):
        self.sources = sources
        self.classifier = classifier
        self.bus = bus
        self.allowlist = allowlist
        self.known_items = known_items
        self.corroboration_min_sources = corroboration_min_sources
        self._seen_text_hashes: set[str] = set()
        # key → (classification, set of sources, first_seen_ts, best weight)
        self._pending: dict[str, tuple[Classification, set[str], float, float]] = {}

    def run_cycle(self, now_ts: float) -> list[Signal]:
        """Poll sources, classify, corroborate, publish. Returns new signals."""
        for source in self.sources:
            for post in source.poll():
                self._ingest(post)
        return self._emit(now_ts)

    def _ingest(self, post: RawPost) -> None:
        if post.source not in self.allowlist:
            return
        digest = hashlib.sha256(post.text.strip().lower().encode()).hexdigest()
        if digest in self._seen_text_hashes:
            return  # dedup reposts (§7.4)
        self._seen_text_hashes.add(digest)
        result = self.classifier.classify(post, self.known_items)
        if result is None:
            return
        key = f"{result.type.value}|{','.join(sorted(result.items))}"
        weight = self.allowlist[post.source]
        if key in self._pending:
            existing, sources, first_ts, best_weight = self._pending[key]
            sources.add(post.source)
            self._pending[key] = (
                existing, sources, min(first_ts, post.ts), max(best_weight, weight)
            )
        else:
            self._pending[key] = (result, {post.source}, post.ts, weight)

    def _emit(self, now_ts: float) -> list[Signal]:
        emitted = []
        for classification, sources, first_ts, best_weight in self._pending.values():
            corroboration = min(
                1.0, len(sources) / self.corroboration_min_sources
            )
            confidence = classification.confidence * best_weight * corroboration
            tier = 2 if classification.type == SignalType.OFFICIAL_ANNOUNCEMENT else 1
            signal = Signal(
                tier=tier,
                type=classification.type,
                items=classification.items,
                direction=classification.direction,
                confidence=round(confidence, 4),
                first_seen_ts=first_ts,
                sources=tuple(sorted(sources)),
            )
            self.bus.publish(signal)
            emitted.append(signal)
        return emitted
