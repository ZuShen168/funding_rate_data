"""Kraken Futures and Lighter -- STUBS.

These endpoint paths and response shapes have NOT been verified against current
docs. They are sketched from the documented mechanics, and the response parsing
is almost certainly wrong in detail. Read the docs, fix the parsing, then move
each class into its own module alongside the others.

  Kraken Futures : https://docs.kraken.com/api/docs/futures-api/trading/
                   (see historical funding rates)
  Lighter        : https://docs.lighter.xyz/trading/funding

Deliberately left unfinished rather than guessed at, because a plausible-looking
adapter that silently mis-parses is worse than no adapter at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from ..schema import FundingObservation
from ..normalize import canonical_symbol
from .base import VenueAdapter


class KrakenFuturesAdapter(VenueAdapter):
    venue = "kraken"
    base_url = "https://futures.kraken.com"

    TIMESTAMP_SEMANTICS = (
        "TODO VERIFY. Kraken accrues funding CONTINUOUSLY as unrealised PnL and "
        "realises it every hour, which is a different model from the discrete-"
        "settlement venues. Decide explicitly whether you treat the hourly "
        "realisation as the settlement moment (recommended, for comparability) "
        "and write the reasoning here.\n\n"
        "Also note: Kraken's interval is region-dependent -- 8h for US clients, "
        "hourly elsewhere. Confirm which applies to your account before "
        "assuming the cadence."
    )
    VERIFIED = "STUB -- not implemented."

    def list_instruments(self) -> List[str]:
        raise NotImplementedError("Read the docs, then implement.")

    def fetch_funding_history(self, venue_symbol, start, end):
        # Sketch only. Endpoint path and field names need verifying.
        #   GET /derivatives/api/v4/historicalfundingrates?symbol=PF_XBTUSD
        # Watch for: `relativeFundingRate` vs `fundingRate` -- Kraken exposes an
        # ABSOLUTE rate (payout per contract unit) and a RELATIVE rate (as a
        # fraction of price). You want the relative one for cross-venue APR.
        # Inverse contracts (PI_*) pay in the base currency -- tag those
        # contract_type='inverse', do not mix them with linear (PF_*).
        raise NotImplementedError("Read the docs, then implement.")


class LighterAdapter(VenueAdapter):
    venue = "lighter"
    base_url = "https://mainnet.zklighter.elliot.ai"  # TODO VERIFY

    TIMESTAMP_SEMANTICS = (
        "TODO VERIFY. Lighter settles hourly, computing a 1-hour premium then "
        "dividing by 8 so payments spread over 8 hours -- the same shape as "
        "Hyperliquid. So the reported rate is very likely ALREADY hourly and "
        "must NOT be divided again. Confirm this against the docs, then "
        "sanity-check: a flat market should show roughly the fixed per-market "
        "interest component, not eight times it."
    )
    VERIFIED = "STUB -- not implemented."

    def list_instruments(self) -> List[str]:
        raise NotImplementedError("Read the docs, then implement.")

    def fetch_funding_history(self, venue_symbol, start, end):
        raise NotImplementedError("Read the docs, then implement.")
