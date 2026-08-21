"""Funding spread explorer.

    streamlit run app.py

Three tabs:
  Spread analysis  - does the spread clear break-even, and for how long?
  Position & risk  - what would it earn, and where do you get liquidated?
  Screener         - every combination ranked, so you don't click through them.

The long leg can be another venue's perp, OR spot with a manually entered APY.
The second mode is cash-and-carry: hold the asset, short the perp, collect
funding. Different capital maths, one liquidatable leg instead of two, and a
different right answer for the yield basis - see the Mean/Median note below.

Everything user-facing is in % APR. Fees stay in bps in the sidebar because
that's how exchanges quote them, and get converted internally.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from src.store import load

st.set_page_config(page_title="Funding spread explorer", layout="wide")

SPOT = "— spot / custom APY —"
DATA_DIR = Path("data/funding")


def _data_fingerprint() -> tuple:
    """mtime + size of every parquet file, so a new backfill busts the cache."""
    return tuple(
        (f.stat().st_mtime, f.stat().st_size) for f in sorted(DATA_DIR.rglob("*.parquet"))
    )


@st.cache_data
def _daily_apr(fingerprint: tuple) -> pd.DataFrame:
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

    Only anchors the liquidation display - the funding analysis doesn't depend
    on it, and equity perps won't resolve here at all.
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
    """Lengths of each consecutive True stretch."""
    groups = (mask != mask.shift()).cumsum()
    return mask.groupby(groups).sum().loc[lambda s: s > 0]


data = _daily_apr(_data_fingerprint())
if data.empty:
    st.error("No data. Run the backfill first.")
    st.stop()

# --- controls --------------------------------------------------------------
with st.sidebar:
    st.header("Pair")
    symbol = st.selectbox("Symbol", sorted(data["canonical_symbol"].unique()))

    venues = sorted(data.loc[data["canonical_symbol"] == symbol, "venue"].unique())

    va = st.selectbox("Venue A (short perp / receive)", venues, index=0)

    vb = st.selectbox(
        "Long leg", venues + [SPOT],
        index=1 if len(venues) > 1 else len(venues),
        help="Another venue's perp, or spot. Spot means cash-and-carry: you own "
             "the asset outright, so that leg cannot be liquidated.",
    )

    spot_mode = vb == SPOT
    custom_apy = 0.0
    if spot_mode:
        custom_apy = st.number_input(
            "Long leg APY (%)", -50.0, 100.0, 0.0, 0.5,
            help="0 for plain spot sitting in a wallet. Positive if the asset "
                 "earns yield (staked ETH, a yield-bearing stable, lending it "
                 "out). Negative for the cost of holding it - borrow rate, or "
                 "the drag on a tokenised equity vs owning the share.",
        )

    st.header("Your costs")
    st.caption("Fees in bps, as exchanges quote them. Defaults are placeholders - "
               "replace with your own fee tier.")
    round_trip_bps = st.number_input(
        "Round-trip fees (bps)", 0.0, 100.0, 16.0, 1.0,
        help="Exchange fees for the whole trade: open both legs, close both "
             "legs. At 4bps taker per leg that's 16bps total. Usually lower at "
             "size, and lower again if you can get filled as maker.",
    )
    slippage_bps = st.number_input(
        "Slippage, both legs (bps)", 0.0, 100.0, 6.0, 1.0,
        help="What you lose crossing the spread, over and above fees. Grows "
             "with position size, shrinks with book depth - much worse on thin "
             "altcoin perps than on BTC.",
    )
    hold_days = st.slider(
        "Assumed hold (days)", 1, 90, 14,
        help="How long you expect to keep the position open. This spreads the "
             "fixed cost out: hold twice as long and the cost per day halves, "
             "so the break-even line drops. Don't set it longer than the "
             "'longest stretch' figure unless you're in spot mode - otherwise "
             "you're amortising over a hold you could never achieve.",
    )

    yield_basis = st.radio(
        "Yield basis", ["Mean", "Median"],
        index=0 if spot_mode else 1, horizontal=True,
        help="MEAN: right when you hold through quiet periods and collect "
             "whatever comes - cash-and-carry. Zero-funding days cost you "
             "nothing, so the occasional spike still counts.\n\n"
             "MEDIAN: right when you only enter while the spread is wide and "
             "exit when it isn't - the cross-venue spread trade, where a "
             "collapsed spread means you're actively paying.",
    )

    st.header("Position")
    capital = st.number_input(
        "Capital deployed (USD)", 1_000, 100_000_000, 100_000, 1_000,
        help="Total capital committed across both legs.",
    )
    leverage = st.slider(
        "Leverage on short perp", 1.0, 10.0, 2.0, 0.5,
        help="Notional of the short perp divided by the margin backing it. "
             "In spot mode this applies only to the short leg - the spot leg "
             "is unlevered by definition.",
    )
    mmr_pct = st.number_input(
        "Maintenance margin (%)", 0.1, 10.0, 0.5, 0.1,
        help="The margin ratio below which the venue liquidates you. Varies by "
             "venue and position size - check your exchange's tier table.",
    )

if not spot_mode and va == vb:
    st.warning("Pick two different venues, or use spot for the long leg.")
    st.stop()

# --- compute ---------------------------------------------------------------
sub = data[data["canonical_symbol"] == symbol]
wide = sub.pivot_table(index="dt", columns="venue", values="apr_pct")
if va not in wide:
    st.error("Missing data for that venue.")
    st.stop()

if spot_mode:
    long_label = "spot"
    long_series = pd.Series(custom_apy, index=wide.index)
    spread = (wide[va] - long_series).dropna()
else:
    if vb not in wide:
        st.error("Missing data for that venue.")
        st.stop()
    long_label = vb
    long_series = wide[vb]
    spread = (wide[va] - wide[vb]).dropna()

cost_pct = (round_trip_bps + slippage_bps) / 100.0
breakeven = cost_pct * (365.0 / hold_days)

above = spread > breakeven
runs = run_lengths(above)

# Capital maths differs by mode.
#
#   perp/perp : margin splits evenly across two venues. Notional per leg is
#               (C/2)*L, so the return multiplier on capital is L/2.
#
#   spot      : you pay full price N for the asset, plus N/L margin for the
#               short. C = N(1 + 1/L), so N = C*L/(L+1) and the multiplier is
#               L/(L+1) - approaches 1 but never exceeds it.
if spot_mode:
    multiplier = leverage / (leverage + 1.0)
    notional_per_leg = capital * multiplier
else:
    multiplier = leverage / 2.0
    notional_per_leg = (capital / 2.0) * leverage

# The yield basis matters enormously on skewed distributions. A perp that pays
# zero 75% of the time and spikes occasionally has a median of 0 and a healthy
# mean - and for a position you simply hold, the mean is what you collect.
basis_spread = spread.mean() if yield_basis == "Mean" else spread.median()
skewed = abs(spread.mean() - spread.median()) > max(1.0, 0.5 * abs(spread.median()))

gross_yield = basis_spread * multiplier
net_yield = (basis_spread - breakeven) * multiplier

# --- headline numbers (shared across tabs) ---------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Break-even", f"{breakeven:.1f}% APR",
          help=f"{cost_pct:.2f}% of notional, amortised over a {hold_days}-day hold")
c2.metric(f"{yield_basis} spread", f"{basis_spread:.1f}% APR",
          help=f"mean {spread.mean():.1f}%  /  median {spread.median():.1f}%")
c3.metric("Days above line", f"{100 * above.mean():.1f}%")
c4.metric("Longest stretch", f"{int(runs.max())}d" if len(runs) else "0d",
          help=f"You need {hold_days}d to break even")

if skewed:
    st.info(
        f"**Skewed distribution.** Mean {spread.mean():.1f}% vs median "
        f"{spread.median():.1f}% APR - this pair pays nothing much of the time "
        f"and spikes occasionally. Which one is right depends on the trade: "
        f"if you hold through the quiet periods (cash-and-carry), use the mean. "
        f"If you only enter when the spread is wide, use the median."
    )

tab_spread, tab_position, tab_screener = st.tabs(
    ["Spread analysis", "Position & risk", "Screener"]
)

# ===========================================================================
# TAB 1 - does the opportunity exist, and does it last?
# ===========================================================================
with tab_spread:
    st.subheader("Spread vs break-even")

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=spread.index, y=spread.values, mode="lines",
            name=f"{va} - {long_label}",
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
    fig.add_hline(y=spread.mean(), line=dict(color="#54a24b", width=1.5, dash="dot"),
                  annotation_text=f"mean {spread.mean():.1f}%",
                  annotation_position="bottom left")

    for _, grp in above.groupby((above != above.shift()).cumsum()):
        if grp.iloc[0]:
            fig.add_vrect(x0=grp.index[0], x1=grp.index[-1], fillcolor="#54a24b",
                          opacity=0.18, line_width=0)

    fig.update_layout(
        height=440, margin=dict(l=10, r=10, t=60, b=10),
        yaxis_title="Funding spread (% APR)", xaxis_title="Date", showlegend=False,
        title=dict(text=f"{symbol} - short {va} vs long {long_label}", y=0.96),
    )
    fig.update_yaxes(ticksuffix="%")
    st.plotly_chart(fig, use_container_width=True)

    if spot_mode:
        st.caption(
            f"Cash-and-carry: hold {symbol.split('-')[0]} spot, short the perp "
            f"on {va}. Positive means the funding you receive beats the "
            f"{custom_apy:.1f}% your spot earns. Note that in this mode a "
            f"collapsed spread costs you nothing - you simply earn nothing that "
            f"day - so the green bands matter less than the mean line."
        )
    else:
        st.caption(
            "Positive means shorting A and going long B earns funding. Green "
            "bands are stretches clearing break-even. A band narrower than your "
            "hold period is not a trade - you'd pay the round trip and exit "
            "before funding covered it."
        )

    st.subheader("Funding rate by leg")

    lines = go.Figure()
    lines.add_trace(
        go.Scatter(x=wide.index, y=wide[va], mode="lines", name=va,
                   line=dict(width=1.4, color="#4c78a8"),
                   hovertemplate=f"{va}<br>%{{x|%Y-%m-%d}}<br>%{{y:.1f}}%% APR"
                                 "<extra></extra>")
    )
    lines.add_trace(
        go.Scatter(x=long_series.index, y=long_series.values, mode="lines",
                   name=long_label,
                   line=dict(width=1.4, color="#f58518",
                             dash="dot" if spot_mode else "solid"),
                   hovertemplate=f"{long_label}<br>%{{x|%Y-%m-%d}}<br>"
                                 "%{y:.1f}%% APR<extra></extra>")
    )
    lines.add_hline(y=0, line=dict(color="#888", width=1))
    lines.update_layout(
        height=380, margin=dict(l=10, r=10, t=80, b=10),
        yaxis_title="Rate (% APR)", xaxis_title="Date",
        title=dict(text=f"{symbol} - {va} vs {long_label}", y=0.95),
        legend=dict(orientation="h", yanchor="bottom", y=1.06, x=0),
        hovermode="x unified",
    )
    lines.update_yaxes(ticksuffix="%")
    st.plotly_chart(lines, use_container_width=True)

    st.caption(
        "Above zero, longs pay shorts. The gap between the lines is what the "
        "chart above measures."
        + (" The dotted line is your assumption, not measured data."
           if spot_mode else "")
    )

    st.subheader("How long do profitable stretches last?")

    if len(runs):
        counts = runs.astype(int).value_counts().sort_index()
        bar = go.Figure(
            go.Bar(x=counts.index, y=counts.values, marker_color="#4c78a8",
                   hovertemplate="%{y} stretch(es) lasting %{x} day(s)"
                                 "<extra></extra>")
        )
        bar.add_vline(x=hold_days, line=dict(color="#e45756", width=2, dash="dash"),
                      annotation_text=f"{hold_days}d needed",
                      annotation_position="top")
        bar.update_layout(
            height=300, margin=dict(l=10, r=10, t=30, b=10),
            xaxis_title="Consecutive days above break-even",
            yaxis_title="Number of stretches", bargap=0.25, showlegend=False,
        )
        bar.update_xaxes(dtick=1, tickangle=0)
        st.plotly_chart(bar, use_container_width=True)
        st.caption(
            f"Bars to the LEFT of the red line are too short to trade. "
            f"Longest stretch was {int(runs.max())} days against {hold_days} "
            f"needed. In spot mode this matters much less - you can sit through "
            f"the gaps."
        )
    else:
        st.info("The spread never clears break-even at this hold period. "
                "Try a longer hold, or lower your cost assumptions.")

# ===========================================================================
# TAB 2 - what it would earn, and what it would cost if it went wrong
# ===========================================================================
with tab_position:
    st.subheader("If you traded this")

    if spot_mode:
        st.info(
            f"**Cash-and-carry.** Buy ${notional_per_leg:,.0f} of spot, short "
            f"the same notional on {va} at {leverage:g}x. Capital splits "
            f"${notional_per_leg:,.0f} into spot and "
            f"${notional_per_leg/leverage:,.0f} into short margin."
        )

    y1, y2, y3, y4 = st.columns(4)
    y1.metric("Notional per leg", f"${notional_per_leg:,.0f}")
    y2.metric("Gross yield on capital", f"{gross_yield:.1f}% APR",
              help=f"{yield_basis} spread x {multiplier:.2f} "
                   + ("(L/(L+1) - spot is unlevered)" if spot_mode
                      else "(leverage/2 - capital splits across two venues)"))
    y3.metric("Net yield on capital", f"{net_yield:.1f}% APR",
              delta=f"${capital * net_yield / 100:,.0f}/yr", delta_color="normal",
              help="After fees and slippage, at the assumed hold period")
    y4.metric("Days the trade exists", f"{100 * above.mean():.0f}% of days",
              help="Share of days the spread cleared break-even.")

    if net_yield <= 0:
        st.error(
            f"**Negative at these assumptions.** The {yield_basis.lower()} "
            f"spread ({basis_spread:.1f}%) doesn't cover the {breakeven:.1f}% "
            f"break-even. Leverage scales gross and costs equally, so it can't "
            f"fix this - it only makes the loss bigger."
            + (f" Note the mean is {spread.mean():.1f}% - if you intend to hold "
               f"through quiet periods rather than trade in and out, switch the "
               f"yield basis." if yield_basis == "Median" and skewed else "")
        )
    elif spot_mode:
        st.warning(
            f"**Assumes you hold continuously for the full year.** Funding is "
            f"collected whenever it's positive and you earn nothing when it "
            f"isn't - you don't lose, but you don't earn either. The round trip "
            f"is paid once, so a longer hold genuinely does improve this. Check "
            f"the history span before extrapolating: a few months of data is "
            f"a regime, not a constant."
        )
    else:
        st.warning(
            f"**Upper bound, not an expectation.** It assumes you're in the "
            f"trade continuously at the {yield_basis.lower()} spread. It only "
            f"cleared break-even on {100 * above.mean():.0f}% of days, and the "
            f"longest unbroken stretch was "
            f"{int(runs.max()) if len(runs) else 0} days against the "
            f"{hold_days} you need."
        )

    if spot_mode:
        st.caption(
            "In spot mode the multiplier is leverage/(leverage+1), which "
            "approaches 1 but never exceeds it - you always pay full price for "
            "the spot leg. Lower ceiling than a levered perp/perp position, but "
            "no second liquidation and no cross-venue margin problem. If the "
            "spot leg is a tokenised asset, put its holding cost in the APY "
            "field as a negative number."
        )
    else:
        st.caption(
            "Leverage multiplies gross funding AND costs by the same factor, so "
            "it never turns a losing spread into a winning one. What it changes "
            "is how far price can move before you're liquidated."
        )

    st.divider()
    st.subheader("Liquidation")

    price = spot_price(symbol)
    adverse_move = (1.0 / leverage) - (mmr_pct / 100.0)

    if adverse_move <= 0:
        st.error("Leverage is too high for that maintenance margin - "
                 "the position would be liquidated immediately.")
    elif price is None:
        st.info(f"Couldn't fetch a current price for {symbol}. At {leverage:g}x "
                f"with {mmr_pct:g}% maintenance margin, the short leg "
                f"liquidates on a **{100 * adverse_move:.1f}%** upward move.")
    else:
        l1, l2, l3 = st.columns(3)
        l1.metric("Current price", f"${price:,.2f}")
        l2.metric(f"Short leg ({va}) liquidates",
                  f"${price * (1 + adverse_move):,.2f}",
                  delta=f"+{100 * adverse_move:.1f}%", delta_color="inverse")
        if spot_mode:
            l3.metric("Spot leg liquidates", "never",
                      help="You own the asset outright. It can lose value, but "
                           "nobody can close it out from under you.")
        else:
            l3.metric(f"Long leg ({vb}) liquidates",
                      f"${price * (1 - adverse_move):,.2f}",
                      delta=f"-{100 * adverse_move:.1f}%", delta_color="inverse")

    if spot_mode:
        st.warning(
            f"**One liquidatable leg instead of two.** If price rises "
            f"{100 * adverse_move:.1f}% the short on {va} is at risk, but your "
            f"spot has gained the same amount and - unlike a perp on another "
            f"venue - you can often post it as collateral on the same exchange, "
            f"or sell part of it to top up margin. Materially better than the "
            f"cross-venue version, though not instant if the spot sits in "
            f"self-custody."
        )
    else:
        st.error(
            f"**The hedge does not protect you here.** If price rises "
            f"{100 * adverse_move:.1f}%, the short leg on {va} liquidates while "
            f"the long leg on {long_label} is sitting on an equal profit - but "
            f"that profit is on a different exchange and cannot be used as "
            f"margin. You would need to move collateral between venues before "
            f"liquidation: a withdrawal, chain confirmation and deposit credit. "
            f"Minutes at best, hours if a venue pauses withdrawals."
        )

    st.caption(
        "Simplified: assumes isolated margin, a flat maintenance margin rate, "
        "and ignores funding accrual and auto-deleveraging. Real venues use "
        "tiered maintenance margin that rises with position size. Check your "
        "exchange's own liquidation calculator before sizing anything."
    )

# ===========================================================================
# TAB 3 - rank every combination, so you don't click through them by hand
# ===========================================================================
with tab_screener:
    st.subheader("Every combination, ranked")
    st.caption(
        "Uses the cost, hold and yield-basis assumptions from the sidebar. "
        "Sorted by net yield, but read 'max run' alongside it - for a "
        "cross-venue spread trade a high yield with a short max run is a trap."
    )

    sc1, sc2 = st.columns(2)
    with sc1:
        include_spot = st.checkbox(
            "Include spot as a long leg (cash-and-carry at 0% APY)", value=True,
            help="Short the perp, hold spot. One liquidatable leg, no "
                 "cross-venue margin problem.",
        )
    with sc2:
        min_history = st.slider("Minimum history (days)", 30, 365, 90, 30,
                                help="New listings dominate any ranking if you "
                                     "let them. Equity perps are mostly recent, "
                                     "so lower this to see them.")

    @st.cache_data
    def screen(fingerprint: tuple, be: float, mult: float, basis: str,
               with_spot: bool, min_days: int) -> pd.DataFrame:
        df = _daily_apr(fingerprint)
        rows = []

        for sym, grp in df.groupby("canonical_symbol"):
            w = grp.pivot_table(index="dt", columns="venue", values="apr_pct")
            if len(w) < min_days:
                continue

            legs = list(w.columns)
            combos = [(a, b) for a in legs for b in legs if a != b]
            if with_spot:
                combos += [(a, "spot") for a in legs]

            for short_leg, long_leg in combos:
                s = (w[short_leg] if long_leg == "spot"
                     else w[short_leg] - w[long_leg]).dropna()
                if len(s) < min_days:
                    continue

                hot = s > be
                r = run_lengths(hot)
                bs = s.mean() if basis == "Mean" else s.median()
                rows.append({
                    "symbol": sym,
                    "short": short_leg,
                    "long": long_leg,
                    "net yield %": round((bs - be) * mult, 1),
                    "mean %": round(s.mean(), 1),
                    "median %": round(s.median(), 1),
                    "% days above": round(100 * hot.mean(), 1),
                    "max run (d)": int(r.max()) if len(r) else 0,
                    "windows": len(r),
                    "days": len(s),
                })

        out = pd.DataFrame(rows)
        if out.empty:
            return out
        return out.sort_values("net yield %", ascending=False).reset_index(drop=True)

    table = screen(_data_fingerprint(), breakeven, multiplier,
                   yield_basis, include_spot, min_history)

    if table.empty:
        st.info("Nothing has enough history yet. Lower the minimum, or backfill more.")
    else:
        positive = table[table["net yield %"] > 0]

        s1, s2, s3 = st.columns(3)
        s1.metric("Combinations tested", len(table))
        s2.metric("Positive net yield", len(positive))
        s3.metric("Break-even", f"{breakeven:.1f}% APR")

        if positive.empty:
            st.error(
                f"**Nothing clears the bar.** No combination has a "
                f"{yield_basis.lower()} spread above the {breakeven:.1f}% APR "
                f"break-even. That is a real answer, not a bug."
            )
        else:
            st.success(
                f"**{len(positive)} combination(s)** show positive net yield at "
                f"these assumptions. Open them in the other tabs before "
                f"believing any of it - check the history span and whether the "
                f"mean is being carried by a handful of spikes."
            )

        st.dataframe(
            table, use_container_width=True, height=520,
            column_config={
                "net yield %": st.column_config.NumberColumn(format="%.1f%%"),
                "mean %": st.column_config.NumberColumn(format="%.1f%%"),
                "median %": st.column_config.NumberColumn(format="%.1f%%"),
                "% days above": st.column_config.NumberColumn(format="%.1f%%"),
                "max run (d)": st.column_config.ProgressColumn(
                    "max run (d)", format="%d", min_value=0,
                    max_value=int(table["max run (d)"].max() or 1),
                ),
            },
        )

        st.caption(
            "**mean vs median** is the tell. Where they diverge sharply, the "
            "pair pays nothing most days and spikes occasionally - fine if you "
            "hold through it, useless if you need to time entries. "
            "**max run** answers whether the spread has EVER survived long "
            "enough to pay for its own round trip. A high '% days above' with a "
            "low 'max run' is the worst case: it flickers across the line "
            "without staying there, looking promising while bleeding fees. "
            "**days** is history length - treat anything under ~180 as a "
            "regime, not a constant."
        )
