"""Anti-monopoly concentration tracker (Leon's predictive thesis).

Valve pushes item-access updates to break trader monopolies on high-barrier,
thin-supply classes — knives were the archetype (2025-10-22 covert→gold gave
them trade-up access). The bet: the classes that MOST resemble pre-access
knives — high price barrier + thin available supply — are Valve's likely next
targets. This ranks them from iflow BUFF snapshots so the reactive engine has
a watchlist to point at.

Monopolization score per item = mean of two cross-sectional percentiles:
  barrier   = price percentile   (higher price = higher entry barrier)
  scarcity  = 1 − listings percentile (fewer listings = thinner, controllable)
Both in [0,1]; score in [0,1]. Aggregated to a class by median.

⚠ This is a SOFT, STRUCTURAL signal — a watchlist of *what* to watch, not a
dated prediction of *when*. We have exactly ONE historical access event
(knives/gloves, 2025-10-22), so the anchor check below is a sanity anchor
(n=1), NOT validation: run the score on a PRE-event snapshot and confirm it
would have ranked knives/gloves near the top. iflow's curated archive also
under-covers ultra-high-end trophies (Dragon Lore/Howl class), a known blind
spot for the very top of the barrier distribution.

Run:  PYTHONPATH=src python -m system_a.concentration
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from shared.iflow_history import parse_record

RIFLES = {"AK-47", "M4A4", "M4A1-S", "FAMAS", "Galil AR", "AUG", "SG 553"}
PISTOLS = {"Desert Eagle", "Glock-18", "USP-S", "P2000", "Five-SeveN", "Tec-9",
           "P250", "CZ75-Auto", "R8 Revolver", "Dual Berettas", "P90"}


def class_of(hash_name: str) -> str:
    """Coarse item class. Knives/gloves (the gold tiers) are the monopoly
    archetypes; weapon categories give the rest a comparable grouping."""
    if hash_name.startswith("★"):
        return "Gloves" if ("Gloves" in hash_name or "Hand Wraps" in hash_name) \
            else "Knife"
    base = hash_name.split(" |")[0].strip()
    if base == "AWP":
        return "AWP"
    if base in RIFLES:
        return "Rifle"
    if base in PISTOLS:
        return "Pistol"
    if base in ("MP9", "MP7", "MP5-SD", "MAC-10", "UMP-45", "PP-Bizon", "P90"):
        return "SMG"
    return "Other"


@dataclass(frozen=True)
class Row:
    hash_name: str
    price: float          # BUFF ask, USD
    listings: int


def load_snapshot(zip_path: Path) -> list[Row]:
    rows = []
    with zipfile.ZipFile(zip_path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in f:
                r = json.loads(line)
                if r.get("appid") != 730:
                    continue
                p = parse_record(r)
                if not p or not p[0]:
                    continue
                rows.append(Row(str(r.get("hash_name", "")), p[0], p[2]))
    return rows


def _percentiles(values: list[float]) -> dict[int, float]:
    """value → rank in [0,1] (ties share the midpoint rank)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = {}
    n = len(values)
    for pos, idx in enumerate(order):
        ranks[idx] = pos / (n - 1) if n > 1 else 0.5
    return ranks


@dataclass
class ClassScore:
    cls: str
    n: int
    median_price: float
    median_listings: float
    score: float          # 0..1 monopolization index (median of item scores)


def rank_classes(rows: list[Row], min_items: int = 5) -> list[ClassScore]:
    prices = [r.price for r in rows]
    listings = [float(max(r.listings, 1)) for r in rows]
    p_rank = _percentiles(prices)
    l_rank = _percentiles(listings)
    by_class: dict[str, list[tuple[float, float, float]]] = {}
    for i, r in enumerate(rows):
        barrier = p_rank[i]
        scarcity = 1.0 - l_rank[i]
        item_score = (barrier + scarcity) / 2
        by_class.setdefault(class_of(r.hash_name), []).append(
            (item_score, r.price, float(r.listings))
        )
    out = []
    for cls, items in by_class.items():
        if len(items) < min_items:
            continue
        out.append(ClassScore(
            cls=cls, n=len(items),
            median_price=statistics.median(p for _, p, _ in items),
            median_listings=statistics.median(l for _, _, l in items),
            score=statistics.median(s for s, _, _ in items),
        ))
    return sorted(out, key=lambda c: -c.score)


def _latest_snapshot(cache: Path) -> Path | None:
    zips = sorted(cache.glob("*.zip"))
    return zips[-1] if zips else None


def _snapshot_on_or_before(cache: Path, date_prefix: str) -> Path | None:
    zips = sorted(p for p in cache.glob("*.zip") if p.name[:10] <= date_prefix)
    return zips[-1] if zips else None


def main(argv=None) -> int:
    from shared.configuration import Config
    repo = Path(__file__).resolve().parents[2]
    config = Config.load(repo, system="system_a")
    cache = repo / config.require("data.iflow_archive")["cache_dir"]
    parser = argparse.ArgumentParser(description="Concentration / monopolization tracker")
    parser.add_argument("--snapshot", type=Path, default=None)
    args = parser.parse_args(argv)

    snap = args.snapshot or _latest_snapshot(cache)
    if snap is None:
        print("no iflow snapshots cached — run shared.iflow_history first")
        return 1
    rows = load_snapshot(snap)
    ranking = rank_classes(rows)

    opened = set(config.get("system_a.concentration", {}).get("opened_classes", []))
    print(f"== MONOPOLIZATION RANKING — {snap.name} ({len(rows)} items) ==")
    print(f"{'class':8} {'score':>6} {'med price':>10} {'med listings':>13} {'n':>5}  status")
    for c in ranking:
        tag = "OPENED (2025-10-22)" if c.cls in opened else ""
        print(f"{c.cls:8} {c.score:>6.2f} ${c.median_price:>9,.0f} "
              f"{c.median_listings:>13,.0f} {c.n:>5}  {tag}")
    candidates = [c for c in ranking if c.cls not in opened]
    nxt = candidates[0] if candidates else None
    if nxt:
        gap = ranking[0].score - nxt.score
        print(f"\nNext access candidate (excluding already-opened): "
              f"{nxt.cls} (score {nxt.score:.2f})")
        if gap > 0.2:
            print(f"  ⚠ but it scores {gap:.2f} BELOW the opened knife/glove tier — "
                  "monopolization is concentrated in classes already opened. The "
                  "remaining high-barrier targets (Contraband/discontinued trophies:"
                  " Dragon Lore, Howl, Medusa) are iflow's blind spot, so the "
                  "strongest 'next target' may be invisible to this data.")
    print("⚠ Soft structural watchlist — what to watch, not when. n=1 evidence.")

    # Anchor check (n=1 sanity, not validation): pre-2025-10-22 ranking.
    pre = _snapshot_on_or_before(cache, "2025-10-21")
    if pre and pre != snap:
        pre_rank = rank_classes(load_snapshot(pre))
        order = [c.cls for c in pre_rank]
        gold_pos = [i for i, c in enumerate(order) if c in ("Knife", "Gloves")]
        print(f"\n== ANCHOR CHECK (n=1) — ranking on {pre.name}, "
              "BEFORE the 2025-10-22 knife/glove access event ==")
        print(f"  pre-event order: {', '.join(order)}")
        if gold_pos and max(gold_pos) <= 1:
            print("  ✓ Knife/Gloves ranked in the top 2 → the score WOULD have "
                  "flagged the class Valve opened. Consistent (n=1), not proof.")
        else:
            print(f"  Knife/Gloves ranked at positions {gold_pos} — the score "
                  "did NOT cleanly flag the opened class; treat with caution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
