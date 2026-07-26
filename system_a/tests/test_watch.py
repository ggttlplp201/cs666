"""The standing watch: it must fire on the real event, stay quiet otherwise,
and survive being forgotten for months.

The measured window is ~10 days, so the failure modes that matter are silent
ones — a watch that stops fetching, double-alerts until it is ignored, or loses
its state on restart. Each is pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

from shared.schema import SignalType
from system_a.monitor import RawPost
from system_a.watch import (
    ALERT_RULES, EXIT_ALERTED, _load_seen, _save_seen, run_watch,
)

FIXTURE = Path(__file__).parent / "fixtures" / "steam_news_2025-10-22.json"


def _real_posts() -> list[RawPost]:
    return [
        RawPost(source="Steam news (app 730)", platform="official_blog",
                text=f"{i['title']}. {i['contents']}", ts=float(i["date"]))
        for i in json.loads(FIXTURE.read_text())
    ]


def test_fires_on_the_real_event_week():
    """The whole point: replay the actual week and get exactly one alert."""
    alerts, _ = run_watch(_real_posts(), set())
    assert len(alerts) == 1, f"expected 1 alert, got {len(alerts)}"
    a = alerts[0]
    assert a["event_rule"] == "trade_up_pool_change"
    assert a["signal_type"] == SignalType.OFFICIAL_ANNOUNCEMENT.value
    assert a["confidence"] >= 0.9


def test_routine_updates_alone_are_silent():
    """A channel that cries wolf gets muted, and then the window is missed."""
    routine = [p for p in _real_posts() if "trade up contract" not in p.text.lower()]
    assert routine, "fixture should contain non-event posts"
    alerts, _ = run_watch(routine, set())
    assert alerts == []


def test_the_same_post_never_alerts_twice():
    posts = _real_posts()
    first, seen = run_watch(posts, set())
    assert len(first) == 1
    second, _ = run_watch(posts, seen)
    assert second == [], "re-alerted on an already-seen post"


def test_a_new_event_still_fires_after_earlier_posts_were_seen():
    """Dedupe must not swallow the next event — the one that actually pays."""
    posts = _real_posts()
    event = next(p for p in posts if "trade up contract" in p.text.lower())
    routine = [p for p in posts if p is not event]

    _, seen = run_watch(routine, set())          # quiet week already processed
    alerts, _ = run_watch([event], seen)
    assert len(alerts) == 1


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "var" / "watch_seen.json"
    _, seen = run_watch(_real_posts(), set())
    _save_seen(path, seen)
    again, _ = run_watch(_real_posts(), _load_seen(path))
    assert again == [], "state did not survive the round-trip; would re-alert"


def test_corrupt_state_does_not_stop_the_watch(tmp_path):
    """A watch that dies on a bad state file is a watch that is not running."""
    path = tmp_path / "watch_seen.json"
    path.write_text("{not json at all")
    assert _load_seen(path) == set()
    alerts, _ = run_watch(_real_posts(), _load_seen(path))
    assert len(alerts) == 1


def test_empty_fetch_is_quiet_not_crashing():
    """ScraplingSource returns [] on any failure by design; that must be a
    quiet run, never an exception."""
    alerts, seen = run_watch([], set())
    assert alerts == [] and seen == set()


def test_only_the_measured_edge_class_alerts():
    """Alerting on rules with no measured edge would dilute the channel."""
    assert ALERT_RULES == {"trade_up_pool_change"}


def test_alert_carries_enough_to_act_on():
    alerts, _ = run_watch(_real_posts(), set())
    a = alerts[0]
    for key in ("detected_at", "posted_at", "source", "confidence", "excerpt"):
        assert key in a and a[key] not in (None, "")
    assert EXIT_ALERTED != 0, "cron needs a distinct exit code to branch on"


def test_post_identity_is_stable_across_processes():
    """Regression: the key originally used builtin hash(), which Python
    randomises per process (PYTHONHASHSEED). Every run then saw every post as
    new and re-alerted forever. Unit tests passed because they ran in ONE
    process — only running the watch twice on the command line exposed it."""
    import subprocess
    import sys as _sys

    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from system_a.monitor import RawPost;"
        "from system_a.watch import post_key;"
        "print(post_key(RawPost(source='s', platform='official_blog',"
        " text='Trade Up Contract covert', ts=1761091200.0)))"
    )
    keys = set()
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [_sys.executable, "-c", code],
            capture_output=True, text=True, cwd=str(Path(__file__).parents[1]),
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert out.returncode == 0, out.stderr
        keys.add(out.stdout.strip())
    assert len(keys) == 1, f"key varies by PYTHONHASHSEED: {keys}"
