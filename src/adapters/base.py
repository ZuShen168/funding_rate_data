"""Adapter interface.

Every venue adapter MUST document, in TIMESTAMP_SEMANTICS, exactly how it maps
the venue's timestamp field onto `settlement_ts` (the cash-moved moment).

This is not decoration. It is the single most common source of silent corruption
in funding datasets, and the note is what lets you audit the mapping later
against a real account statement.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List

import requests

from ..schema import FundingObservation


def ms_to_utc(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


class VenueAdapter(ABC):
    venue: str
    base_url: str

    #: REQUIRED. Plain-English description of how the venue's timestamp field
    #: maps onto settlement_ts, and whether it needed shifting.
    TIMESTAMP_SEMANTICS: str = ""

    #: REQUIRED. How this adapter was verified. Ideally: "reconciled against
    #: account funding statement for BTCUSDT, 2026-03-01..2026-03-07".
    VERIFIED: str = "UNVERIFIED"

    def __init__(self, session: requests.Session | None = None, rate_limit_s: float = 0.25):
        self.session = session or requests.Session()
        self.rate_limit_s = rate_limit_s
        self._last_call = 0.0
        if not self.TIMESTAMP_SEMANTICS:
            raise NotImplementedError(
                f"{type(self).__name__} must document TIMESTAMP_SEMANTICS"
            )

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)
        r = self.session.get(self.base_url + path, params=params, timeout=20)
        self._last_call = time.monotonic()
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict | list:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)
        r = self.session.post(self.base_url + path, json=payload, timeout=20)
        self._last_call = time.monotonic()
        r.raise_for_status()
        return r.json()

    @abstractmethod
    def list_instruments(self) -> List[str]:
        """Venue symbols with a perpetual funding rate."""

    @abstractmethod
    def fetch_funding_history(
        self, venue_symbol: str, start: datetime, end: datetime
    ) -> List[FundingObservation]:
        """Realised funding, paginated internally. interval_hours/rate_apr left
        as None -- derive_intervals() fills them once the full series is known."""
