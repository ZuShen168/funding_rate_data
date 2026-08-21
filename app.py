"""Funding spread explorer.

    streamlit run app.py

Two questions:
  1. Does the spread between two venues clear break-even, and for how long?
  2. If I actually put capital behind it, what would I earn - and where do I
     get liquidated?

Everything user-facing is in % APR. Fees stay in bps in the sidebar because
that's how exchanges quote them, and get converted internally.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from src.store import load

st.set_page_config(page_title="Funding spread explorer", layout="wide")


@st.cache_data
def daily_apr() -> pd.DataFrame:
    """Daily average APR per venue/symbol, as a percentage."""
    df = load()
    df = df[df["rate_apr"].notna() & ~df["is_predicted"]].copy()
    df["dt"] = pd.to_datetime(df["settlement_ts"], utc=True).dt.floor("D")
    return (
        df.groupby(["dt", "venue", "canonical_symbol"], as_index=False)["rate_apr"]
        .mean()
        .assign(apr_pct=lambda d: d["rate_apr"] * 100)
    )


@st.cache_data(ttl=300)
def spot_price(canonical: str) -> float | None:
    """Current mark price from Binance futures. Cached 5 min.

    Only used to anchor the liquidation calculation - the funding analysis
    itself doesn't depend on it.
    """
    base, quote, _ = canonical.split("-")
    for sym in (f"{base}{quote}", f"{base}USDT"):
        try:
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/ticker/price",
                params={"symbol": sym}, timeout=5,
            )
            if r.ok:
                return float(r.json()["price"])
        except Exception:
            continue
    return None


def run_lengths(mask: pd.Series) -> pd.Series:
    groups = (mask != mask.shift()).cumsum()
    return mask.groupby(groups).sum().loc[lambda s: s > 0]


data = daily_apr()
if data.empty:
    st.error("No data. Run the backfill first.")
    st.stop()

# --- controls --------------------------------------------------------------
with st.sidebar:
    st.header("Pair")
    symbol = st.selectbox("Symbol", sorted(data["canonical_symbol"].unique()))

    venues = sorted(data.loc[data["canonical_symbol"] == symbol, "venue"].unique())
    if len(venues) < 2:
        st.error(f"Only one venue has data for {symbol}.")
        st.stop()

    va = st.selectbox("Venue A (short / receive)", venues, index=0)
    vb = st.selectbox("Venue B (long / pay)", venues, index=1)

    st.header("Your costs")
    st.caption("Fees in bps, as exchanges quote them. Defaults are placeholders - "
               "replace with your own fee tier.")
    round_trip_bps = st.number_input(
        "Round-trip fees (bps)", 0.0, 100.0, 16.0, 1.0,
        help="Exchange fees for the whole trade. A hedged position means four "
             "fee events: open both legs, close both legs. At 4bps taker per "
             "leg that's 16bps total. Usually lower at size, and lower again "
             "if you can get filled as maker.",
    )
    slippage_bps = st.number_input(
        "Slippage, both legs (bps)", 0.0, 100.0, 6.0, 1.0,
        help="What you lose crossing the spread, over and above fees. You buy "
             "slightly above mid and sell slightly below it, on both legs. "
             "Grows with position size, shrinks with book depth - much worse "
             "on thin altcoin perps than on BTC.",
    )
    hold_days = st.slider(
        "Assumed hold (days)", 1, 60, 14,
        help="How long you expect to keep the position open. This spreads the "
             "fixed cost out: hold twice as long and the cost per day halves, "
             "so the break-even line drops. Drag it and watch the red line move.",
    )

    st.header("Position")
    capital = st.number_input(
        "Capital deployed (USD)", 1_000, 100_000_000, 100_000, 1_000,
        help="Total margin you post, split evenly across the two venues. "
             "Margin is NOT shared between exchanges - that split is what "
             "makes leverage dangerous here.",
    )
    leverage = st.slider(
        "Leverage", 1.0, 10.0, 2.0, 0.5,
        help="Notional per leg divided by margin on that leg. Because capital "
             "splits across two venues, your return multiplier is leverage/2 - "
             "so 2x leverage earns roughly the raw spread.",
    )
    mmr_pct = st.number_input(
        "Maintenance margin (%)", 0.1, 10.0, 0.5, 0.1,
        help="The margin ratio below which the venue liquidates you. Varies by "
             "venue and position size - check your exchange's tier table. "
             "0.5% is typical for modest BTC positions.",
    )

if va == vb:
    st.warning("Pick two different venues.")
    st.stop()

# --- compute ---------------------------------------------------------------
sub = data[data["canonical_symbol"] == symbol]
wide = sub.pivot_table(index="dt", columns="venue", values="apr_pct")
if va not in wide or vb not in wide:
    st.error("Missing data for that combination.")
    st.stop()

spread = (wide[va] - wide[vb]).dropna()

cost_pct = (round_trip_bps + slippage_bps) / 100.0
breakeven = cost_pct * (365.0 / hold_days)

above = spread > breakeven
runs = run_lengths(above)

# Capital splits evenly across the two venues, so notional per leg is
# (capital/2) * leverage, and the return multiplier on capital is leverage/2.
notional_per_leg = (capital / 2.0) * leverage
multiplier = leverage / 2.0

median_spread = spread.median()
gross_yield = median_spread * multiplier
net_yield = (median_spread - breakeven) * multiplier

# --- headline numbers ------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Break-even", f"{breakeven:.1f}% APR",
          help=f"{cost_pct:.2f}% of notional, amortised over a {hold_days}-day hold")
c2.metric("Median spread", f"{median_spread:.1f}% APR")
c3.metric("Days above line", f"{100 * above.mean():.1f}%")
c4.metric("Longest stretch", f"{int(runs.max())}d" if len(runs) else "0d",
          help=f"You need {hold_days}d to break even")

# --- 1. the spread ---------------------------------------------------------
st.subheader("Spread vs break-even")

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=spread.index, y=spread.values, mode="lines", name=f"{va} - {vb}",
        line=dict(width=1.4, color="#4c78a8"),
        hovertemplate="%{x|%Y-%m-%d}<br>%{y:.1f}%% APR<extra></extra>",
    )
)
fig.add_hline(y=0, line=dict(color="#888", width=1))
fig.add_hline(
    y=breakeven, line=dict(color="#e45756", width=2, dash="dash"),
    annotation_text=f"break-even {breakeven:.1f}% (at {hold_days}d hold)",
    annotation_position="top left",
)
fig.add_hline(y=-breakeven, line=dict(color="#e45756", width=2, dash="dash"))

for _, grp in above.groupby((above != above.shift()).cumsum()):
    if grp.iloc[0]:
        fig.add_vrect(x0=grp.index[0], x1=grp.index[-1], fillcolor="#54a24b",
                      opacity=0.18, line_width=0)

fig.update_layout(
    height=440, margin=dict(l=10, r=10, t=60, b=10),
    yaxis_title="Funding spread (% APR)", xaxis_title="Date", showlegend=False,
    title=dict(text=f"{symbol} - {va} minus {vb}", y=0.96),
)
fig.update_yaxes(ticksuffix="%")
st.plotly_chart(fig, use_container_width=True)

st.caption(
    "Positive means shorting A and going long B earns funding. Green bands are "
    "stretches clearing break-even. A band narrower than your hold period is "
    "not a trade - you'd pay the round trip and exit before funding covered it."
)

# --- 2. both venues, raw ---------------------------------------------------
st.subheader("Funding rate by venue")

lines = go.Figure()
for venue, colour in ((va, "#4c78a8"), (vb, "#f58518")):
    lines.add_trace(
        go.Scatter(
            x=wide.index, y=wide[venue], mode="lines", name=venue,
            line=dict(width=1.4, color=colour),
            hovertemplate=f"{venue}<br>%{{x|%Y-%m-%d}}<br>%{{y:.1f}}%% APR<extra></extra>",
        )
    )
lines.add_hline(y=0, line=dict(color="#888", width=1))
lines.update_layout(
    height=380, margin=dict(l=10, r=10, t=80, b=10),
    yaxis_title="Funding rate (% APR)", xaxis_title="Date",
    title=dict(text=f"{symbol} - {va} vs {vb}", y=0.95),
    legend=dict(orientation="h", yanchor="bottom", y=1.06, x=0),
    hovermode="x unified",
)
lines.update_yaxes(ticksuffix="%")
st.plotly_chart(lines, use_container_width=True)

st.caption(
    "Above zero, longs pay shorts. Where the two lines sit on top of each other "
    "there is no trade - the gap between them is what the chart above measures."
)

# --- 3. what you'd actually earn -------------------------------------------
st.subheader("If you traded this")

y1, y2, y3, y4 = st.columns(4)
y1.metric("Notional per leg", f"${notional_per_leg:,.0f}",
          help=f"${capital/2:,.0f} margin per venue x {leverage:g}x")
y2.metric("Gross yield on capital", f"{gross_yield:.1f}% APR",
          help=f"Median spread x {multiplier:g} (leverage/2)")
y3.metric("Net yield on capital", f"{net_yield:.1f}% APR",
          delta=f"${capital * net_yield / 100:,.0f}/yr", delta_color="normal",
          help="After fees and slippage, at the assumed hold period")
y4.metric("At median spread only",
          f"{100 * above.mean():.0f}% of days",
          help="Share of days the spread actually cleared break-even. The yield "
               "above assumes you're in the trade - you can't be, most of the time.")

if net_yield <= 0:
    st.error(
        f"**Negative at these assumptions.** The median spread ({median_spread:.1f}%) "
        f"doesn't cover the {breakeven:.1f}% break-even. Leverage scales gross and "
        f"costs equally, so it can't fix this - it only makes the loss bigger."
    )
else:
    st.warning(
        f"**This is an upper bound, not an expectation.** It assumes you hold "
        f"continuously at the median spread. The spread only cleared break-even on "
        f"{100 * above.mean():.0f}% of days, and the longest unbroken stretch was "
        f"{int(runs.max()) if len(runs) else 0} days against the {hold_days} you need."
    )

st.caption(
    "Note what leverage does and doesn't do: it multiplies gross funding AND "
    "costs by the same factor, so it never turns a losing spread into a winning "
    "one. What it does change is how far price can move before you're liquidated."
)

# --- 4. liquidation --------------------------------------------------------
st.subheader("Liquidation")

price = spot_price(symbol)
adverse_move = (1.0 / leverage) - (mmr_pct / 100.0)

if adverse_move <= 0:
    st.error("Leverage is too high for that maintenance margin - "
             "the position would be liquidated immediately.")
elif price is None:
    st.info(f"Couldn't fetch a current price. At {leverage:g}x with "
            f"{mmr_pct:g}% maintenance margin, either leg liquidates on a "
            f"**{100 * adverse_move:.1f}%** adverse move.")
else:
    l1, l2, l3 = st.columns(3)
    l1.metric("Current price", f"${price:,.2f}")
    l2.metric(f"Short leg ({va}) liquidates", f"${price * (1 + adverse_move):,.2f}",
              delta=f"+{100 * adverse_move:.1f}%", delta_color="inverse")
    l3.metric(f"Long leg ({vb}) liquidates", f"${price * (1 - adverse_move):,.2f}",
              delta=f"-{100 * adverse_move:.1f}%", delta_color="inverse")

    st.error(
        f"**The hedge does not protect you here.** If price rises {100 * adverse_move:.1f}%, "
        f"the short leg on {va} liquidates while the long leg on {vb} is sitting on an "
        f"equal profit - but that profit is on a different exchange and cannot be used "
        f"as margin. You would need to move collateral between venues before liquidation, "
        f"which means a withdrawal, chain confirmation and deposit credit. Minutes at "
        f"best, hours if a venue pauses withdrawals."
    )

st.caption(
    "Simplified: assumes isolated margin, a flat maintenance margin rate, and "
    "ignores funding accrual and auto-deleveraging. Real venues use tiered "
    "maintenance margin that rises with position size. Check your exchange's "
    "own liquidation calculator before sizing anything."
)

# --- 5. how long do the good stretches last? -------------------------------
st.subheader("How long do profitable stretches last?")

if len(runs):
    counts = runs.astype(int).value_counts().sort_index()
    bar = go.Figure(
        go.Bar(x=counts.index, y=counts.values, marker_color="#4c78a8",
               hovertemplate="%{y} stretch(es) lasting %{x} day(s)<extra></extra>")
    )
    bar.add_vline(x=hold_days, line=dict(color="#e45756", width=2, dash="dash"),
                  annotation_text=f"{hold_days}d needed", annotation_position="top")
    bar.update_layout(
        height=300, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title="Consecutive days above break-even",
        yaxis_title="Number of stretches", bargap=0.25, showlegend=False,
    )
    bar.update_xaxes(dtick=1, tickangle=0)
    st.plotly_chart(bar, use_container_width=True)
    st.caption(
        f"Bars to the LEFT of the red line are too short to trade. "
        f"Longest stretch was {int(runs.max())} days against {hold_days} needed."
    )
else:
    st.info("The spread never clears break-even at this hold period. "
            "Try a longer hold, or lower your cost assumptions.")
