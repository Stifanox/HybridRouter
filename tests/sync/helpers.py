"""Pomocnicze funkcje testów cache: publikacja małych tabel dla wybranych dni."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa

from app.sync.publisher import Clock, PublicationResult, publish_partitions, utc_now


def day_table(days: Sequence[dt.date], value: int = 1, rows_per_day: int = 1) -> pa.Table:
    """Tabela ``pickup_date`` + ``x`` (``value``) z ``rows_per_day`` wierszami na dzień."""
    dates = [day for day in days for _ in range(rows_per_day)]
    return pa.table(
        {
            "pickup_date": pa.array(dates, pa.date32()),
            "x": pa.array([value] * len(dates), pa.int64()),
        }
    )


def publish_days(
    base: Path,
    days: Sequence[dt.date],
    *,
    value: int = 1,
    date_range: tuple[dt.date, dt.date] | None = None,
    table: pa.Table | None = None,
    retention: dt.timedelta = dt.timedelta(hours=1),
    clock: Clock = utc_now,
) -> PublicationResult:
    """Publikuje dni ``days`` (zakres domyślnie ``[min, max]``) do cache w ``base``."""
    if date_range is None:
        date_range = (min(days), max(days))
    return publish_partitions(
        day_table(days, value) if table is None else table,
        base,
        "pickup_date",
        date_range,
        retention=retention,
        clock=clock,
    )
