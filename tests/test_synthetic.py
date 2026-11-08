"""Generator danych syntetycznych: determinizm i obecność przypadków brzegowych."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pytest

from tests.conftest import SYNTH_DAYS, SYNTH_GAP, SYNTH_ROWS_PER_DAY, SYNTH_START
from tests.fixtures.synthetic import (
    MONEY_COLUMNS,
    NUMERIC_TYPE,
    PAYMENT_DISPUTE,
    PAYMENT_NO_CHARGE,
    RAW_SCHEMA,
    generate_trips,
    to_decimal_money,
)


def _query(table: pa.Table, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect() as con:
        con.register("t", table)
        return con.execute(sql).fetchall()


def test_schema_and_row_count(synthetic_raw: pa.Table) -> None:
    assert synthetic_raw.schema == RAW_SCHEMA
    assert synthetic_raw.num_rows == (SYNTH_DAYS - 1) * SYNTH_ROWS_PER_DAY
    assert synthetic_raw.schema.field("pickup_datetime").type == pa.timestamp("us")
    assert synthetic_raw.schema.field("pickup_datetime").type.tz is None


def test_deterministic_for_same_seed() -> None:
    assert generate_trips(days=3, rows_per_day=50).equals(generate_trips(days=3, rows_per_day=50))
    assert not generate_trips(days=3, rows_per_day=50, seed=1).equals(
        generate_trips(days=3, rows_per_day=50, seed=2)
    )


def test_empty_day_does_not_change_other_days() -> None:
    with_gap = generate_trips(days=4, rows_per_day=20, empty_day_offsets=(1,))
    without_gap = generate_trips(days=4, rows_per_day=20, empty_day_offsets=())
    day0 = without_gap.slice(0, 20)
    assert with_gap.slice(0, 20).equals(day0)
    assert with_gap.slice(20).equals(without_gap.slice(40))


def test_timestamps_have_whole_seconds(synthetic_raw: pa.Table) -> None:
    for column in ("pickup_datetime", "dropoff_datetime"):
        values = synthetic_raw[column].drop_null().to_pylist()
        assert values
        assert all(value.microsecond == 0 for value in values)


def test_gap_day_has_no_rows(synthetic_raw: pa.Table) -> None:
    days = {ts.date() for ts in synthetic_raw["pickup_datetime"].to_pylist()}
    expected = {SYNTH_START + dt.timedelta(days=i) for i in range(SYNTH_DAYS)} - {SYNTH_GAP}
    assert days == expected


def test_midnight_crossing_trips_exist_every_day(synthetic_raw: pa.Table) -> None:
    rows = _query(
        synthetic_raw,
        "SELECT COUNT(DISTINCT CAST(pickup_datetime AS DATE)) FROM t "
        "WHERE CAST(dropoff_datetime AS DATE) > CAST(pickup_datetime AS DATE)",
    )
    assert rows[0][0] == SYNTH_DAYS - 1


def test_zero_fare_group_every_day(synthetic_raw: pa.Table) -> None:
    rows = _query(
        synthetic_raw,
        "SELECT CAST(pickup_datetime AS DATE) d, COALESCE(SUM(fare_amount), 0) FROM t "
        f"WHERE payment_type = {PAYMENT_NO_CHARGE} GROUP BY d",
    )
    assert len(rows) == SYNTH_DAYS - 1
    assert all(total == 0 for _, total in rows)


def test_zero_duration_trips(synthetic_raw: pa.Table) -> None:
    rows = _query(
        synthetic_raw,
        "SELECT payment_type, COUNT(*) FROM t WHERE dropoff_datetime = pickup_datetime "
        "GROUP BY payment_type ORDER BY payment_type",
    )
    by_payment: dict[int, int] = dict(rows)
    assert by_payment[PAYMENT_DISPUTE] >= SYNTH_DAYS - 1
    assert any(p != PAYMENT_DISPUTE for p in by_payment)  # pojedyncze także w innych grupach


@pytest.mark.parametrize(
    "column",
    [
        "fare_amount",
        "tip_amount",
        "total_amount",
        "trip_distance",
        "passenger_count",
        "dropoff_datetime",
        "rate_code",
    ],
)
def test_nulls_present(synthetic_raw: pa.Table, column: str) -> None:
    assert synthetic_raw[column].null_count > 0


def test_daily_parameters_vary(synthetic_raw: pa.Table) -> None:
    rows = _query(
        synthetic_raw,
        "SELECT SUM(tip_amount) / SUM(fare_amount) FROM t GROUP BY CAST(pickup_datetime AS DATE)",
    )
    ratios: list[float] = [r[0] for r in rows]
    assert max(ratios) / min(ratios) > 1.5


def test_decimal_money_variant() -> None:
    table = generate_trips(days=2, rows_per_day=30, money_as_decimal=True)
    floats = generate_trips(days=2, rows_per_day=30)
    for name in MONEY_COLUMNS:
        assert table.schema.field(name).type == NUMERIC_TYPE
        assert table[name].null_count == floats[name].null_count
    assert table["fare_amount"][5].as_py() == Decimal(repr(floats["fare_amount"][5].as_py()))
    assert to_decimal_money(floats).equals(table)
    assert pc.sum(table["fare_amount"]).as_py() > 0


@pytest.mark.parametrize(("days", "rows"), [(0, 10), (2, 2)])
def test_invalid_parameters(days: int, rows: int) -> None:
    with pytest.raises(ValueError):
        generate_trips(days=days, rows_per_day=rows)
