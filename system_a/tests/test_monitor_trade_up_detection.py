"""Would the monitor actually have caught the 2025-10-22 trade-up event?

This is the only question that decides whether the trade-up class is tradeable
for us. The entry-lag study (`system_a.trade_up_lag`) measured a ~10-day window
in which a late entry still captures a large excess — but only if something
tells us the event happened. A detector that misses the one event we have
ground truth for is worthless, and a keyword rule can regress silently.

So the real Steam announcements from that week are frozen in
`tests/fixtures/steam_news_2025-10-22.json` (fetched live from the Steam news
API for app 730) and asserted against directly. The 2025-10-22 "Counter-Strike
2 Update" post is the actual copy that shipped the Covert->gold trade-up
change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.schema import SignalType
from system_a.monitor import KeywordClassifier, RawPost

FIXTURE = Path(__file__).parent / "fixtures" / "steam_news_2025-10-22.json"


def _posts() -> list[tuple[str, str, RawPost]]:
    items = json.loads(FIXTURE.read_text())
    out = []
    for i in items:
        text = f"{i['title']}. {i['contents']}"
        out.append((i["day"], i["title"], RawPost(
            source="Steam news (app 730)", platform="official_blog",
            text=text, ts=float(i["date"]),
        )))
    return out


def _the_event() -> RawPost:
    for day, title, post in _posts():
        if day == "2025-10-22" and title == "Counter-Strike 2 Update":
            return post
    pytest.fail("the 2025-10-22 update is missing from the fixture")


def test_fixture_contains_the_real_announcement_copy():
    """Guard the ground truth itself: if the fixture stops containing Valve's
    actual wording, every assertion below becomes meaningless."""
    text = _the_event().text.lower()
    assert "trade up contract" in text
    assert "covert" in text


def test_the_event_is_detected():
    c = KeywordClassifier().classify(_the_event(), known_items=[])
    assert c is not None, "the monitor would have MISSED the canonical event"
    assert c.event_rule == "trade_up_pool_change"


def test_valve_first_party_copy_is_not_demoted_to_a_leak():
    """Valve titles its posts "Counter-Strike 2 Update" and never writes "patch
    notes", so a wording-based officialness test rated its own announcement a
    0.6 leak. Officialness comes from the SOURCE."""
    c = KeywordClassifier().classify(_the_event(), known_items=[])
    assert c.type is SignalType.OFFICIAL_ANNOUNCEMENT
    assert c.confidence >= 0.9


def test_a_third_party_repost_of_the_same_text_stays_a_leak():
    """The upgrade must come from provenance, not from the words — identical
    copy on an unofficial platform is still unverified."""
    ev = _the_event()
    rumour = RawPost(source="some_leaker", platform="x", text=ev.text, ts=ev.ts)
    c = KeywordClassifier().classify(rumour, known_items=[])
    assert c is not None and c.event_rule == "trade_up_pool_change"
    assert c.type is SignalType.UPDATE_LEAK
    assert c.confidence < 0.9


def test_ordinary_updates_that_week_do_not_fire_the_trade_up_rule():
    """The window is ~10 days; a detector that cries wolf on every routine
    patch is as useless as one that stays silent."""
    clf = KeywordClassifier()
    fired = []
    for day, title, post in _posts():
        c = clf.classify(post, known_items=[])
        if c is not None and c.event_rule == "trade_up_pool_change":
            fired.append((day, title))
    assert fired == [("2025-10-22", "Counter-Strike 2 Update")], (
        f"expected exactly the event post to fire, got {fired}")


def test_truncated_content_still_catches_it():
    """ScraplingSource requests maxlength=1200 and the Trade Up line sits deep
    in the body — the signal must survive the truncation we actually use."""
    ev = _the_event()
    clipped = RawPost(source=ev.source, platform=ev.platform,
                      text=ev.text[:1200], ts=ev.ts)
    c = KeywordClassifier().classify(clipped, known_items=[])
    assert c is not None and c.event_rule == "trade_up_pool_change"
