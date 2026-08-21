from __future__ import annotations

from datetime import datetime
from typing import List

from ..schema import FundingObservation
from ..normalize import canonical_symbol, split_concatenated
from .base import VenueAdapter, ms_to_utc


class BybitAdapter(VenueAdapter):
    venue = "bybit"
    base_url = "https://api.bybit.com"

    TIMESTAMP_SEMANTICS = (
        "Bybit `fundingRateTimestamp` is the settlement moment (end of the "
        "accrual window). Maps directly onto settlement_ts. Note Bybit paginates "
        "BACKWARDS from endTime -- rows come newest-first, so we walk endTime "
        "down rather than startTime up."
    )
    VERIFIED = "UNVERIFIED -- reconcile against your own funding statement before trusting."

    def list_instruments(self) -> List[str]:
        info = self._get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})
        return [
            s["symbol"]
            for s in info["result"]["list"]
            if s.get("contractType") == "LinearPerpetual" and s.get("status") == "Trading"
        ]

    def fetch_funding_history(
        self, venue_symbol: str, start: datetime, end: datetime
    ) -> List[FundingObservation]:
        if venue_symbol.endswith("PERP"):
            base, quote = venue_symbol[:-4], "USDC"   # Bybit's USDC-settled naming
        else:
            base, quote = split_concatenated(venue_symbol)
        canon = canonical_symbol(base, quote)

        out: List[FundingObservation] = []
        start_ms = int(start.timestamp() * 1000)
        cursor_end = int(end.timestamp() * 1000)

        while cursor_end > start_ms:
            resp = self._get(
                "/v5/market/funding/history",
                {
                    "category": "linear",
                    "symbol": venue_symbol,
                    "startTime": start_ms,
                    "endTime": cursor_end,
                    "limit": 200,
                },
            )
            rows = resp.get("result", {}).get("list", [])
            if not rows:
                break

            for r in rows:
                out.append(
                    FundingObservation(
                        venue=self.venue,
                        venue_symbol=venue_symbol,
                        canonical_symbol=canon,
                        settlement_ts=ms_to_utc(r["fundingRateTimestamp"]),
                        rate_raw=float(r["fundingRate"]),
                        interval_hours=None,
                        rate_apr=None,
                        is_predicted=False,
                        contract_type="linear",
                    )
                )

            oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
            if oldest >= cursor_end or len(rows) < 200:
                break
            cursor_end = oldest - 1

        return out
