"""Decision-provenance logging (Shared §12).

Every trade decision — including refusals — appends one JSONL record with
its inputs, the signals that fired, the score, the rule that triggered, and
the regime, so any decision is replayable ("why did it buy that?").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ProvenanceLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        ts: float,
        action: str,           # e.g. buy_placed | buy_refused | sell_placed | pipeline_paused
        item: str | None,
        rule: str,             # the rule/gate that produced this outcome
        regime: str,
        signals: list[dict[str, Any]],
        inputs: dict[str, Any],
        score: float | None = None,
        order_id: str | None = None,
    ) -> None:
        record = {
            "ts": ts, "action": action, "item": item, "rule": rule,
            "regime": regime, "signals": signals, "inputs": inputs,
            "score": score, "order_id": order_id,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]
