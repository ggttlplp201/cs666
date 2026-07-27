"""How late can you be to a trade-up event and still capture it?

WHY THIS QUESTION, AND NOT "CAN WE PREDICT THE DATE".

The tradeable question left open by `trade_up_control` was "anticipation +
frequency of trade-up mechanic changes". Anticipation is **not testable here**:
the labeled event set contains exactly ONE trade-up mechanic change
(2025-10-22 — the rules table calls it "THE canonical event"; the 2025-10-30
entry is its T+7 echo, not a second event). You cannot fit or validate a timing
model on n=1, and there is no out-of-sample left to check it against. Any
"predictor" of that date would be a story, not a result.

But n=1 in TIME is not n=1 in the CROSS-SECTION. One event date, hundreds of
gold-case items, each with its own price path. That supports a different and
more useful question:

    You do not need to predict the date — the event is public the moment it
    ships. You need to know how much of the move survives arriving late.

If the edge survives a week's delay, neither anticipation nor a speed race is
required — a monitor that notices within days is enough, and System A's own doc
concedes the millisecond race is unwinnable against entrenched bots. If the edge
decays within hours, the whole event class is untradeable for us and should be
dropped, not chased.

The magnitude is what makes this class interesting at all: gold-case items ran
~40x the broad market, so a 4.4% BUFF round-trip spread — the cost that killed
System B's 3%/21-day edge — is noise against it.

Entry at EVENT+lag for a range of lags, hold HOLD_DAYS from entry, BUFF
frictions throughout. Run:  PYTHONPATH=src python -m system_a.trade_up_lag
"""

from __future__ import annotations

import json
import statistics
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

from shared.configuration import Config
from shared.iflow_history import parse_record
from shared.store import SnapshotStore
from system_a.collections import load_collection_map
from system_a.event_study import DAY, _bar_after, _event_ts
from system_a.spread_study import spread_stats

EVENT = "2025-10-22"
HOLD_DAYS = 60
FEE = 0.025
# Days after the announcement at which a buyer could realistically enter.
LAGS = [0, 1, 2, 3, 5, 7, 10, 14, 21, 30]


def lagged_return(series, event_ts: float, lag_days: int, spread: float) -> float | None:
    """Buy `lag_days` after the event, hold HOLD_DAYS, sell. Net of BUFF costs.

    The entry bar must land close to the intended lag — `max_delay_days=1.5`
    stops a sparse series from silently turning a "T+1 entry" into a T+10 one
    and flattering the late lags.
    """
    entry = _bar_after(series, event_ts + lag_days * DAY, max_delay_days=1.5)
    if not entry:
        return None
    exit_bar = _bar_after(series, entry[0] + HOLD_DAYS * DAY)
    if not exit_bar:
        return None
    buy = entry[1] * (1 + spread / 2)
    sell = exit_bar[1] * (1 - spread / 2) * (1 - FEE)
    return sell / buy - 1 if buy > 0 else None


def _zip_prices(cache: Path, day: date) -> dict[str, float]:
    """Every CS2 ask price in one archive snapshot, keyed by full hash_name.

    The 20-item poller universe is far too narrow here — only one gold-case
    item in it is priced at every lag, which is no cross-section at all. The
    archive holds the whole market on each day, which is what makes a balanced
    per-lag panel possible."""
    path = cache / f"{day.isoformat()}-00-15.zip"
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in f:
                r = json.loads(line)
                if r.get("appid") != 730:
                    continue
                p = parse_record(r)
                if p and p[0] > 0:
                    out[str(r.get("hash_name", ""))] = p[0]
    return out


def _summ(x: list[float]) -> str:
    if not x:
        return "n=0"
    return (f"n={len(x):3d}  median {statistics.median(x):+7.0%}  "
            f"mean {statistics.mean(x):+7.0%}  "
            f"win {sum(1 for v in x if v > 0) / len(x):.0%}")


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parents[2]
    config = Config.load(repo, system="system_a")
    store = SnapshotStore(repo / config.require("data.snapshot_poller")["db_path"])
    cmap = load_collection_map(repo / "config" / "trade_up_collections.yaml")
    seed = repo / config.require("data.steam_history")["items_file"]
    universe = sorted({l.strip() for l in seed.read_text().splitlines() if l.strip()})
    spreads = {s.item: s.median for s in spread_stats(store, source="buff_iflow")}
    median_spread = statistics.median(spreads.values()) if spreads else 0.04
    event_ts = _event_ts(EVENT)

    gold = [i for i in universe if cmap.is_gold_case_covert(i)]
    others = [i for i in universe if i not in gold]

    print(f"== ENTRY-LAG DECAY — {EVENT} trade-up event ==")
    print(f"   {HOLD_DAYS}d hold from entry, BUFF spread + {FEE:.1%} fee, "
          f"median spread {median_spread:.1%}")
    print(f"   gold-case items: {len(gold)}   non-gold control: {len(others)}\n")
    print(f"   {'lag':>4}  {'GOLD-CASE (the event basket)':<45}  {'NON-GOLD control':<40}")

    rows = []
    for lag in LAGS:
        g, o = [], []
        for name in gold:
            s = store.series(name, source="buff_iflow")
            if s:
                r = lagged_return(s, event_ts, lag, spreads.get(name, median_spread))
                if r is not None:
                    g.append(r)
        for name in others:
            s = store.series(name, source="buff_iflow")
            if s:
                r = lagged_return(s, event_ts, lag, spreads.get(name, median_spread))
                if r is not None:
                    o.append(r)
        rows.append((lag, g, o))
        print(f"   T+{lag:<3} {_summ(g):<45}  {_summ(o):<40}")

    # ---- wide cross-section from the archive --------------------------------
    cache = repo / config.require("data.iflow_archive")["cache_dir"]
    ev_day = date.fromisoformat(EVENT)
    print(f"\n== ARCHIVE CROSS-SECTION (full market, {cache.name}) ==")
    wide: dict[int, dict[str, float]] = {}
    for lag in LAGS:
        entry_prices = _zip_prices(cache, ev_day + timedelta(days=lag))
        exit_prices = _zip_prices(cache, ev_day + timedelta(days=lag + HOLD_DAYS))
        if not entry_prices or not exit_prices:
            continue
        rets = {}
        for name, p0 in entry_prices.items():
            p1 = exit_prices.get(name)
            if p1 is None:
                continue
            buy = p0 * (1 + median_spread / 2)
            sell = p1 * (1 - median_spread / 2) * (1 - FEE)
            rets[name] = sell / buy - 1
        wide[lag] = rets

    if wide:
        # The archive is a ROTATING sample, not a full daily census: two files a
        # day apart share only 36-74% of their names, so intersecting all ten
        # lags leaves nothing. Compare each lag to T+0 on the basket common to
        # THAT PAIR instead — still like-for-like (same items on both sides of
        # the comparison), and it keeps a usable n.
        base_lag = min(wide)
        print(f"   archive is a rotating sample — comparing each lag to T+{base_lag}")
        print("   on the basket common to that pair (n varies by pair, by design)\n")
        print(f"   {'lag':>5}  {'n':>4}  {'GOLD-CASE median':>17}  "
              f"{'REST median':>12}  {'gold excess':>12}")
        for lag in sorted(wide):
            common = set(wide[base_lag]) & set(wide[lag])
            gold_c = [n for n in common if cmap.is_gold_case_covert(n.split(" (")[0])]
            rest_c = [n for n in common if n not in set(gold_c)]
            if len(gold_c) < 5:
                print(f"   T+{lag:<3}  {len(gold_c):>4}  "
                      f"{'(too few to read)':>17}")
                continue
            g = statistics.median([wide[lag][n] for n in gold_c])
            o = statistics.median([wide[lag][n] for n in rest_c]) if rest_c else 0.0
            print(f"   T+{lag:<3}  {len(gold_c):>4}  {g:>16.0%}  {o:>11.0%}  "
                  f"{g - o:>11.0%}")

    # ---- verdict ------------------------------------------------------------
    # Read off the ARCHIVE cross-section, which is the only part of this study
    # with a real sample. The store-based curve at the top uses the 20-item
    # poller universe, where exactly one gold-case item is priced at every lag —
    # no cross-section at all — so it is printed for reference, not decided on.
    if wide:
        base_lag = min(wide)
        excess = {}
        for lag in sorted(wide):
            common = set(wide[base_lag]) & set(wide[lag])
            g = [wide[lag][n] for n in common
                 if cmap.is_gold_case_covert(n.split(" (")[0])]
            o = [wide[lag][n] for n in common
                 if not cmap.is_gold_case_covert(n.split(" (")[0])]
            if len(g) >= 5 and o:
                excess[lag] = statistics.median(g) - statistics.median(o)

        # Persist for the dashboard — recomputing this scans ~20 archive zips
        # and takes minutes, far too slow for a page load. The dashboard reads
        # the artifact and shows when it was produced, so a stale curve is
        # visible as stale rather than silently wrong.
        curve = []
        for lag in sorted(wide):
            common = set(wide[base_lag]) & set(wide[lag])
            g = [wide[lag][n] for n in common
                 if cmap.is_gold_case_covert(n.split(" (")[0])]
            o = [wide[lag][n] for n in common
                 if not cmap.is_gold_case_covert(n.split(" (")[0])]
            if len(g) >= 5 and o:
                curve.append({"lag": lag, "n": len(g),
                              "gold": statistics.median(g),
                              "rest": statistics.median(o),
                              "excess": statistics.median(g) - statistics.median(o)})
        out = repo / "var" / "lag_decay.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        # freshness comes from the artifact's own mtime — no clock field to
        # drift out of sync with the file
        out.write_text(json.dumps({
            "event": EVENT, "hold_days": HOLD_DAYS, "fee": FEE, "curve": curve,
        }, indent=1))
        print(f"\n   curve -> {out}")

        print("\n== VERDICT ==")
        if not excess:
            print("   NOT ESTABLISHED — no lag has a readable gold-case basket.")
            return 0
        week = [v for l, v in excess.items() if 1 <= l <= 7]
        late = [v for l, v in excess.items() if l >= 14]
        print(f"   gold-case EXCESS over the rest of the market, net of BUFF costs:")
        print(f"     same day   {excess.get(0, float('nan')):+.0%}")
        if week:
            print(f"     1-7 days   {statistics.median(week):+.0%}  (median across lags)")
        if late:
            print(f"     14+ days   {statistics.median(late):+.0%}")
        if week and statistics.median(week) > 0.20:
            print("\n   → Arriving a WEEK late still captures a large excess. This class")
            print("     does not need anticipation, and does not need to win a speed")
            print("     race against bots — it needs a monitor that notices within days.")
            print("     That is buildable; predicting Valve is not.")
        else:
            print("\n   → The excess is gone before a realistic reaction window.")

    print("\n   CAVEATS, and they are heavy:")
    print("     * n=1 IN TIME. One event (2025-10-22). This is that event's decay")
    print("       curve, not a law about trade-up events. Nothing here says the")
    print("       next one behaves the same way.")
    print("     * The iflow archive is a ROTATING sample (36-74% day-to-day name")
    print("       overlap), so each lag is compared to T+0 on its own pairwise")
    print("       basket and the n moves around a lot.")
    print("     * Anticipating the DATE remains untestable: one event cannot fit")
    print("       or validate a timing model, and there is no out-of-sample left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
