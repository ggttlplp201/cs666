"""Canonical record types shared by Systems A & B.

Item follows the normalization schema in docs/Shared §2.3; Signal follows the
tiered bus schema in docs/System-A §7.5. All prices are CNY unless a field
name says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Item:
    market_hash_name: str
    buff_lowest_sell_cny: float
    buff_highest_buy_cny: float
    buff_listing_count: int      # sell-side depth proxy
    buff_buy_order_count: int    # bid-side depth proxy (valid buy orders)
    buff_volume_24h: int | None  # executed trades, NOT listings; None when the
                                 # feed tier can't provide it (cs2.sh Developer)
    ts: float                    # snapshot time, unix UTC
    variant: str | None = None   # wear / Doppler phase / fade %
    float_range: tuple[float, float] | None = None
    cross_market: dict[str, float] = field(default_factory=dict)


class SignalType(str, Enum):
    UPDATE_LEAK = "update_leak"
    OFFICIAL_ANNOUNCEMENT = "official_announcement"
    CONFIRMED_UPDATE = "confirmed_update"
    HYPE = "hype"
    ATTENTION = "attention"
    MARKET_BREAK = "market_break"  # §4.1a break detector, market-data-based


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class Signal:
    tier: int                    # 1 = raw leak · 2 = confirmed · 3 = attention
    type: SignalType
    items: tuple[str, ...]       # affected market_hash_names / collections
    direction: Direction
    confidence: float            # 0..1, corroboration-weighted
    first_seen_ts: float
    sources: tuple[str, ...] = ()
    attention_score: float | None = None  # Tier 3 only
    sentiment: float | None = None        # Tier 3 only
    event_rule: str | None = None         # rules-table event_rules id, when the
                                          # classifier could attribute one, -1..1

    def key(self) -> str:
        """Dedup key: same type + item set counts as the same event."""
        return f"{self.type.value}|{','.join(sorted(self.items))}"


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    WEAK = "weak"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Order:
    client_order_id: str         # idempotency key
    side: OrderSide
    market_hash_name: str
    qty: int
    limit_price_cny: float


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    side: OrderSide
    market_hash_name: str
    qty: int                     # may be < requested (partial, depth-capped)
    price_cny: float             # per unit, before fees
    fee_cny: float               # total fee on this fill (seller-side on BUFF)
    ts: float
