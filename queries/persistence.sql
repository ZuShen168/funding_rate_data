-- ===========================================================================
-- THE STEP 2 QUERY.
--
-- "For every (coin, venue-A, venue-B) combination: how often was the spread
--  worth taking after my fees, and how long did it last?"
--
-- Everything in step 1 exists to make this query answerable and trustworthy.
--
--   duckdb -c ".read queries/persistence.sql"
-- ===========================================================================

-- Tune these to YOUR fee tier and YOUR size. This is the whole point of
-- building it yourself instead of reading Coinglass.
SET VARIABLE round_trip_cost_bps  = 16.0;  -- 4 fee events @ ~4bp
SET VARIABLE slippage_bps         = 6.0;   -- both legs, at your notional
SET VARIABLE assumed_hold_days    = 3.0;   -- amortisation horizon
SET VARIABLE min_history_days     = 180;   -- TRAP 7
SET VARIABLE data_glob = 'data/funding/**/*.parquet';

CREATE OR REPLACE TEMP VIEW obs AS
SELECT *
FROM read_parquet(getvariable('data_glob'))
WHERE NOT is_predicted
  AND interval_hours IS NOT NULL   -- TRAP 5: gaps stay gaps, never filled
  AND rate_apr       IS NOT NULL;

-- --- TRAP 7: drop series without enough history to be comparable ------------
CREATE OR REPLACE TEMP VIEW eligible AS
SELECT o.*
FROM obs o
JOIN (
    SELECT venue, canonical_symbol
    FROM obs
    GROUP BY 1, 2
    HAVING date_diff('day', min(settlement_ts), max(settlement_ts))
           >= getvariable('min_history_days')
) k USING (venue, canonical_symbol);

-- --- Align venues onto a common hourly spine -------------------------------
-- Venues settle on different clocks (Binance 8h, Hyperliquid 1h). To compare
-- them we ask, for each hour: what rate was in force on each venue?
--
-- settlement_ts is the END of the accrual window, so the window an observation
-- covers is [settlement_ts - interval_hours, settlement_ts).
--
-- NOTE: this is NOT forward-filling across gaps. It attributes a rate only to
-- the hours it genuinely accrued over. A missing settlement leaves those hours
-- genuinely absent, which is what we want.
CREATE OR REPLACE TEMP VIEW hourly AS
SELECT
    h.hour,
    e.venue,
    e.canonical_symbol,
    e.rate_apr
FROM (
    SELECT unnest(generate_series(
        (SELECT date_trunc('hour', min(settlement_ts)) FROM eligible),
        (SELECT date_trunc('hour', max(settlement_ts)) FROM eligible),
        INTERVAL 1 HOUR
    )) AS hour
) h
JOIN eligible e
  ON h.hour >= e.settlement_ts - (e.interval_hours * INTERVAL 1 HOUR)
 AND h.hour <  e.settlement_ts;

-- --- Pairwise spreads, net of YOUR costs -----------------------------------
CREATE OR REPLACE TEMP VIEW spreads AS
SELECT
    a.canonical_symbol,
    a.venue AS venue_short,   -- receive: short the HIGH funding venue
    b.venue AS venue_long,    -- pay:     long the LOW funding venue
    a.hour,
    (a.rate_apr - b.rate_apr) * 10000 AS gross_spread_bps_apr,
    -- Amortise the round trip over the assumed hold, then annualise.
    (a.rate_apr - b.rate_apr) * 10000
      - (getvariable('round_trip_cost_bps') + getvariable('slippage_bps'))
        * (365.0 / getvariable('assumed_hold_days'))
      AS net_spread_bps_apr
FROM hourly a
JOIN hourly b
  ON a.canonical_symbol = b.canonical_symbol
 AND a.hour = b.hour
 AND a.venue < b.venue;      -- each unordered pair once; sign carries direction

-- --- Gaps-and-islands: how long does a profitable window LAST? -------------
-- This is the number that decides whether the strategy is real. A spread that
-- clears the threshold 30% of hours but never for more than 4 hours at a time
-- is untradeable at your fee tier, however good the average looks.
CREATE OR REPLACE TEMP VIEW runs AS
WITH flagged AS (
    SELECT *,
           net_spread_bps_apr > 0 AS profitable,
           row_number() OVER w
             - row_number() OVER (PARTITION BY canonical_symbol, venue_short,
                                               venue_long, net_spread_bps_apr > 0
                                  ORDER BY hour) AS grp
    FROM spreads
    WINDOW w AS (PARTITION BY canonical_symbol, venue_short, venue_long ORDER BY hour)
)
SELECT canonical_symbol, venue_short, venue_long, grp,
       count(*) AS run_hours
FROM flagged
WHERE profitable
GROUP BY 1, 2, 3, 4;

-- --- The answer ------------------------------------------------------------
SELECT
    s.canonical_symbol,
    s.venue_short,
    s.venue_long,
    count(*)                                          AS hours_observed,
    round(100.0 * avg(CASE WHEN s.net_spread_bps_apr > 0
                           THEN 1 ELSE 0 END), 1)     AS pct_hours_profitable,
    round(median(s.gross_spread_bps_apr), 0)          AS median_gross_bps,
    round(median(s.net_spread_bps_apr), 0)            AS median_net_bps,
    round(max(r.run_hours) / 24.0, 1)                 AS longest_window_days,
    round(median(r.run_hours), 1)                     AS median_window_hours,
    -- How often does the sign flip? High flip count = noise, not structure.
    count(DISTINCT r.grp)                             AS n_windows
FROM spreads s
LEFT JOIN runs r USING (canonical_symbol, venue_short, venue_long)
GROUP BY 1, 2, 3
HAVING pct_hours_profitable > 5
ORDER BY median_window_hours DESC NULLS LAST, median_net_bps DESC
LIMIT 50;

-- Read the output by median_window_hours FIRST, not median_net_bps.
-- A 300bp spread lasting 2 hours is a data artifact.
-- A 40bp spread lasting 9 days is a trade.
