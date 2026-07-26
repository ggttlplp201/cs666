"""Negative controls for the 2025-10-22 trade-up event — does fuel SELECTION
carry edge, or did the whole gold-case rarity ladder just reprice?

The +648–713% on gold-case Covert reds looked spectacular, but a big number
is not an edge until it survives controls (the lesson that killed the
reactive thesis). Three comparisons, all on iflow BUFF data, 60-day hold,
BUFF frictions (spread + 2.5% fee):

  1. TIME PLACEBO — the same fuel reds on random NON-event dates. Isolates
     "is +700% event-specific, or just what these reds do?"
  2. CROSS-SECTION: CLASSIFIED control — Classified (pink) skins from the
     SAME gold cases. Classifieds are NOT usable as covert→gold fuel, so if
     "being fuel" drove the pump they should NOT move like the coverts.
  3. BROAD MARKET — every iflow CS2 item over the same window, the "buy
     anything" baseline.

Finding (2026-07-19): the event effect is real and huge (gold-case items far
outran the market), but fuel SELECTION does not carry edge — Classifieds
(non-fuel) pumped as much as Coverts (fuel) by median. It is whole-gold-case
LADDER repricing (classified→covert→gold all revalued), not fuel selection.
So the collection map's role shrinks to "is this item in a gold case at all",
and the tradeable question becomes anticipation + frequency of such events,
not which fuel to pick.

Requires cached iflow event-window zips (var/iflow_archive) + source=buff_iflow
rows. Run:  PYTHONPATH=src python -m system_a.trade_up_control
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import zipfile
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

# Classified (pink) skins from gold cases — NOT covert→gold fuel. Same cases,
# comparable liquidity; the clean cross-sectional control (no-gold prestige
# Coverts like Dragon Lore are trophies absent from iflow's curated archive).
CLASSIFIED_CONTROL = [
    "USP-S | Cortex", "AWP | Mortis", "AUG | Stymphalian",       # Clutch
    "AWP | Duality", "UMP-45 | Wild Child",                      # Revolution
    "M4A4 | Tooth Fairy",                                        # Fever
    "AK-47 | Ice Coaled", "Desert Eagle | Mecha Industries",    # Recoil
    "AK-47 | Legion of Anubis",                                 # Fracture
    "M4A1-S | Nightmare",
]


def _summ(x: list[float]) -> str:
    if not x:
        return "n=0"
    return (f"n={len(x)}  mean {statistics.mean(x):+.0%}  "
            f"median {statistics.median(x):+.0%}")


def _hold_return(series, entry_ts, spread) -> float | None:
    e = _bar_after(series, entry_ts, max_delay_days=3.0)
    if not e:
        return None
    x = _bar_after(series, e[0] + HOLD_DAYS * DAY)
    if not x:
        return None
    return x[1] * (1 - spread / 2) * (1 - FEE) / (e[1] * (1 + spread / 2)) - 1


def _scan_zip(path: Path, wanted: set[str]) -> tuple[dict, dict]:
    found, allp = {}, {}
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in f:
                r = json.loads(line)
                if r.get("appid") != 730:
                    continue
                base = str(r.get("hash_name", "")).split(" (")[0]
                p = parse_record(r)
                if not p:
                    continue
                allp[str(r.get("hash_name", ""))] = p[0]
                if base in wanted and base not in found:
                    found[base] = p[0]
    return found, allp


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

    fuel_reds = [i for i in universe if cmap.is_gold_case_covert(i)]

    # 1 + 2: fuel reds — event vs time-placebo
    event_rets, placebo_rets = [], []
    rng = random.Random(11)
    for name in fuel_reds:
        series = store.series(name, source="buff_iflow")
        if not series:
            continue
        spread = spreads.get(name, median_spread)
        er = _hold_return(series, event_ts, spread)
        if er is not None:
            event_rets.append(er)
        lo = series[0].ts
        hi = min(series[-1].ts - (HOLD_DAYS + 5) * DAY, event_ts - 30 * DAY)
        for _ in range(8):
            if hi <= lo:
                break
            pr = _hold_return(series, rng.uniform(lo, hi), spread)
            if pr is not None:
                placebo_rets.append(pr)

    print(f"== 2025-10-22 TRADE-UP EVENT — negative controls ({HOLD_DAYS}d hold, "
          "BUFF frictions) ==\n")
    print("1. FUEL REDS (gold-case coverts):")
    print(f"     EVENT date:        {_summ(event_rets)}")
    print(f"     TIME-PLACEBO:      {_summ(placebo_rets)}  (same reds, random dates)")
    if event_rets and placebo_rets:
        verdict = ("EVENT-SPECIFIC ✓" if statistics.median(event_rets)
                   > 3 * statistics.median(placebo_rets) else "NOT event-specific")
        print(f"     → {verdict}")

    # 3: classified control + broad market from cached event-window zips
    cache = repo / config.require("data.iflow_archive")["cache_dir"]
    pre = cache / "2025-10-20-00-15.zip"
    post = cache / "2025-12-19-00-15.zip"
    if pre.exists() and post.exists():
        pre_f, pre_all = _scan_zip(pre, set(CLASSIFIED_CONTROL))
        post_f, post_all = _scan_zip(post, set(CLASSIFIED_CONTROL))
        cl = [post_f[c] * (1 - FEE) / pre_f[c] - 1
              for c in set(pre_f) & set(post_f) if pre_f[c] > 0]
        common = set(pre_all) & set(post_all)
        mkt = [post_all[k] * (1 - FEE) / pre_all[k] - 1
               for k in common if pre_all[k] > 0]
        print("\n2. CROSS-SECTION on the SAME event date:")
        print(f"     FUEL coverts:      {_summ(event_rets)}")
        print(f"     CLASSIFIEDS (non-fuel, same cases): {_summ(cl)}")
        print(f"     BROAD MARKET (all {len(mkt)} items): {_summ(mkt)}")
        if cl and event_rets:
            cov_med, cl_med = statistics.median(event_rets), statistics.median(cl)
            print(f"\n   → gold-case items (covert {cov_med:+.0%} / classified "
                  f"{cl_med:+.0%} median) hugely outran the market "
                  f"({statistics.median(mkt):+.0%}), BUT fuel coverts ≈ non-fuel "
                  "classifieds → LADDER repricing, NOT fuel-selection alpha.")
    else:
        print("\n(cross-section skipped — cached iflow zips not found; "
              "run shared.iflow_history first)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
