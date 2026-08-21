"""Canonical schema for a single funding observation.

ONE definition of truth. Every adapter must emit rows in this shape.

Key semantic decisions locked in here (see README "Timestamp semantics"):

  settlement_ts  -- the UTC moment cash ACTUALLY MOVED between longs and shorts.
                    NOT the start of the accrual window. NOT the moment the rate
                    was published. If a venue gives you the window start, add the
                    interval. Getting this wrong by one period puts lookahead
                    bias into every backtest downstream.

  rate_raw       -- exactly what the venue returned, unmodified. Never overwrite.
                    You WILL find a bug in your normalisation and you will need
                    to re-derive from raw.

  interval_hours -- the accrual period this rate covers, in hours. Derived from
                    observed gaps between settlements, not from static config,
                    because venues change intervals on volatile pairs.

  rate_apr       -- the only field you compare across venues.
                    rate_raw * (8760 / interval_hours)
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

HOURS_PER_YEAR = 24 * 365  # 8760. Ignores leap years on purpose: consistency
                           # across venues matters more than calendar precision.


@dataclass(frozen=True)
class FundingObservation:
    venue: str                      # 'binance' | 'bybit' | 'hyperliquid' | ...
    venue_symbol: str               # raw, as the venue names it ('BTCUSDT', 'PF_XBTUSD')
    canonical_symbol: str           # 'BTC-USDT-PERP'  -- see normalize.canonical_symbol
    settlement_ts: datetime         # UTC, tz-aware. Cash-moved moment.
    rate_raw: float                 # as reported, decimal (0.0001 = 1bp), never %
    interval_hours: Optional[float] # None until derive_intervals() has run
    rate_apr: Optional[float]       # None until derive_intervals() has run
    is_predicted: bool = False      # realised history vs forward-looking estimate
    contract_type: str = "linear"   # 'linear' | 'inverse'  -- inverse pays in base ccy
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    open_interest_usd: Optional[float] = None
    ingested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if self.settlement_ts.tzinfo is None:
            raise ValueError(
                f"settlement_ts must be tz-aware UTC, got naive: {self.settlement_ts}"
            )
        if abs(self.rate_raw) > 0.5:
            # Sanity guard for the classic percent-vs-decimal mixup. A real
            # funding rate above 50% per interval is essentially impossible
            # outside of Hyperliquid's 4%/hr cap territory; if you trip this,
            # your adapter is almost certainly dividing by 100 too few times.
            raise ValueError(
                f"rate_raw={self.rate_raw} for {self.venue}:{self.venue_symbol} "
                f"looks like a percent, not a decimal. Check the adapter."
            )

    def as_dict(self) -> dict:
        return asdict(self)


# Column order for the parquet files. Keep stable -- downstream SQL depends on it.
COLUMNS = [
    "venue",
    "venue_symbol",
    "canonical_symbol",
    "settlement_ts",
    "rate_raw",
    "interval_hours",
    "rate_apr",
    "is_predicted",
    "contract_type",
    "mark_price",
    "index_price",
    "open_interest_usd",
    "ingested_at",
]
