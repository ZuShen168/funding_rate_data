# funding-rate-data

Cross-venue perpetual funding rate data, normalised so venues on different
settlement clocks can actually be compared — plus a dashboard that asks the
question the funding screeners don't:

> Not "is there a spread right now?" but **"does the spread persist long enough
> to be worth trading after costs?"**

Plenty of sites show you current funding rates across exchanges. None of them
tell you how long a given spread historically *lasted*, or whether it clears
your own fee tier. That gap is what this is for.

## The finding

Running this over a year of BTC, ETH and SOL data across Binance, Bybit and
Hyperliquid:

- Funding spreads above 5% APR held the same direction for a **median of 1 day**
- The longest single stretch was **7 days**
- At ~22bps round-trip cost, you need roughly **16 days** to break even

So on liquid majors across these venues, cross-exchange funding arbitrage
doesn't clear its own costs. The spreads are real but they're noise, not
structure. A screener showing "40% APR available" is annualising a payment that
settles in the next hour — and the rate is usually back at baseline before you
could hold it long enough to profit.

That's a negative result, and it's the point. It took a couple of weekends
instead of six months and a live bot.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -v
```

## Run

```bash
# Fetch history
python3 -m src.backfill --venues binance bybit hyperliquid \
                        --symbols BTC ETH SOL --days 365

# Persistence analysis
duckdb -c ".read queries/persistence.sql"

# Dashboard
streamlit run app.py
```

---

## The seven traps

The API calls are the easy part. These are the ways a funding dataset goes
quietly wrong and hands you a confident wrong answer. Each one has a test.

**1. Timestamp semantics.** Is the venue's timestamp the *start* of the accrual
window, the *end*, or the publication moment? Off by one period and you have
lookahead bias — the backtest "knows" a rate before it was knowable, and results
look *better* than reality. `settlement_ts` is defined here as the moment cash
actually moved, and every adapter must document how it mapped the venue's field
onto that. The base class refuses to instantiate without it.

**2. Never mix realised and predicted.** History endpoints give realised rates;
live endpoints give forward-looking estimates. Separate column, separate
semantics.

**3. Store raw, derive APR, never overwrite.** Venues report in decimals,
percent, per-8h and per-hour — and some (Hyperliquid, Lighter) have *already*
divided by 8. `rate_raw` stays untouched so you can re-derive when you find a
bug. There's a guard that rejects any rate above 0.5 as a probable
percent-vs-decimal mixup.

**4. Quote currency is not cosmetic.** BTCUSDT, BTCUSDC and BTCUSD are different
instruments with different funding. Part of the gap between them is stablecoin
basis, not perp premium. Collapse them into "BTC" and you'll manufacture spreads
that are really just USDT/USDC risk.

**5. Gaps stay NULL.** Venue downtime, delistings, a scraper dying overnight.
Forward-fill any of it and your persistence statistic becomes a measure of how
often your scraper broke.

**6. `interval_hours` comes from the data.** Binance shortens funding intervals
on volatile pairs and changes them back. A static `binance = 8h` config
annualises those rows wrong by 2x or 8x — enough to invent an arb that doesn't
exist. Interval is measured from observed gaps and snapped to the nearest
plausible value.

**7. Track history length.** A pair with 30 days of history isn't comparable to
one with 365, and new listings dominate any ranking if you let them.

---

## Layout

```
src/schema.py            Canonical row. One definition of truth.
src/normalize.py         The traps. Symbol canonicalisation, interval
                         derivation, APR conversion, coverage reporting.
src/store.py             Parquet, one file per venue.
src/backfill.py          CLI: fetch -> normalise -> write -> coverage report.
src/adapters/base.py     Adapter interface. Enforces TIMESTAMP_SEMANTICS.
src/adapters/*.py        Binance, Bybit, Hyperliquid. Kraken + Lighter stubbed.
queries/persistence.sql  Gaps-and-islands run-length analysis.
config/venue_spec.yaml   Fee tiers with effective dates.
app.py                   Streamlit dashboard.
```

Why parquet and DuckDB rather than a database: roughly 200 pairs x 6 venues x
3 observations/day x 365 days is about 1.3M rows/year — a few hundred MB, and it
fits in memory. No Postgres, no Timescale, no Airflow.

## Status

Binance, Bybit and Hyperliquid adapters are verified against the live APIs:
a full year of BTC/ETH/SOL pulls clean with zero missing intervals.

Kraken and Lighter are deliberately unfinished stubs. Both have doc links and
notes on what to check. A plausible-looking adapter that silently mis-parses is
worse than no adapter — Kraken's continuous-accrual model and region-dependent
interval need a decision, and Lighter's rate is probably already hourly like
Hyperliquid's, but that needs confirming.

Fee values in `config/venue_spec.yaml` are placeholders. Replace them with your
own tier — the whole point of running this yourself is that it knows your costs.

## Before trusting the numbers

Reconcile against a real funding statement. Pull the backfill for a window where
you actually held a position and check the rates and especially the *timestamps*
line up. Then compare an aggregator against the venue's own API — where they
disagree, one is wrong about a convention, and finding out which is worth more
than everything else here.

## Reading the output

Sort by how long the profitable stretches last, not by how big the spread is.
A 300bp spread lasting 2 hours is a data artifact. A 40bp spread lasting 9 days
is a trade.

## Licence

MIT.
