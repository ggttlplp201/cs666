"""Decision provenance + trade journal (Shared §9, §12).

Every order must be replayable: inputs, signals fired, score, rule, regime.
JSONL so it's greppable and feedable back into attribution ("which signals
actually carry edge net of fees"). No log = wasted experience.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if hasattr(o, "value"):  # enums
        return o.value
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


class Journal:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **payload: Any) -> dict:
        rec = {"kind": kind, **payload}
        self.records.append(rec)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=_default) + "\n")
        return rec

    # convenience wrappers -----------------------------------------------------
    def decision(self, day: date, item: str, action: str, rule: str, regime: str,
                 score: float | None = None, signals: dict | None = None,
                 detail: dict | None = None) -> None:
        self.log(
            "decision", day=day, item=item, action=action, rule=rule,
            regime=regime, score=score, signals=signals or {}, detail=detail or {},
        )

    def trade(self, lot: Any, day: date, note: str = "") -> None:
        self.log("trade", day=day, lot=lot, note=note)

    def cycle(self, day: date, regime: str, equity: float, cash: float,
              deployed_pct: float, locked_value: float, extra: dict | None = None) -> None:
        self.log(
            "cycle", day=day, regime=regime, equity=equity, cash=cash,
            deployed_pct=deployed_pct, locked_value=locked_value, **(extra or {}),
        )
