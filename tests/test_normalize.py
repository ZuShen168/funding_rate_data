"""Tests for the traps. Each test corresponds to a documented failure mode.

    python -m pytest tests/ -v          (or: python tests/test_normalize.py)
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.normalize import (
    canonical_symbol,
    split_concatenated,
    derive_intervals,
    to_apr,
    coverage_report,
)
from src.schema import FundingObservation

UTC = timezone.utc


def obs(venue, sym, ts, rate):
    return FundingObservation(
        venue=venue,
        venue_symbol=sym,
        canonical_symbol=canonical_symbol(*split_concatenated(sym)),
        settlement_ts=ts,
        rate_raw=rate,
        interval_hours=None,
        rate_apr=None,
    )


# --- TRAP 4: quote currency is not cosmetic --------------------------------

def test_quote_currencies_do_not_collapse():
    assert canonical_symbol("BTC", "USDT") != canonical_symbol("BTC", "USDC")


def test_longest_quote_wins():
    assert split_concatenated("BTCUSDT") == ("BTC", "USDT")
    assert split_concatenated("BTCUSDC") == ("BTC", "USDC")
    assert split_concatenated("ETHUSD") == ("ETH", "USD")


def test_bare_symbol_gets_default_quote():
    # Hyperliquid returns 'BTC' with no quote; caller supplies USDC.
    assert split_concatenated("BTC") == ("BTC", "USDT")
    assert canonical_symbol("BTC", "USDC") == "BTC-USDC-PERP"


def test_kraken_xbt_alias():
    assert canonical_symbol("XBT", "USD") == "BTC-USD-PERP"


# --- TRAP 6: interval derived from data, not config ------------------------

def test_interval_derived_from_observed_gaps():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [obs("binance", "BTCUSDT", t0 + timedelta(hours=8 * i), 0.0001)
            for i in range(5)]
    out = derive_intervals(rows)
    assert all(o.interval_hours == 8.0 for o in out)


def test_interval_change_mid_series_is_detected():
    # Binance shortens a volatile pair from 8h to 4h. A static config would
    # annualise the 4h rows 2x too high and invent an arb.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    stamps = [t0 + timedelta(hours=8 * i) for i in range(3)]
    t = stamps[-1]
    stamps += [t + timedelta(hours=4 * i) for i in range(1, 4)]
    out = sorted(
        derive_intervals([obs("binance", "BTCUSDT", s, 0.0001) for s in stamps]),
        key=lambda o: o.settlement_ts,
    )
    assert [o.interval_hours for o in out] == [8.0, 8.0, 8.0, 4.0, 4.0, 4.0]


def test_hyperliquid_hourly_series():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [obs("hyperliquid", "BTC", t0 + timedelta(hours=i), 0.0000125)
            for i in range(24)]
    out = derive_intervals(rows)
    assert all(o.interval_hours == 1.0 for o in out)
    # The fixed interest component should annualise to ~11%.
    assert 0.10 < out[0].rate_apr < 0.12


def test_venues_are_not_mixed_when_deriving():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = (
        [obs("binance", "BTCUSDT", t0 + timedelta(hours=8 * i), 0.0001) for i in range(4)]
        + [obs("hyperliquid", "BTC", t0 + timedelta(hours=i), 0.0000125) for i in range(24)]
    )
    out = derive_intervals(rows)
    by_venue = {}
    for o in out:
        by_venue.setdefault(o.venue, set()).add(o.interval_hours)
    assert by_venue["binance"] == {8.0}
    assert by_venue["hyperliquid"] == {1.0}


# --- TRAP 5: gaps stay NULL, never filled ----------------------------------

def test_unexplained_gap_yields_null_not_a_guess():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    stamps = [t0, t0 + timedelta(hours=8), t0 + timedelta(hours=93)]  # 85h hole
    out = sorted(
        derive_intervals([obs("binance", "BTCUSDT", s, 0.0001) for s in stamps]),
        key=lambda o: o.settlement_ts,
    )
    assert out[-1].interval_hours is None
    assert out[-1].rate_apr is None
    assert out[-1].rate_raw == 0.0001  # raw always preserved


# --- APR: the only cross-venue comparable number ---------------------------

def test_hourly_and_8h_rates_annualise_to_the_same_apr():
    eight_h = to_apr(0.0001, 8.0)
    hourly = to_apr(0.0001 / 8, 1.0)
    assert eight_h == pytest.approx(hourly)
    assert eight_h == pytest.approx(0.1095, abs=1e-4)  # ~10.95% APR


def test_negative_rates_survive():
    assert to_apr(-0.0005, 8.0) < 0


# --- TRAP 3: percent-vs-decimal guard --------------------------------------

def test_percent_scale_mistake_is_rejected_loudly():
    with pytest.raises(ValueError, match="percent"):
        obs("binance", "BTCUSDT", datetime(2026, 1, 1, tzinfo=UTC), 1.0)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="tz-aware"):
        obs("binance", "BTCUSDT", datetime(2026, 1, 1), 0.0001)


# --- TRAP 7: history length is tracked -------------------------------------

def test_coverage_reports_history_span():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = derive_intervals(
        [obs("binance", "BTCUSDT", t0 + timedelta(hours=8 * i), 0.0001) for i in range(90)]
    )
    stats = list(coverage_report(rows).values())[0]
    assert stats["rows"] == 90
    assert stats["history_days"] == pytest.approx(29.7, abs=0.2)
    assert stats["rows_missing_interval"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
