from __future__ import annotations

from datetime import datetime
from typing import List

from ..schema import FundingObservation
from ..normalize import canonical_symbol, split_concatenated
from .base import VenueAdapter, ms_to_utc


class BinanceAdapter(VenueAdapter):
    venue = "binance"
    base_url = "https://fapi.binance.com"

    TIMESTAMP_SEMANTICS = (
        "Binance `fundingTime` is the SETTLEMENT moment -- the instant the "
        "payment was applied, at the END of the accrual window. This maps "
        "directly onto settlement_ts with no shift. Standard cadence is "
        "00/08/16 UTC, but Binance shortens the interval on volatile pairs, "
        "which is why interval is derived from observed gaps rather than assumed."
    )
    VERIFIED = "UNVERIFIED -- reconcile against your own funding statement before trusting."

    def list_instruments(self) -> List[str]:
        info = self._get("/fapi/v1/exchangeInfo")
        return [
            s["symbol"]
            for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
        ]

    def fetch_funding_history(
        self, venue_symbol: str, start: datetime, end: datetime
    ) -> List[FundingObservation]:
        base, quote = split_concatenated(venue_symbol)
        canon = canonical_symbol(base, quote)

        out: List[FundingObservation] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        while cursor < end_ms:
            rows = self._get(
                "/fapi/v1/fundingRate",
                {
                    "symbol": venue_symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not rows:
                break

            for r in rows:
                out.append(
                    FundingObservation(
                        venue=self.venue,
                        venue_symbol=venue_symbol,
                        canonical_symbol=canon,
                        settlement_ts=ms_to_utc(r["fundingTime"]),
                        rate_raw=float(r["fundingRate"]),
                        interval_hours=None,
                        rate_apr=None,
                        is_predicted=False,
                        contract_type="linear",
                        mark_price=float(r["markPrice"]) if r.get("markPrice") else None,
                    )
                )

            last = int(rows[-1]["fundingTime"])
            if last <= cursor or len(rows) < 1000:
                break
            cursor = last + 1

        return out
