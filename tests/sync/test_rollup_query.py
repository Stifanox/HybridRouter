"""Generator zapytania rollupu (``app.sync.rollup_query``)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from app.renderers.raw_table import RawTableRenderer
from app.semantic.ast_nodes import MeasureRef
from app.semantic.resolver import SemanticModel
from app.sync.rollup_query import (
    build_rollup_plan,
    render_rollup_query,
    rollup_dimensions,
    rollup_measures,
)
from tests.conftest import ModelFactory

DEC = (dt.date(2022, 12, 1), dt.date(2022, 12, 31))
BQ_TABLE = "bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022"
ROLLUP_DIMENSIONS = ["pickup_date", "vendor_id", "payment_type", "rate_code"]
ROLLUP_MEASURES = [
    "trips_count",
    "total_fare_amount",
    "total_tip_amount",
    "total_revenue",
    "total_distance_miles",
    "total_passenger_count",
    "total_duration_seconds",
]


def test_dimensions_and_measures(nyc_model: SemanticModel) -> None:
    assert [d.name for d in rollup_dimensions(nyc_model.config)] == ROLLUP_DIMENSIONS
    assert list(rollup_measures(nyc_model.config)) == ROLLUP_MEASURES  # bez COUNT_DISTINCT


def test_plan(nyc_model: SemanticModel) -> None:
    plan = build_rollup_plan(nyc_model.config, DEC)
    assert [d.name for d in plan.dimensions] == ROLLUP_DIMENSIONS
    assert [m.name for m in plan.metrics] == ROLLUP_MEASURES
    assert all(m.expr == MeasureRef(m.name) and m.additive for m in plan.metrics)
    assert plan.date_range == DEC
    assert [(f.dimension.name, f.op, f.values) for f in plan.filters] == [
        ("pickup_date", ">=", (DEC[0],)),
        ("pickup_date", "<=", (DEC[1],)),
    ]
    assert [o.field for o in plan.order_by] == ROLLUP_DIMENSIONS
    assert plan.limit is None
    assert set(plan.used_dimensions) == set(ROLLUP_DIMENSIONS)
    assert plan.used_base_measures == frozenset(ROLLUP_MEASURES)


def test_bigquery_sql_uses_original_aggregations(nyc_model: SemanticModel) -> None:
    sql = render_rollup_query(nyc_model.config, BQ_TABLE, DEC, "bigquery")
    tree = sqlglot.parse_one(sql, read="bigquery")
    aliases = {p.alias: p.this.sql("bigquery") for p in tree.expressions}
    assert aliases == {
        "pickup_date": "DATE(pickup_datetime)",
        "vendor_id": "CAST(vendor_id AS STRING)",
        "payment_type": "CAST(payment_type AS STRING)",
        "rate_code": "CAST(rate_code AS STRING)",
        "trips_count": "COUNT(1)",
        "total_fare_amount": "SUM(fare_amount)",
        "total_tip_amount": "SUM(tip_amount)",
        "total_revenue": "SUM(total_amount)",
        "total_distance_miles": "SUM(trip_distance)",
        "total_passenger_count": "SUM(passenger_count)",
        "total_duration_seconds": "SUM(TIMESTAMP_DIFF(dropoff_datetime, pickup_datetime, SECOND))",
    }
    assert "COUNT(DISTINCT" not in sql.upper()
    table = tree.find(exp.Table)
    assert table is not None and table.sql("bigquery") == BQ_TABLE
    where = tree.args["where"].sql("bigquery")
    assert "DATE(pickup_datetime) >= CAST('2022-12-01' AS DATE)" in where
    assert "DATE(pickup_datetime) <= CAST('2022-12-31' AS DATE)" in where
    assert len(tree.args["group"].expressions) == 4


def test_same_plan_as_raw_table_renderer(nyc_model: SemanticModel) -> None:
    """Generator nie ma własnej logiki SQL – to RawTableRenderer na planie rollupu."""
    plan = build_rollup_plan(nyc_model.config, DEC)
    for dialect in ("duckdb", "bigquery"):
        assert render_rollup_query(nyc_model.config, "raw_trips", DEC, dialect) == (
            RawTableRenderer("raw_trips").render(plan, dialect)
        )


def test_duckdb_dialect(nyc_model: SemanticModel) -> None:
    sql = render_rollup_query(nyc_model.config, "raw_trips", DEC, "duckdb", pretty=True)
    assert "DATE_DIFF('SECOND', pickup_datetime, dropoff_datetime)" in sql
    assert "CAST(pickup_datetime AS DATE) AS pickup_date" in sql
    assert "FROM raw_trips" in sql


def test_empty_range_rejected(nyc_model: SemanticModel) -> None:
    with pytest.raises(ValueError, match="empty date range"):
        build_rollup_plan(nyc_model.config, (DEC[1], DEC[0]))


def test_single_day_range(nyc_model: SemanticModel) -> None:
    plan = build_rollup_plan(nyc_model.config, (DEC[0], DEC[0]))
    assert plan.date_range == (DEC[0], DEC[0])


def test_model_without_additive_measures_rejected(
    nyc_raw: dict[str, Any], model_from: ModelFactory
) -> None:
    nyc_raw["base_measures"] = {"zones": {"aggregation": "COUNT_DISTINCT", "sql": "x"}}
    nyc_raw["derived_metrics"] = {}
    model = model_from(nyc_raw)
    with pytest.raises(ValueError, match="no additive base measures"):
        build_rollup_plan(model.config, DEC)


def test_dimensions_outside_rollup_are_skipped(
    nyc_raw: dict[str, Any], model_from: ModelFactory
) -> None:
    nyc_raw["dimensions"]["vendor_id"]["in_rollup"] = False
    model = model_from(nyc_raw)
    plan = build_rollup_plan(model.config, DEC)
    assert [d.name for d in plan.dimensions] == ["pickup_date", "payment_type", "rate_code"]
