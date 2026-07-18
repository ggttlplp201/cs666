"""Item universe for System B (config/universe_b.yaml).

The universe file carries the human-supplied inputs the HANDOFF (§B) says a
model can't infer — aesthetics scores, category/priority assignments, supply
estimates — plus per-item metadata for the factor model. Synthetic/backtest
runs can bypass it (metadata comes with the panel).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from shared_b.schema import ItemMeta, SourceStatus


def load_universe(path: Path) -> dict[str, ItemMeta]:
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, ItemMeta] = {}
    for entry in raw.get("items", []):
        d = dict(entry)
        name = d.pop("market_hash_name")
        status = d.pop("source_status", "active")
        known = {k: v for k, v in d.items() if k in ItemMeta.__dataclass_fields__}
        meta = ItemMeta(market_hash_name=name, source_status=SourceStatus(status), **known)
        out[name] = meta
    return out
