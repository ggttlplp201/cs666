"""Standing watch for the trade-up event class — the thing that has to work.

WHY THIS EXISTS. `system_a.trade_up_lag` measured the 2025-10-22 event: a
gold-case basket beat the rest of the market by ~+68% entering same-day and
still ~+49% entering a WEEK late, decaying to nothing by T+14. So the class
does not need anticipation (untestable — n=1 event) and does not need to win a
speed race against bots (System A's doc concedes that is unwinnable). It needs
one thing: to *notice*, within days, that the event happened.

Nothing in the stack did that. `--poll` polls PRICES. The monitor could
classify announcements but was only driven by the demo/replay path, so no
process was watching the news between runs.

This is deliberately small and boring, because it has to survive being
forgotten for months:

  * one HTTP call to Steam's official news API per run (via the monitor's
    ScraplingSource, which degrades to [] on any failure rather than raising)
  * seen article ids persist in var/, so a restart neither re-alerts nor
    silently skips the backlog
  * an alert is appended to var/alerts.jsonl AND printed loudly, so it is
    visible whether you read files, cron mail, or a terminal
  * exit code 0 on a quiet run, 10 when something fired — cron/CI can branch

Run it from cron a few times a day; the window is ~10 days, so cadence is not
critical, but continuity is:

    0 */6 * * *  cd /path/to/system_a && make watch

Detection is pinned against the real 2025-10-22 announcement copy in
tests/test_monitor_trade_up_detection.py — the one event we have ground truth
for. A detector that would have missed it is worthless, so that test is the
gate on any change here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from shared.configuration import Config
from system_a.monitor import KeywordClassifier, RawPost, ScraplingSource

# Event rules worth waking a human for. `trade_up_pool_change` is the only
# class with a measured, cost-clearing edge (trade_up_lag); the others are
# logged but not alerted, to keep the alert channel meaningful.
ALERT_RULES = {"trade_up_pool_change"}
EXIT_ALERTED = 10


def _load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception:
        return set()   # corrupt state must not stop the watch


def _save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # keep the file bounded — only recent ids matter for dedupe
    path.write_text(json.dumps(sorted(seen)[-2000:]))


def _notify(title: str, body: str) -> None:
    """Best-effort desktop notification; never let it break the watch."""
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
            check=False, capture_output=True, timeout=10)
    except Exception:
        pass


def post_key(post: RawPost) -> str:
    """Stable identity for a post, across processes and restarts.

    Must NOT use builtin hash(): Python randomises string hashing per process
    (PYTHONHASHSEED), so a hash()-based key changes every run and the watch
    re-alerts on every post forever. Caught by running the watch twice in a
    row — both runs reported all 15 posts as new.
    """
    digest = hashlib.sha1(post.text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{post.platform}:{int(post.ts)}:{digest}"


def run_watch(
    posts: list[RawPost],
    seen: set[str],
    classifier=None,
    known_items: list[str] | None = None,
) -> tuple[list[dict], set[str]]:
    """Classify unseen posts; return (alerts, updated seen). Pure — no I/O."""
    classifier = classifier or KeywordClassifier()
    alerts: list[dict] = []
    for post in posts:
        key = post_key(post)
        if key in seen:
            continue
        seen.add(key)
        c = classifier.classify(post, known_items or [])
        if c is None or c.event_rule not in ALERT_RULES:
            continue
        alerts.append({
            "detected_at": time.time(),
            "posted_at": post.ts,
            "source": post.source,
            "platform": post.platform,
            "event_rule": c.event_rule,
            "signal_type": c.type.value,
            "confidence": c.confidence,
            "direction": c.direction.value,
            "excerpt": post.text[:400],
        })
    return alerts, seen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--alerts", type=Path, default=None)
    ap.add_argument("--quiet", action="store_true", help="only print on an alert")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parents[2]
    Config.load(repo, system="system_a")     # loads .env; keeps behaviour uniform
    state = args.state or repo / "var" / "watch_seen.json"
    alerts_path = args.alerts or repo / "var" / "alerts.jsonl"

    posts = ScraplingSource().poll()
    seen = _load_seen(state)
    known_before = len(seen)
    alerts, seen = run_watch(posts, seen)
    _save_seen(state, seen)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if not alerts:
        if not args.quiet:
            print(f"[{stamp}] watch: {len(posts)} posts fetched, "
                  f"{len(seen) - known_before} new, nothing actionable.")
            if not posts:
                print("           (0 posts — scrapling missing or the fetch failed; "
                      "this degrades silently BY DESIGN, so check it occasionally)")
        return 0

    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(alerts_path, "a", encoding="utf-8") as f:
        for a in alerts:
            f.write(json.dumps(a) + "\n")

    bar = "=" * 68
    print(f"\n{bar}\n  TRADE-UP EVENT DETECTED — {len(alerts)} alert(s)  [{stamp}]\n{bar}")
    for a in alerts:
        posted = time.strftime("%Y-%m-%d %H:%M", time.localtime(a["posted_at"]))
        print(f"  posted {posted} · {a['source']} · {a['signal_type']} "
              f"conf {a['confidence']}")
        print(f"  {a['excerpt'][:220]}...")
    print(f"\n  Measured window: a gold-case basket beat the market by ~+68% entering")
    print(f"  same-day and ~+49% a WEEK late; the excess is gone by T+14.")
    print(f"  Logged to {alerts_path}")
    print(f"  NOTE: config is DO-NOT-TRADE / log-only. This is a prompt to look,")
    print(f"  not an order. n=1 event — the next one may not behave the same.\n{bar}\n")
    if not args.no_notify:
        _notify("CS2 trade-up event detected",
                f"{len(alerts)} alert(s) — see var/alerts.jsonl")
    return EXIT_ALERTED


if __name__ == "__main__":
    sys.exit(main())
