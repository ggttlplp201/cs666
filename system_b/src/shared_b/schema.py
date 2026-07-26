"""Normalization schema (System A doc §2.3) + core domain types shared by both systems.

Every data vendor maps into `ItemDay` (one normalized daily record per item) and
`ItemMeta` (slow-moving structural metadata). Vendor field names live in config,
never in strategy code.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


class Regime(str, enum.Enum):
    """Shared §2 — market regime; gates deployment ceilings in both systems."""

    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    WEAK = "weak"


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class SourceStatus(str, enum.Enum):
    """Shared §4.1 — production/source state of an item's drop pool."""

    ACTIVE = "active"          # still drops / case still in rotation
    RETIRED = "retired"        # out of active rotation, could return (Armory risk)
    DISCONTINUED = "discontinued"  # collection discontinued; supply capped


@dataclass(frozen=True)
class ItemDay:
    """One normalized daily observation for one item (System A §2.3).

    `volume` is EXECUTED trades (成交量), never listings (在售量) — the
    distinction Shared §3.3 calls critical.
    """

    market_hash_name: str
    day: date
    sell_price: float          # buff_lowest_sell_cny
    buy_price: float           # buff_highest_buy_cny
    listing_count: int         # sell-side depth proxy
    buy_order_count: int       # bid-side depth proxy
    volume: int                # buff_volume_24h — executed trades
    valid_buy_orders: int      # bids near market (Shared §4.3); -1 = unknown
    variant: str | None = None
    float_range: tuple[float, float] | None = None
    cross_market: dict[str, float] = field(default_factory=dict)


@dataclass
class ItemMeta:
    """Structural metadata for the factor model (Shared §4, System B §3.1).

    `aesthetics` is the one human-supplied factor (HANDOFF §B): 0..1 rank
    within its weapon category.
    """

    market_hash_name: str
    weapon: str = ""
    category: str = "other"    # mid_tier_primary | small_item | glove | knife | material | collection | sticker | other
    collection: str = ""
    source_status: SourceStatus = SourceStatus.ACTIVE
    rerelease_risk: float = 0.0    # 0..1 probability-ish of Armory re-release (bearish)
    case_price_cny: float = 0.0    # opening-cost proxy (Shared §4.3: >= 80 CNY)
    supply: int = 0                # circulating supply (存世量), the traded wear tier
    supply_fn: int = 0             # Factory New supply if known
    aesthetics: float = 0.5        # human-curated 0..1
    is_primary: bool = False       # AK-47/M4A1-S/M4A4/USP-S/Glock/AWP
    is_secondary_primary: bool = False  # Galil-tier — the fresh-edge tier this cycle
    substitute: str | None = None  # substitute-pair partner (Nikolaenko)
    notes: str = ""


@dataclass
class BusSignal:
    """Event on the shared tiered signal bus (System A §7.5)."""

    tier: int                          # 1 | 2 | 3
    type: str                          # update_leak | official_announcement | confirmed_update | hype | attention
    items: list[str]
    direction: str = "unclear"         # bullish | bearish | unclear
    confidence: float = 0.0
    attention_score: float = 0.0       # Tier 3: mention volume vs baseline
    sentiment: float = 0.0             # Tier 3: -1..1
    first_seen_ts: datetime | None = None
    sources: list[str] = field(default_factory=list)


@dataclass
class Order:
    """Client-side order with idempotent id (System A §6)."""

    item: str
    side: Side
    qty: int
    limit_price: float
    day: date                       # decision day; fills settle next cycle
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    reason: str = ""                # provenance tag (which rule emitted it)
    lot_id: str | None = None       # for sells: which lot is being closed
    batch_index: int = 0            # staged-entry batch number (System B §5)


@dataclass
class Fill:
    order: Order
    fill_day: date
    fill_price: float               # price actually paid/received, pre-fee
    qty: int                        # may be < order.qty (thin book)
    fee: float                      # CNY, charged on sells (BUFF ~2.5%)


@dataclass
class Lot:
    """One purchased batch; the unit the T+7 lock applies to (System A §5.3)."""

    lot_id: str
    item: str
    qty: int
    buy_price: float                # per unit, pre-fee
    buy_fee: float                  # total CNY fee paid on entry (0 on BUFF)
    buy_day: date
    unlock_day: date                # buy_day + trade_lock_days
    batch_index: int = 0            # 0 = first batch of the staged build
    thesis: str = ""                # written thesis (System B §8.3)
    invalidation: str = ""          # what kills the thesis
    sell_day: date | None = None
    sell_price: float | None = None  # per unit, pre-fee
    sell_fee: float = 0.0
    exit_reason: str = ""

    @property
    def open(self) -> bool:
        return self.sell_day is None

    def locked(self, on_day: date) -> bool:
        return self.open and on_day < self.unlock_day

    @property
    def cost(self) -> float:
        return self.qty * self.buy_price + self.buy_fee

    def realized_pnl(self) -> float:
        if self.open or self.sell_price is None:
            return 0.0
        return self.qty * (self.sell_price - self.buy_price) - self.sell_fee - self.buy_fee


def unlock_day_for(buy_day: date, trade_lock_days: int) -> date:
    return buy_day + timedelta(days=trade_lock_days)


def to_record(obj: Any) -> dict:
    """Loose dataclass -> dict for journaling (handles enums/dates)."""
    import dataclasses

    def conv(v: Any) -> Any:
        if isinstance(v, enum.Enum):
            return v.value
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return {k: conv(w) for k, w in dataclasses.asdict(v).items()}
        if isinstance(v, dict):
            return {k: conv(w) for k, w in v.items()}
        if isinstance(v, (list, tuple)):
            return [conv(w) for w in v]
        return v

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: conv(v) for k, v in dataclasses.asdict(obj).items()}
    raise TypeError(f"not a dataclass: {type(obj)}")
