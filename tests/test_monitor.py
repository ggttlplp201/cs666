import json
from pathlib import Path

from shared.bus import SignalBus
from shared.schema import Direction, SignalType
from system_a.monitor import (
    FileReplaySource, KeywordClassifier, MonitorAgent, RawPost, XApiSource,
    load_allowlist,
)

KNOWN = [
    "M4A4 | Desolate Space (Field-Tested)",
    "M4A1-S | Decimator (Field-Tested)",
]
ALLOW = {"trusted_leaker": 0.8, "official": 1.0, "rando_weight": 0.3}


def _agent(bus=None, min_sources=3):
    return MonitorAgent(
        sources=[], classifier=KeywordClassifier(), bus=bus or SignalBus(),
        allowlist=ALLOW, known_items=KNOWN,
        corroboration_min_sources=min_sources,
    )


def _post(text, source="trusted_leaker", ts=100.0):
    return RawPost(source=source, platform="x", text=text, ts=ts)


class TestKeywordClassifier:
    def test_leak_with_nerf_is_bearish_leak(self):
        c = KeywordClassifier().classify(
            _post("Datamined: CS2 update will nerf the M4A1-S next patch"), KNOWN
        )
        assert c.type == SignalType.UPDATE_LEAK
        assert c.direction == Direction.BEARISH
        assert c.items == ("M4A1-S | Decimator (Field-Tested)",)

    def test_official_patch_notes_high_confidence(self):
        c = KeywordClassifier().classify(
            _post("CS2 release notes: M4A4 damage buffed, update is live"), KNOWN
        )
        assert c.type == SignalType.OFFICIAL_ANNOUNCEMENT
        assert c.confidence == 0.9
        assert c.direction == Direction.BULLISH

    def test_hype_flagged_even_without_known_item(self):
        c = KeywordClassifier().classify(
            _post("this CS2 skin is guaranteed easy money, all in boys"), KNOWN
        )
        assert c.type == SignalType.HYPE

    def test_non_cs2_and_itemless_posts_ignored(self):
        assert KeywordClassifier().classify(_post("stocks nerfed lol"), KNOWN) is None
        assert (
            KeywordClassifier().classify(
                _post("CS2 update leaked, huge changes coming"), KNOWN
            )
            is None
        )  # no known item matched → nothing actionable


class TestMonitorAgent:
    def test_corroboration_scales_confidence(self):
        agent = _agent(min_sources=3)
        text = "Leak: next CS2 update nerfs the M4A1-S"
        agent._ingest(_post(text, source="trusted_leaker"))
        signals = agent._emit(now_ts=200.0)
        single = signals[0].confidence
        agent2 = _agent(min_sources=3)
        variants = [  # distinct wordings, same classification key
            ("Leak: next CS2 update nerfs the M4A1-S", "trusted_leaker"),
            ("Datamined M4A1-S nerf coming in the CS2 update", "official"),
            ("rumor: upcoming CS2 patch will nerf M4A1-S", "rando_weight"),
        ]
        for text2, src in variants:
            agent2._ingest(_post(text2, source=src))
        corroborated = agent2._emit(now_ts=200.0)[0]
        assert corroborated.confidence > single
        assert set(corroborated.sources) == {"trusted_leaker", "official", "rando_weight"}

    def test_dedup_and_allowlist_filter(self):
        agent = _agent()
        text = "Leak: CS2 update nerfs the M4A1-S"
        agent._ingest(_post(text))
        agent._ingest(_post(text))  # repost — deduped
        agent._ingest(_post("Rumor: CS2 M4A4 getting removed from drops", source="unknown_account"))
        signals = agent._emit(now_ts=200.0)
        assert len(signals) == 1
        assert len(signals[0].sources) == 1

    def test_official_is_tier2_leak_is_tier1(self):
        bus = SignalBus()
        agent = _agent(bus=bus)
        agent._ingest(_post("CS2 release notes: M4A1-S nerfed, update is live", source="official"))
        agent._ingest(_post("leak: CS2 M4A4 buff datamined", source="trusted_leaker"))
        agent._emit(now_ts=200.0)
        assert len(bus.active([2], 200.0, 48)) == 1
        assert len(bus.active([1], 200.0, 48)) == 1

    def test_file_replay_source_and_cycle(self, tmp_path: Path):
        posts = [
            {"source": "trusted_leaker", "platform": "x",
             "text": "Datamined: M4A1-S nerf in upcoming CS2 patch", "ts": 50.0},
        ]
        f = tmp_path / "posts.jsonl"
        f.write_text("\n".join(json.dumps(p) for p in posts))
        source = FileReplaySource(f)
        bus = SignalBus()
        agent = MonitorAgent(
            sources=[source], classifier=KeywordClassifier(), bus=bus,
            allowlist=ALLOW, known_items=KNOWN, corroboration_min_sources=3,
        )
        emitted = agent.run_cycle(now_ts=100.0)
        assert len(emitted) == 1
        assert source.poll() == []  # replay consumed once

    def test_x_source_degrades_without_key(self, monkeypatch):
        monkeypatch.setenv("X_BEARER_TOKEN", "PLACEHOLDER")
        assert XApiSource(["CounterStrike"]).poll() == []


def test_load_allowlist_from_config():
    weights = load_allowlist(
        Path(__file__).resolve().parents[1] / "config" / "monitor_allowlist.yaml"
    )
    assert weights["CounterStrike"] == 1.0
    assert 0 < weights["example_leaker"] < 1


def test_contradictory_directions_never_corroborate():
    agent = _agent(min_sources=3)
    agent._ingest(_post("Leak: CS2 update will nerf the M4A1-S", source="trusted_leaker"))
    agent._ingest(_post("Datamined: CS2 update buffs the M4A1-S", source="official"))
    signals = agent._emit(now_ts=200.0)
    assert len(signals) == 2                      # separate signals, not merged
    assert all(len(s.sources) == 1 for s in signals)
