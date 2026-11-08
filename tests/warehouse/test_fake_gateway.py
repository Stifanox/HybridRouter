"""``FakeBigQueryGateway`` – atrapa hurtowni na DuckDB (ADR-014).

Najważniejsza własność: SQL wygenerowany dla BigQuery (``RawTableRenderer@bigquery``)
wykonany w atrapie daje ten sam wynik co ``RawTableRenderer@duckdb`` na tych samych danych
surowych – atrapa nie zmienia semantyki zapytania, więc testy strażnika kosztów, API i
synchronizatora na atrapie testują ścieżkę produkcyjną.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.planner.builder import plan_query
from app.renderers.raw_table import RawTableRenderer
from app.semantic.resolver import SemanticModel
from app.warehouse.cost_guard import MIN_BYTES_BILLED
from app.warehouse.fake_gateway import (
    BYTES_PER_VALUE,
    RAW_TABLE_NAME,
    FakeBigQueryGateway,
    FakeCall,
    billed_bytes,
    integer_decimals_to_int64,
)
from app.warehouse.gateway import WarehouseQueryError, WarehouseTimeoutError
from tests.conftest import SYNTH_END, SYNTH_START, make_request
from tests.test_differential import assert_same_result

DATASET_TABLE = "bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022"


@pytest.fixture(scope="module")
def fake(synthetic_raw: pa.Table) -> Iterator[FakeBigQueryGateway]:
    gateway = FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE)
    yield gateway
    gateway.close()


@pytest.fixture(scope="module")
def reference(synthetic_raw: pa.Table) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(":memory:")
    connection.register(RAW_TABLE_NAME, synthetic_raw)
    yield connection
    connection.close()


def run(gateway: FakeBigQueryGateway, sql: str, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "location": "US",
        "maximum_bytes_billed": None,
        "use_query_cache": False,
        "timeout_s": 30.0,
    }
    kwargs.update(overrides)
    return gateway.run(sql, **kwargs)


def _range(start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    return [
        {"field": "pickup_date", "op": ">=", "value": start.isoformat()},
        {"field": "pickup_date", "op": "<=", "value": end.isoformat()},
    ]


FULL = _range(SYNTH_START, SYNTH_END)
ONE_DAY = [{"field": "pickup_date", "op": "==", "value": "2022-12-02"}]

PLANS: dict[str, dict[str, Any]] = {
    "example_request": {},
    "fallback_dimension": {
        "dimensions": ["dropoff_location_id"],
        "metrics": ["trips_count", "total_revenue", "avg_revenue_per_mile"],
        "filters": FULL,
        "order_by": [],
        "limit": None,
    },
    "holistic_measure": {
        "dimensions": ["pickup_date", "payment_type"],
        "metrics": ["unique_pickup_zones", "total_duration_seconds", "avg_trip_speed_mph"],
        "filters": FULL,
        "order_by": [],
        "limit": None,
    },
    "all_derived_no_dimensions": {
        "dimensions": [],
        "metrics": [
            "tip_to_fare_ratio_pct",
            "non_fare_surcharges",
            "surcharge_to_revenue_roi_pct",
            "avg_fare_per_trip",
            "total_passenger_count",
        ],
        "filters": [],
        "order_by": [],
        "limit": None,
    },
    "in_and_exclusions": {
        "dimensions": ["vendor_id", "rate_code"],
        "metrics": ["trips_count", "total_tip_amount", "total_distance_miles"],
        "filters": [
            *FULL,
            {"field": "payment_type", "op": "in", "value": ["1", "2"]},
            {"field": "rate_code", "op": "!=", "value": "5"},
        ],
        "order_by": [],
        "limit": None,
    },
}


@pytest.mark.parametrize("name", PLANS)
def test_bigquery_sql_matches_duckdb_render(
    name: str,
    fake: FakeBigQueryGateway,
    reference: duckdb.DuckDBPyConnection,
    nyc_model: SemanticModel,
) -> None:
    plan = plan_query(make_request(**PLANS[name]), nyc_model)
    bigquery_sql = RawTableRenderer(DATASET_TABLE).render(plan, "bigquery")
    duckdb_sql = RawTableRenderer(RAW_TABLE_NAME).render(plan, "duckdb")

    fake_rows = run(fake, bigquery_sql).table.to_pylist()
    expected_rows = reference.sql(duckdb_sql).to_arrow_table().to_pylist()

    assert fake_rows, "comparison would be vacuous"
    dimensions = [d.name for d in plan.dimensions]
    assert len(fake_rows) == len(expected_rows)
    for metric in plan.metrics:
        assert_same_result(expected_rows, fake_rows, dimensions, metric.name)


def test_example_request_preserves_order_and_limit(
    fake: FakeBigQueryGateway, nyc_model: SemanticModel
) -> None:
    plan = plan_query(make_request(limit=3), nyc_model)
    rows = run(fake, RawTableRenderer(DATASET_TABLE).render(plan, "bigquery")).table.to_pylist()
    assert len(rows) == 3
    assert [r["pickup_date"] for r in rows] == sorted(r["pickup_date"] for r in rows)


def test_integer_sums_have_bigquery_types(
    fake: FakeBigQueryGateway, nyc_model: SemanticModel
) -> None:
    plan = plan_query(
        make_request(
            dimensions=[],
            metrics=["trips_count", "total_passenger_count"],
            filters=[],
            order_by=[],
        ),
        nyc_model,
    )
    table = run(fake, RawTableRenderer(DATASET_TABLE).render(plan, "bigquery")).table
    # COUNT -> INT64, SUM(INT64) -> INT64 (w DuckDB: HUGEINT / decimal128(38, 0)).
    assert table.schema.field("trips_count").type == pa.int64()
    assert table.schema.field("total_passenger_count").type == pa.int64()


def test_to_duckdb_replaces_table_and_keeps_alias(fake: FakeBigQueryGateway) -> None:
    sql = f"SELECT t.vendor_id FROM `{DATASET_TABLE}` AS t WHERE t.vendor_id = 1"
    translated = fake.to_duckdb(sql)
    assert RAW_TABLE_NAME in translated
    assert "bigquery-public-data" not in translated
    assert run(fake, sql).table.num_rows > 0


def test_cte_names_are_not_treated_as_unknown_tables(fake: FakeBigQueryGateway) -> None:
    sql = f"WITH x AS (SELECT vendor_id FROM {DATASET_TABLE}) SELECT COUNT(*) AS n FROM x"
    assert run(fake, sql).table.to_pylist()[0]["n"] > 0


def test_unknown_table_is_not_found(fake: FakeBigQueryGateway) -> None:
    with pytest.raises(WarehouseQueryError) as exc_info:
        fake.dry_run("SELECT 1 FROM other_project.ds.table", location="US")
    assert (exc_info.value.reason, exc_info.value.status_code) == ("notFound", 404)
    assert "other_project.ds.table" in exc_info.value.message


def test_syntax_error_is_rejected(fake: FakeBigQueryGateway) -> None:
    with pytest.raises(WarehouseQueryError) as exc_info:
        run(fake, "SELECT FROM WHERE (")
    assert exc_info.value.reason == "invalidQuery"


def test_execution_error_is_rejected(fake: FakeBigQueryGateway) -> None:
    with pytest.raises(WarehouseQueryError) as exc_info:
        run(fake, f"SELECT no_such_column FROM {DATASET_TABLE}")
    assert exc_info.value.job_id is not None
    assert "no_such_column" in exc_info.value.message


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #


def test_default_estimate_is_rows_times_columns(
    fake: FakeBigQueryGateway, synthetic_raw: pa.Table
) -> None:
    all_rows = synthetic_raw.num_rows
    one_column = f"SELECT SUM(fare_amount) FROM {DATASET_TABLE}"
    two_columns = f"SELECT SUM(fare_amount), SUM(tip_amount) FROM {DATASET_TABLE}"
    assert fake.dry_run(one_column, location="US").bytes_processed == all_rows * BYTES_PER_VALUE
    assert fake.dry_run(two_columns, location="US").bytes_processed == 2 * all_rows * 8
    assert fake.dry_run(f"SELECT COUNT(*) FROM {DATASET_TABLE}", location="US").bytes_processed == 0


def test_estimate_follows_where_filter(fake: FakeBigQueryGateway, nyc_model: SemanticModel) -> None:
    renderer = RawTableRenderer(DATASET_TABLE)
    metrics = {"metrics": ["total_fare_amount"], "dimensions": [], "order_by": [], "limit": None}
    one_day = renderer.render(
        plan_query(make_request(filters=ONE_DAY, **metrics), nyc_model), "bigquery"
    )
    full = renderer.render(plan_query(make_request(filters=FULL, **metrics), nyc_model), "bigquery")

    one_day_bytes = fake.dry_run(one_day, location="US").bytes_processed
    full_bytes = fake.dry_run(full, location="US").bytes_processed
    assert 0 < one_day_bytes < full_bytes
    # Jeden dzień z 11 dni z danymi (dzień 2022-12-04 jest pusty): ok. 1/11 bajtów.
    assert full_bytes / one_day_bytes == pytest.approx(11, rel=0.05)


def test_constant_and_callable_estimators(synthetic_raw: pa.Table) -> None:
    sql = f"SELECT SUM(fare_amount) FROM {DATASET_TABLE}"
    constant = FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE, bytes_estimator=7)
    seen: list[str] = []

    def estimator(text: str) -> int:
        seen.append(text)
        return 42

    dynamic = FakeBigQueryGateway(
        synthetic_raw, dataset_table=DATASET_TABLE, bytes_estimator=estimator
    )
    negative = FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE, bytes_estimator=-1)
    try:
        assert constant.dry_run(sql, location="US").bytes_processed == 7
        assert dynamic.dry_run(sql, location="US").bytes_processed == 42
        assert seen == [sql]
        assert run(dynamic, sql).stats.bytes_processed == 42
        # Wstrzyknięty estymator nie wyłącza walidacji SQL.
        with pytest.raises(WarehouseQueryError):
            constant.dry_run("SELECT 1 FROM unknown_table", location="US")
        with pytest.raises(ValueError, match="negative"):
            negative.dry_run(sql, location="US")
    finally:
        for gateway in (constant, dynamic, negative):
            gateway.close()


# --------------------------------------------------------------------------- #
# Wykonanie: statystyki, limit bajtów, limit czasu, historia
# --------------------------------------------------------------------------- #


def test_job_stats_are_consistent(synthetic_raw: pa.Table) -> None:
    gateway = FakeBigQueryGateway(
        synthetic_raw, dataset_table=DATASET_TABLE, bytes_estimator=15 * 1024**2 + 1
    )
    try:
        first = run(gateway, f"SELECT COUNT(*) AS n FROM {DATASET_TABLE}").stats
        second = run(gateway, f"SELECT COUNT(*) AS n FROM {DATASET_TABLE}").stats
    finally:
        gateway.close()
    assert first.job_id != second.job_id
    assert first.created <= first.started <= first.ended
    assert first.created.tzinfo is dt.UTC
    assert first.queue_ms == 0
    assert first.server_execution_ms >= 0
    assert first.bytes_processed == 15 * 1024**2 + 1
    assert first.bytes_billed == 16 * 1024**2  # zaokrąglenie w górę do 1 MB
    assert first.cache_hit is False
    assert first.result_fetch_ns >= 0


def test_execution_delay_is_visible_in_server_time(synthetic_raw: pa.Table) -> None:
    gateway = FakeBigQueryGateway(
        synthetic_raw, dataset_table=DATASET_TABLE, execution_delay_s=0.05
    )
    try:
        stats = run(gateway, f"SELECT 1 AS x FROM {DATASET_TABLE} LIMIT 1", timeout_s=5).stats
    finally:
        gateway.close()
    assert stats.server_execution_ms >= 50


def test_maximum_bytes_billed_is_enforced(synthetic_raw: pa.Table) -> None:
    gateway = FakeBigQueryGateway(
        synthetic_raw, dataset_table=DATASET_TABLE, bytes_estimator=50 * 1024**2
    )
    sql = f"SELECT 1 AS x FROM {DATASET_TABLE} LIMIT 1"
    try:
        with pytest.raises(WarehouseQueryError) as exc_info:
            run(gateway, sql, maximum_bytes_billed=50 * 1024**2 - 1)
        assert exc_info.value.reason == "bytesBilledLimitExceeded"
        assert run(gateway, sql, maximum_bytes_billed=50 * 1024**2).table.num_rows == 1
    finally:
        gateway.close()


def test_small_query_bills_minimum(fake: FakeBigQueryGateway) -> None:
    sql = f"SELECT SUM(fare_amount) AS s FROM {DATASET_TABLE}"
    with pytest.raises(WarehouseQueryError, match="10485760 or higher"):
        run(fake, sql, maximum_bytes_billed=1024)
    assert (
        run(fake, sql, maximum_bytes_billed=MIN_BYTES_BILLED).stats.bytes_billed == MIN_BYTES_BILLED
    )


def test_timeout_cancels_job(synthetic_raw: pa.Table) -> None:
    gateway = FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE, execution_delay_s=10)
    try:
        with pytest.raises(WarehouseTimeoutError) as exc_info:
            run(gateway, f"SELECT 1 AS x FROM {DATASET_TABLE}", timeout_s=0.05)
        assert exc_info.value.cancelled is True
        assert exc_info.value.timeout_s == 0.05
        assert gateway.cancelled_jobs == [exc_info.value.job_id]
    finally:
        gateway.close()


def test_call_history(synthetic_raw: pa.Table) -> None:
    gateway = FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE)
    sql = f"SELECT 1 AS x FROM {DATASET_TABLE} LIMIT 1"
    try:
        gateway.dry_run(sql, location="EU")
        run(gateway, sql, maximum_bytes_billed=MIN_BYTES_BILLED, use_query_cache=True, timeout_s=3)
        assert gateway.calls == [
            FakeCall("dry_run", sql, "EU"),
            FakeCall("run", sql, "US", MIN_BYTES_BILLED, True, 3),
        ]
        assert gateway.cancelled_jobs == []
    finally:
        gateway.close()


def test_closed_gateway_refuses_queries(synthetic_raw: pa.Table) -> None:
    gateway = FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE)
    gateway.close()
    gateway.close()  # idempotentne
    with pytest.raises(RuntimeError, match="closed"):
        run(gateway, f"SELECT 1 AS x FROM {DATASET_TABLE}")


def test_negative_delay_rejected(synthetic_raw: pa.Table) -> None:
    with pytest.raises(ValueError, match="execution_delay_s"):
        FakeBigQueryGateway(synthetic_raw, dataset_table=DATASET_TABLE, execution_delay_s=-1)


# --------------------------------------------------------------------------- #
# Źródło danych: plik / katalog Parquet
# --------------------------------------------------------------------------- #

COUNT_SQL = f"SELECT COUNT(*) AS n FROM {DATASET_TABLE}"


def test_loads_parquet_file(tmp_path: Path, synthetic_raw: pa.Table) -> None:
    path = tmp_path / "raw.parquet"
    pq.write_table(synthetic_raw, path)
    gateway = FakeBigQueryGateway(path, dataset_table=DATASET_TABLE)
    try:
        assert run(gateway, COUNT_SQL).table.to_pylist() == [{"n": synthetic_raw.num_rows}]
    finally:
        gateway.close()


def test_loads_parquet_directory(tmp_path: Path, synthetic_raw: pa.Table) -> None:
    half = synthetic_raw.num_rows // 2
    (tmp_path / "part=1").mkdir()
    pq.write_table(synthetic_raw.slice(0, half), tmp_path / "a.parquet")
    pq.write_table(synthetic_raw.slice(half), tmp_path / "part=1" / "b.parquet")
    gateway = FakeBigQueryGateway(str(tmp_path), dataset_table=DATASET_TABLE)
    try:
        assert run(gateway, COUNT_SQL).table.to_pylist() == [{"n": synthetic_raw.num_rows}]
    finally:
        gateway.close()


def test_missing_raw_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="raw data not found"):
        FakeBigQueryGateway(tmp_path / "missing.parquet", dataset_table=DATASET_TABLE)


# --------------------------------------------------------------------------- #
# Funkcje pomocnicze
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("processed", "billed"),
    [
        (0, MIN_BYTES_BILLED),
        (1, MIN_BYTES_BILLED),
        (10 * 1024**2, 10 * 1024**2),
        (10 * 1024**2 + 1, 11 * 1024**2),
    ],
)
def test_billed_bytes(processed: int, billed: int) -> None:
    assert billed_bytes(processed) == billed


def test_integer_decimals_to_int64() -> None:
    table = pa.table(
        {
            "count": pa.array([1, 2], pa.decimal128(38, 0)),
            "money": pa.array([1, 2], pa.decimal128(38, 9)),
            "name": ["a", "b"],
        }
    )
    converted = integer_decimals_to_int64(table)
    assert converted.schema.types == [pa.int64(), pa.decimal128(38, 9), pa.string()]
    assert converted.column("count").to_pylist() == [1, 2]


def test_estimate_for_set_operation_counts_all_rows(
    fake: FakeBigQueryGateway, synthetic_raw: pa.Table
) -> None:
    sql = (
        f"SELECT vendor_id FROM {DATASET_TABLE} WHERE vendor_id = 1 "
        f"UNION ALL SELECT vendor_id FROM {DATASET_TABLE}"
    )
    assert fake.estimate_bytes(sql) == synthetic_raw.num_rows * BYTES_PER_VALUE


def test_estimate_with_invalid_where_is_rejected(fake: FakeBigQueryGateway) -> None:
    with pytest.raises(WarehouseQueryError, match="no_such_column"):
        fake.dry_run(
            f"SELECT SUM(fare_amount) FROM {DATASET_TABLE} WHERE no_such_column = 1",
            location="US",
        )
