"""Funding spread explorer.

    streamlit run app.py

One question, one chart: does the spread between two venues clear my break-even
line, and for how long at a stretch?

Everything user-facing is in % APR. Fees stay in bps in the sidebar because
that's how exchanges quote them, and get converted internally.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
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


def run_lengths(mask: pd.Series) -> pd.Series:
    """Lengths of each consecutive True stretch."""
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
    st.caption("Fees in bps, as exchanges quote them. Defaults are placeholders — "
               "replace with your own fee tier.")
    round_trip_bps = st.number_input(
        "Round-trip fees (bps)", 0.0, 100.0, 16.0, 1.0,
        help="Exchange fees for the whole trade. A hedged position means four "
             "fee events: open both legs, close both legs. At 4bps taker per "
             "leg that's 16bps total. Check your own tier — it's usually lower "
             "at size, and lower again if you can get filled as maker.",
    )
    slippage_bps = st.number_input(
        "Slippage, both legs (bps)", 0.0, 100.0, 6.0, 1.0,
        help="What you lose crossing the spread, over and above fees. You buy "
             "slightly above mid and sell slightly below it, on both legs. "
             "Grows with your position size and shrinks with book depth — so "
             "it's much worse on thin altcoin perps than on BTC.",
    )
    hold_days = st.slider("Assumed hold (days)", 1, 60, 14)

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

# Total cost as a percentage of notional, amortised over the hold and annualised.
cost_pct = (round_trip_bps + slippage_bps) / 100.0
breakeven = cost_pct * (365.0 / hold_days)

above = spread > breakeven
runs = run_lengths(above)

# --- headline numbers ------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Break-even", f"{breakeven:.1f}% APR",
          help=f"{cost_pct:.2f}% of notional, amortised over a {hold_days}-day hold")
c2.metric("Median spread", f"{spread.median():.1f}% APR")
c3.metric("Days above line", f"{100 * above.mean():.1f}%")
c4.metric("Longest stretch", f"{int(runs.max())}d" if len(runs) else "0d",
          help=f"You need {hold_days}d to break even")

# --- both venues, raw --------------------------------------------------------
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
    "there is no trade - the gap between them is what the chart below measures."
)
# --- the chart -------------------------------------------------------------
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
    height=460, margin=dict(l=10, r=10, t=40, b=10),
    yaxis_title="Funding spread (% APR)", xaxis_title="Date", showlegend=False,
    title=f"{symbol} - {va} minus {vb}",
)
fig.update_yaxes(ticksuffix="%")
st.plotly_chart(fig, use_container_width=True)

st.caption(
    "Positive means shorting A and going long B earns funding. Green bands are "
    "stretches clearing break-even. A band narrower than your hold period is not "
    "a trade - you'd pay the round trip and exit before funding covered it."
)

# --- how long do the good stretches last? ----------------------------------
st.subheader("How long do profitable stretches last?")

if len(runs):
    counts = runs.astype(int).value_counts().sort_index()
    bar = go.Figure(
        go.Bar(
            x=counts.index, y=counts.values, marker_color="#4c78a8",
            hovertemplate="%{y} stretch(es) lasting %{x} day(s)<extra></extra>",
        )
    )
    bar.add_vline(
        x=hold_days, line=dict(color="#e45756", width=2, dash="dash"),
        annotation_text=f"{hold_days}d needed", annotation_position="top",
    )
    bar.update_layout(
        height=300, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title="Consecutive days above break-even",
        yaxis_title="Number of stretches",
        bargap=0.25, showlegend=False,
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
