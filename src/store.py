"""Storage: parquet partitioned by venue/date, queried with DuckDB.

Deliberately boring. ~1.3M rows/year fits comfortably in memory; the whole
dataset is a few hundred MB. No database server, no Timescale, no Airflow.
If this ever outgrows DuckDB you will know, and by then you will also know
what the access patterns actually are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import pandas as pd

from .schema import FundingObservation, COLUMNS

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "funding"


def to_frame(observations: Iterable[FundingObservation]) -> pd.DataFrame:
    df = pd.DataFrame([o.as_dict() for o in observations])
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    return df[COLUMNS]


def write(observations: Iterable[FundingObservation], root: Path = DATA_ROOT) -> int:
    """Partition by venue and settlement date. Idempotent per partition:
    rewrites the whole partition rather than appending, so re-running a backfill
    for an overlapping window does not duplicate rows."""
    df = to_frame(observations)
    if df.empty:
        return 0

    df["_date"] = pd.to_datetime(df["settlement_ts"], utc=True).dt.date

    written = 0
    for venue, part in df.groupby("venue"):
        out_dir = root / f"venue={venue}"
        out_dir.mkdir(parents=True, exist_ok=True)
        part = part.drop(columns=["_date"])

        # Merge with whatever is already there, so a 7-day run doesn't
        # wipe a year of history.
        existing = out_dir / "data.parquet"
        if existing.exists():
            part = pd.concat([pd.read_parquet(existing), part], ignore_index=True)

        part = (part
                .drop_duplicates(subset=["venue", "venue_symbol", "settlement_ts"],
                                 keep="last")
                .sort_values(["venue_symbol", "settlement_ts"]))
        part.to_parquet(existing, index=False)
        written += len(part)
    return written


def load(root: Path = DATA_ROOT) -> pd.DataFrame:
    files = sorted(root.rglob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


def duckdb_glob(root: Path = DATA_ROOT) -> str:
    """Path pattern to drop into a DuckDB read_parquet() call."""
    return str(root / "**" / "*.parquet")
