"""Normalisation -- where the actual work is.

This module implements the traps documented in the README. If you change
anything here, run the tests; every function below corresponds to a specific
way the dataset can be silently corrupted.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from datetime import timedelta
from typing import Iterable, List, Sequence

from .schema import FundingObservation, HOURS_PER_YEAR

# ---------------------------------------------------------------------------
# TRAP 4: quote currency is NOT cosmetic.
#
# BTCUSDT / BTCUSDC / BTCUSD are different instruments with different funding.
# The gap between them is partly stablecoin basis, not perp premium. Collapsing
# them into "BTC" manufactures spreads that are really just USDT/USDC risk.
# ---------------------------------------------------------------------------

# Base-asset aliases. Kraken uses XBT; some venues prefix with 1000 for
# low-price tokens (1000PEPE) -- those are genuinely different contract sizes
# but the SAME underlying, so they map to the same base with a multiplier note.
_BASE_ALIASES = {
    "XBT": "BTC",
    "XDG": "DOGE",
}

_KNOWN_QUOTES = ["USDT", "USDC", "USDE", "USD", "BTC", "ETH"]


def canonical_base(raw: str) -> str:
    b = raw.upper().strip()
    return _BASE_ALIASES.get(b, b)


def canonical_symbol(base: str, quote: str, kind: str = "PERP") -> str:
    """'BTC', 'USDT' -> 'BTC-USDT-PERP'."""
    return f"{canonical_base(base)}-{quote.upper().strip()}-{kind}"


def split_concatenated(venue_symbol: str, default_quote: str = "USDT") -> tuple[str, str]:
    """Split 'BTCUSDT' -> ('BTC', 'USDT').

    Greedy longest-quote match, because 'USDT' must win over 'USD' when both
    would match. Falls back to default_quote for venues that omit it entirely
    (Hyperliquid returns bare 'BTC').
    """
    s = venue_symbol.upper().strip()
    for q in sorted(_KNOWN_QUOTES, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, default_quote


# ---------------------------------------------------------------------------
# TRAP 6: interval_hours comes from the DATA, not from static config.
#
# Binance shortens funding intervals on volatile pairs (8h -> 4h -> 1h) and
# changes them back. A hardcoded "binance = 8h" annualises those rows wrong by
# a factor of 2 or 8, which is more than enough to invent a fake arb.
# ---------------------------------------------------------------------------

# Real-world funding intervals. Observed gaps get snapped to the nearest of
# these, so that a few seconds of settlement jitter doesn't produce 7.998h.
_PLAUSIBLE_INTERVALS = [1.0, 2.0, 4.0, 8.0, 12.0, 24.0]
_SNAP_TOLERANCE = 0.20  # 20% -- generous, because venues do drift


def _snap(hours: float) -> float | None:
    for candidate in _PLAUSIBLE_INTERVALS:
        if abs(hours - candidate) / candidate <= _SNAP_TOLERANCE:
            return candidate
    return None


def derive_intervals(
    observations: Sequence[FundingObservation],
) -> List[FundingObservation]:
    """Fill interval_hours and rate_apr by measuring gaps between settlements.

    Grouped per (venue, venue_symbol) and sorted by time. Each observation's
    interval is the gap to the PREVIOUS settlement -- because that is the window
    the rate actually accrued over. The first observation in each series inherits
    the interval of the second, since it has no predecessor.

    Rows whose gap does not snap to a plausible interval get interval_hours=None
    and rate_apr=None. That is deliberate: an unexplained gap usually means
    venue downtime or a scraper failure, and TRAP 5 says gaps stay NULL rather
    than being guessed at. Filter them out in analysis; do not fill them.
    """
    grouped: dict[tuple[str, str], List[FundingObservation]] = defaultdict(list)
    for o in observations:
        grouped[(o.venue, o.venue_symbol)].append(o)

    out: List[FundingObservation] = []
    for series in grouped.values():
        series.sort(key=lambda o: o.settlement_ts)

        gaps: List[float | None] = [None] * len(series)
        for i in range(1, len(series)):
            delta_h = (
                series[i].settlement_ts - series[i - 1].settlement_ts
            ).total_seconds() / 3600.0
            gaps[i] = _snap(delta_h)

        # First row has no predecessor -- borrow from its successor.
        if len(series) > 1:
            gaps[0] = gaps[1]

        for obs, interval in zip(series, gaps):
            if interval is None:
                out.append(
                    dataclasses.replace(obs, interval_hours=None, rate_apr=None)
                )
            else:
                out.append(
                    dataclasses.replace(
                        obs,
                        interval_hours=interval,
                        rate_apr=to_apr(obs.rate_raw, interval),
                    )
                )
    return out


def to_apr(rate_raw: float, interval_hours: float) -> float:
    """The ONLY cross-venue comparable number.

    A 0.01% 8h rate and a 0.00125% 1h rate are the same thing. This is what
    makes Binance's three-a-day cadence comparable to Hyperliquid's hourly one.

    Simple (not compounded) annualisation on purpose: funding is a cash flow you
    receive and redeploy, not something that auto-compounds in the position.
    Compounding would flatter high-rate venues.
    """
    return rate_raw * (HOURS_PER_YEAR / interval_hours)


# ---------------------------------------------------------------------------
# TRAP 5 + 7: explicit gap and coverage reporting.
# ---------------------------------------------------------------------------


def coverage_report(observations: Iterable[FundingObservation]) -> dict:
    """Per-series first_seen / last_seen / row count / missing-interval count.

    Run this after every backfill and eyeball it. A series with 340 days of
    history and 400 missing intervals is a broken adapter, not a market.
    TRAP 7: a pair with 30 days of history is not comparable to one with 365 --
    filter on min_history_days downstream.
    """
    stats: dict[tuple[str, str], dict] = {}
    for o in observations:
        key = (o.venue, o.canonical_symbol)
        s = stats.setdefault(
            key,
            {
                "venue": o.venue,
                "canonical_symbol": o.canonical_symbol,
                "first_seen": o.settlement_ts,
                "last_seen": o.settlement_ts,
                "rows": 0,
                "rows_missing_interval": 0,
            },
        )
        s["rows"] += 1
        s["first_seen"] = min(s["first_seen"], o.settlement_ts)
        s["last_seen"] = max(s["last_seen"], o.settlement_ts)
        if o.interval_hours is None:
            s["rows_missing_interval"] += 1

    for s in stats.values():
        span = s["last_seen"] - s["first_seen"]
        s["history_days"] = round(span / timedelta(days=1), 1)
    return stats
