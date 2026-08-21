from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from ..schema import FundingObservation
from ..normalize import canonical_symbol
from .base import VenueAdapter, ms_to_utc


class HyperliquidAdapter(VenueAdapter):
    venue = "hyperliquid"
    base_url = "https://api.hyperliquid.xyz"

    TIMESTAMP_SEMANTICS = (
        "Hyperliquid `time` is the hourly settlement moment. Maps directly onto "
        "settlement_ts.\n\n"
        "CRITICAL: `fundingRate` is ALREADY the hourly rate -- Hyperliquid "
        "computes an 8h-equivalent rate and settles 1/8 of it every hour. Do NOT "
        "divide by 8 again. Because interval_hours is derived as 1.0 from the "
        "observed gaps, to_apr() multiplies by 8760 and lands on the correct "
        "APR. On native markets a flat market shows 0.0000125/hr (the fixed "
        "interest component), which annualises to ~11% APR paid to shorts.\n\n"
        "HIP-3 markets ('dex:SYMBOL') are builder-deployed: each dex has its own "
        "order book, margining and FUNDING PARAMETERS, so the 11% floor does NOT "
        "carry across. xyz:TSLA was observed at half the native interest rate. "
        "The hourly cadence is the same, so normalisation is unaffected.\n\n"
        "Quote currency is USDC, not USDT -- this matters (see TRAP 4). Native "
        "markets return a bare coin name ('BTC') so the quote is supplied here."
    )
    VERIFIED = (
        "Verified 2026-08-21: 365d of BTC/ETH/SOL pulled clean, zero missing "
        "intervals, flat-market rate matches the documented 0.0000125/hr. "
        "HIP-3 fundingHistory confirmed working with prefixed coins."
    )

    QUOTE = "USDC"

    #: HIP-3 dexes with LIVE funding. Verified 2026-08-21 over 500 consecutive
    #: hourly observations per market:
    #:   xyz   -> 100% nonzero (TSLA mean 8.0% APR, NVDA 6.8%, AAPL 0.3%)
    #:   flx, km, cash, mkts, vntl -> exactly 0.0 on every single row
    #: The zero dexes are not quiet, they have funding switched off. Adding them
    #: would produce a large, clean, entirely useless dataset. Re-check
    #: periodically -- a deployer can turn funding on at any time.
    HIP3_DEXES = ["xyz"]

    def __init__(self, *a, **kw):
        # ~1200 weight/min per IP, and fundingHistory is an expensive call.
        # 1.2s between requests keeps us under the limit without leaning on
        # the backoff in the base class.
        kw.setdefault("rate_limit_s", 1.2)
        super().__init__(*a, **kw)

    # -- discovery ----------------------------------------------------------

    def list_instruments(self, dex: str = "") -> List[str]:
        """Markets on the native perp DEX, or on a named HIP-3 dex.

        HIP-3 names come back already prefixed ('xyz:TSLA') in the universe,
        so no extra munging is needed here.
        """
        payload = {"type": "meta", "dex": dex} if dex else {"type": "meta"}
        meta = self._post("/info", payload)
        return [a["name"] for a in meta["universe"] if not a.get("isDelisted", False)]

    def list_perp_dexes(self) -> List[str]:
        """Every active builder-deployed dex. Use to re-check HIP3_DEXES."""
        return [d["name"] for d in self._post("/info", {"type": "perpDexs"})
                if isinstance(d, dict) and d.get("name")]

    def list_all(self) -> List[str]:
        """Native markets plus every HIP-3 market we've decided to track."""
        out = list(self.list_instruments())
        for d in self.HIP3_DEXES:
            out += self.list_instruments(d)
        return out

    # -- history ------------------------------------------------------------

    def fetch_funding_history(
        self, venue_symbol: str, start: datetime, end: datetime
    ) -> List[FundingObservation]:
        # HIP-3 markets are 'dex:SYMBOL'. Each dex has independent order books,
        # margining and funding config, so it is a separate VENUE for our
        # purposes - not a variant of native Hyperliquid. Folding them together
        # would hide the very spread we're looking for.
        if ":" in venue_symbol:
            dex, base = venue_symbol.split(":", 1)
            canon = canonical_symbol(base, self.QUOTE)
            venue_name = f"hyperliquid-{dex}"
        else:
            canon = canonical_symbol(venue_symbol, self.QUOTE)
            venue_name = self.venue

        out: List[FundingObservation] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        while cursor < end_ms:
            # The endpoint caps responses at ~500 rows, so walk forward in
            # windows comfortably under that at hourly resolution.
            window_end = min(
                cursor + int(timedelta(days=20).total_seconds() * 1000), end_ms
            )
            rows = self._post(
                "/info",
                {
                    "type": "fundingHistory",
                    "coin": venue_symbol,
                    "startTime": cursor,
                    "endTime": window_end,
                },
            )
            if not rows:
                cursor = window_end + 1
                continue

            for r in rows:
                out.append(
                    FundingObservation(
                        venue=venue_name,
                        venue_symbol=venue_symbol,
                        canonical_symbol=canon,
                        settlement_ts=ms_to_utc(r["time"]),
                        rate_raw=float(r["fundingRate"]),  # already hourly
                        interval_hours=None,
                        rate_apr=None,
                        is_predicted=False,
                        contract_type="linear",
                    )
                )

            last = max(int(r["time"]) for r in rows)
            cursor = max(last + 1, cursor + 1)

        return out
