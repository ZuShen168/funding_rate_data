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
        "divide by 8 again. Because interval_hours will be derived as 1.0 from "
        "the observed gaps, to_apr() multiplies by 8760 and lands on the correct "
        "APR. A 0.00125%/hr rate (the fixed interest component) annualises to "
        "~11.0% APR paid to shorts, which is the number to sanity-check against.\n\n"
        "Quote currency is USDC, not USDT -- this matters (see TRAP 4). The venue "
        "returns a bare coin name ('BTC') so the quote is supplied here."
    )
    VERIFIED = "UNVERIFIED -- sanity-check that a flat market shows ~0.00125%/hr."

    QUOTE = "USDC"

    def __init__(self, *a, **kw):
        # ~1200 weight/min and fundingHistory is expensive. 1.2s between calls
        # keeps us under it without relying on backoff.
        kw.setdefault("rate_limit_s", 1.2)
        super().__init__(*a, **kw)

    def list_instruments(self) -> List[str]:
        meta = self._post("/info", {"type": "meta"})
        return [
            a["name"] for a in meta["universe"] if not a.get("isDelisted", False)
        ]

    def fetch_funding_history(
        self, venue_symbol: str, start: datetime, end: datetime
    ) -> List[FundingObservation]:
        canon = canonical_symbol(venue_symbol, self.QUOTE)

        out: List[FundingObservation] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        while cursor < end_ms:
            # The endpoint caps the response (~500 rows), so walk forward.
            window_end = min(cursor + int(timedelta(days=20).total_seconds() * 1000), end_ms)
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
                        venue=self.venue,
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
