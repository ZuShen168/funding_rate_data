"""Backfill CLI.

    python -m src.backfill --venues binance bybit hyperliquid \
        --symbols BTC ETH SOL --days 365

Symbols are given as BASE names; each adapter resolves them to its own
convention (BTC -> BTCUSDT on Binance, BTC on Hyperliquid).
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Type

from .adapters.base import VenueAdapter
from .adapters.binance import BinanceAdapter
from .adapters.bybit import BybitAdapter
from .adapters.hyperliquid import HyperliquidAdapter
from .normalize import derive_intervals, coverage_report
from .schema import FundingObservation
from . import store

log = logging.getLogger("backfill")

ADAPTERS: Dict[str, Type[VenueAdapter]] = {
    "binance": BinanceAdapter,
    "bybit": BybitAdapter,
    "hyperliquid": HyperliquidAdapter,
    # "kraken": KrakenFuturesAdapter,   # see adapters/stubs.py
    # "lighter": LighterAdapter,        # see adapters/stubs.py
}


VENUE_QUOTES = {
    "binance": {"USDT", "USDC"},
    "bybit": {"USDT", "USDC"},
    "hyperliquid": {"USDC"},      # USDC-margined only
}


def resolve_symbol(venue: str, base: str, quote: str) -> str:
    b, q = base.upper(), quote.upper()
    if venue == "binance":
        return f"{b}{q}"
    if venue == "bybit":
        return f"{b}PERP" if q == "USDC" else f"{b}{q}"
    if venue == "hyperliquid":
        return b
    raise KeyError(venue)


def run(venues: List[str], bases: List[str], days: int, quotes: List[str]) -> List[FundingObservation]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    all_obs: List[FundingObservation] = []
    for venue in venues:
        adapter = ADAPTERS[venue]()
        log.info("=== %s ===", venue)
        log.info("timestamp semantics: %s", adapter.TIMESTAMP_SEMANTICS.split("\n")[0])
        if adapter.VERIFIED.startswith(("UNVERIFIED", "STUB")):
            log.warning("%s adapter is %s", venue, adapter.VERIFIED)

        for base in bases:
            for quote in quotes:
                if quote.upper() not in VENUE_QUOTES[venue]:
                    continue
                sym = resolve_symbol(venue, base, quote)
                try:
                    rows = adapter.fetch_funding_history(sym, start, end)
                except Exception as e:
                    log.error("  %-14s FAILED: %s", sym, e)
                    continue
                log.info("  %-14s %5d rows", sym, len(rows))
                all_obs.extend(rows)

    log.info("deriving intervals from observed settlement gaps...")
    all_obs = derive_intervals(all_obs)

    n = store.write(all_obs)
    log.info("wrote %d rows", n)

    log.info("--- coverage ---")
    for s in sorted(
        coverage_report(all_obs).values(),
        key=lambda x: (x["venue"], x["canonical_symbol"]),
    ):
        flag = "  <-- CHECK" if s["rows_missing_interval"] else ""
        log.info(
            "  %-12s %-16s %6d rows  %6.1f days  %4d missing interval%s",
            s["venue"], s["canonical_symbol"], s["rows"],
            s["history_days"], s["rows_missing_interval"], flag,
        )
    return all_obs


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--venues", nargs="+", default=list(ADAPTERS))
    p.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL"])
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--quotes", nargs="+", default=["USDT", "USDC"])
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(a.venues, a.symbols, a.days, a.quotes)


if __name__ == "__main__":
    main()
