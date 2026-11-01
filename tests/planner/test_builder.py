"""Builder planu logicznego: QueryRequest + SemanticModel -> LogicalQuery."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest

from app.planner.builder import build_logical_query, compute_date_range, plan_query
from app.planner.errors import (
    DuplicateFieldError,
    EmptyDateRangeError,
    EmptyMetricsError,
    FilterOnMetricError,
    InvalidFilterValueError,
    InvalidLimitError,
    OrderByFieldNotSelectedError,
    QueryValidationError,
    UnknownDimensionError,
    UnknownMetricError,
    UnknownModelError,
    UnsupportedOperatorError,
)
from app.planner.logical_plan import (
    FILTER_OPERATORS,
    LogicalQuery,
    PlannedDimension,
    PlannedFilter,
    PlannedOrder,
)
from app.semantic.ast_nodes import BinaryOp, Literal, MeasureRef, to_formula
from app.semantic.resolver import SemanticModel
from tests.conftest import ModelFactory, make_request

D = dt.date


def plan(models: dict[str, SemanticModel], **overrides: Any) -> LogicalQuery:
    return build_logical_query(make_request(**overrides), models)


def date_filters(*specs: tuple[str, Any]) -> list[dict[str, Any]]:
    return [{"field": "pickup_date", "op": op, "value": value} for op, value in specs]


# --------------------------------------------------------------------------- #
# Plan dla przykładu z dok. 03
# --------------------------------------------------------------------------- #


def test_example_plan(models: dict[str, SemanticModel]) -> None:
    query = plan(models)

    assert query.model_name == "nyc_yellow_taxi_analytics"
    assert query.partition_key == "pickup_date"
    assert [d.name for d in query.dimensions] == ["pickup_date", "vendor_id"]
    assert query.dimensions[1].definition.sql == "CAST(vendor_id AS STRING)"
    assert [m.name for m in query.metrics] == [
        "trips_count",
        "total_revenue",
        "tip_to_fare_ratio_pct",
        "avg_trip_speed_mph",
    ]
    assert query.metrics[0].expr == MeasureRef("trips_count")
    assert to_formula(query.metrics[2].expr) == "total_tip_amount / total_fare_amount * 100"
    assert all(m.additive for m in query.metrics)
    assert query.additive

    assert query.filters[0].op == ">="
    assert query.filters[0].value == D(2022, 12, 1)
    assert query.filters[2].dimension.name == "payment_type"
    assert query.filters[2].values == ("1",)
    assert query.order_by == (PlannedOrder("pickup_date", "ASC"),)
    assert query.limit == 1000
    assert query.date_range == (D(2022, 12, 1), D(2022, 12, 23))

    assert query.used_base_measures == {
        "trips_count",
        "total_revenue",
        "total_tip_amount",
        "total_fare_amount",
        "total_distance_miles",
        "total_duration_seconds",
    }
    assert query.base_measures["total_fare_amount"].sql == "fare_amount"
    assert list(query.used_dimensions) == ["pickup_date", "vendor_id", "payment_type"]


def test_plan_is_immutable(models: dict[str, SemanticModel]) -> None:
    query = plan(models)
    with pytest.raises(dataclasses.FrozenInstanceError):
        query.limit = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        query.base_measures["x"] = query.base_measures["trips_count"]  # type: ignore[index]
    with pytest.raises(TypeError):
        query.used_dimensions["x"] = query.dimensions[0]  # type: ignore[index]


def test_used_dimensions_include_filter_only_dimensions(models: dict[str, SemanticModel]) -> None:
    query = plan(
        models,
        dimensions=["vendor_id"],
        filters=[{"field": "dropoff_location_id", "op": "==", "value": "132"}],
        order_by=[],
    )
    assert set(query.used_dimensions) == {"vendor_id", "dropoff_location_id"}
    assert not query.used_dimensions["dropoff_location_id"].in_rollup
    assert query.date_range is None


def test_holistic_metric_is_not_additive(models: dict[str, SemanticModel]) -> None:
    query = plan(models, metrics=["trips_count", "unique_pickup_zones"], order_by=[])
    assert [m.additive for m in query.metrics] == [True, False]
    assert not query.additive
    assert "unique_pickup_zones" in query.used_base_measures


def test_multilevel_metric_expanded(models: dict[str, SemanticModel]) -> None:
    query = plan(models, metrics=["surcharge_to_revenue_roi_pct"], order_by=[])
    assert query.used_base_measures == {"total_revenue", "total_fare_amount", "total_tip_amount"}
    assert "non_fare_surcharges" not in to_formula(query.metrics[0].expr)


def test_empty_dimensions_and_no_limit(models: dict[str, SemanticModel]) -> None:
    query = plan(models, dimensions=[], filters=[], order_by=[], limit=None)
    assert query.dimensions == ()
    assert query.limit is None
    assert query.used_dimensions == {}


def test_order_by_metric_desc(models: dict[str, SemanticModel]) -> None:
    query = plan(models, order_by=[{"field": "total_revenue", "direction": "DESC"}])
    assert query.order_by == (PlannedOrder("total_revenue", "DESC"),)


def test_plan_query_ignores_model_name(nyc_model: SemanticModel) -> None:
    query = plan_query(make_request(model="whatever"), nyc_model)
    assert query.model_name == nyc_model.name


# --------------------------------------------------------------------------- #
# Konwersja wartości filtrów
# --------------------------------------------------------------------------- #


def test_number_dimension_conversion(nyc_raw: dict[str, Any], model_from: ModelFactory) -> None:
    nyc_raw["dimensions"]["passenger_bucket"] = {
        "sql": "passenger_count",
        "type": "number",
        "in_rollup": True,
    }
    model = model_from(nyc_raw)
    query = plan_query(
        make_request(
            filters=[
                {"field": "passenger_bucket", "op": "in", "value": [1, 2.5, "3", " 4.25 ", -1]},
                {"field": "passenger_bucket", "op": ">", "value": "0"},
            ]
        ),
        model,
    )
    assert query.filters[0].values == (1, 2.5, 3, 4.25, -1)
    assert type(query.filters[0].values[2]) is int
    assert query.filters[1].value == 0

    for bad in ("abc", "nan", "inf"):
        with pytest.raises(InvalidFilterValueError, match="number"):
            plan_query(
                make_request(filters=[{"field": "passenger_bucket", "op": "==", "value": bad}]),
                model,
            )


def test_string_dimension_conversion(models: dict[str, SemanticModel]) -> None:
    query = plan(models, filters=[{"field": "vendor_id", "op": "in", "value": ["1", 2]}])
    assert query.filters[0].values == ("1", "2")
    with pytest.raises(InvalidFilterValueError, match="expected a string"):
        plan(models, filters=[{"field": "vendor_id", "op": "==", "value": 1.0}])


@pytest.mark.parametrize("value", ["2022/12/01", "20221201", "2022-13-01", "2022-02-30", 20221201])
def test_invalid_date_values(models: dict[str, SemanticModel], value: Any) -> None:
    with pytest.raises(InvalidFilterValueError, match="date"):
        plan(models, filters=date_filters((">=", value)))


def test_list_operator_requires_list(models: dict[str, SemanticModel]) -> None:
    with pytest.raises(InvalidFilterValueError, match="requires a list") as info:
        plan(models, filters=[{"field": "vendor_id", "op": "in", "value": "1"}])
    assert info.value.field == "filters[0].value"
    with pytest.raises(InvalidFilterValueError, match="non-empty"):
        plan(models, filters=[{"field": "vendor_id", "op": "not_in", "value": []}])


def test_scalar_operator_rejects_list(models: dict[str, SemanticModel]) -> None:
    with pytest.raises(InvalidFilterValueError, match="single value"):
        plan(models, filters=[{"field": "vendor_id", "op": "==", "value": ["1"]}])


def test_list_item_error_points_to_item(models: dict[str, SemanticModel]) -> None:
    with pytest.raises(InvalidFilterValueError) as info:
        plan(models, filters=date_filters(("in", ["2022-12-01", "bad"])))
    assert info.value.field == "filters[0].value[1]"


def test_planned_filter_value_on_list_operator(models: dict[str, SemanticModel]) -> None:
    query = plan(models, filters=[{"field": "vendor_id", "op": "in", "value": ["1"]}])
    with pytest.raises(TypeError):
        _ = query.filters[0].value


# --------------------------------------------------------------------------- #
# Błędy walidacji
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "error", "field", "message"),
    [
        (
            {"model": "nyc_yellow_taxi"},
            UnknownModelError,
            "model",
            "did you mean 'nyc_yellow_taxi_analytics'",
        ),
        (
            {"dimensions": ["vendor"]},
            UnknownDimensionError,
            "dimensions[0]",
            "did you mean 'vendor_id'",
        ),
        (
            {"dimensions": ["trips_count"]},
            UnknownDimensionError,
            "dimensions[0]",
            "is a metric; use it in 'metrics'",
        ),
        (
            {"metrics": ["trips_cnt"]},
            UnknownMetricError,
            "metrics[0]",
            "did you mean 'trips_count'",
        ),
        (
            {"metrics": ["vendor_id"], "order_by": []},
            UnknownMetricError,
            "metrics[0]",
            "is a dimension",
        ),
        ({"metrics": []}, EmptyMetricsError, "metrics", "at least one metric"),
        ({"limit": 0}, InvalidLimitError, "limit", "positive"),
        ({"limit": -5}, InvalidLimitError, "limit", "got -5"),
        (
            {"dimensions": ["vendor_id", "vendor_id"]},
            DuplicateFieldError,
            "dimensions[1]",
            "more than once",
        ),
        (
            {"metrics": ["trips_count", "trips_count"]},
            DuplicateFieldError,
            "metrics[1]",
            "more than once",
        ),
        (
            {"filters": [{"field": "total_revenue", "op": ">", "value": 10}]},
            FilterOnMetricError,
            "filters[0].field",
            "HAVING",
        ),
        (
            {"filters": [{"field": "paymnt_type", "op": "==", "value": "1"}]},
            UnknownDimensionError,
            "filters[0].field",
            "did you mean 'payment_type'",
        ),
        (
            {"filters": [{"field": "vendor_id", "op": "LIKE", "value": "1%"}]},
            UnsupportedOperatorError,
            "filters[0].op",
            "'LIKE'",
        ),
        (
            {"filters": [{"field": "vendor_id", "op": "=", "value": "1"}]},
            UnsupportedOperatorError,
            "filters[0].op",
            "allowed: '==', '!='",
        ),
        (
            {"order_by": [{"field": "payment_type", "direction": "ASC"}]},
            OrderByFieldNotSelectedError,
            "order_by[0].field",
            "not in dimensions or metrics",
        ),
        (
            {"order_by": [{"field": "total_revenu", "direction": "ASC"}]},
            OrderByFieldNotSelectedError,
            "order_by[0].field",
            "did you mean 'total_revenue'",
        ),
        (
            {"order_by": [{"field": "vendor_id"}, {"field": "vendor_id", "direction": "DESC"}]},
            DuplicateFieldError,
            "order_by[1].field",
            "more than once",
        ),
    ],
)
def test_validation_errors(
    models: dict[str, SemanticModel],
    overrides: dict[str, Any],
    error: type[QueryValidationError],
    field: str,
    message: str,
) -> None:
    with pytest.raises(error) as info:
        plan(models, **overrides)
    assert isinstance(info.value, QueryValidationError)
    assert info.value.field == field
    assert message in info.value.message
    assert info.value.error_type == error.__name__
    assert str(info.value).startswith(f"{field}: ")


def test_unsupported_operator_lists_all_allowed(models: dict[str, SemanticModel]) -> None:
    with pytest.raises(UnsupportedOperatorError) as info:
        plan(models, filters=[{"field": "vendor_id", "op": "between", "value": "1"}])
    for op in FILTER_OPERATORS:
        assert f"'{op}'" in info.value.message


def test_unknown_model_without_suggestion(models: dict[str, SemanticModel]) -> None:
    with pytest.raises(UnknownModelError) as info:
        plan(models, model="zzz")
    assert info.value.message == "unknown model 'zzz'"


# --------------------------------------------------------------------------- #
# Zakres dat (dok. 03, Moduł 2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ([(">=", "2022-12-01"), ("<=", "2022-12-23")], (D(2022, 12, 1), D(2022, 12, 23))),
        ([(">", "2022-12-01"), ("<", "2022-12-23")], (D(2022, 12, 2), D(2022, 12, 22))),
        ([(">", "2022-12-31"), ("<", "2023-03-01")], (D(2023, 1, 1), D(2023, 2, 28))),
        ([("==", "2022-12-05")], (D(2022, 12, 5), D(2022, 12, 5))),
        ([("in", ["2022-12-07", "2022-12-01", "2022-12-03"])], (D(2022, 12, 1), D(2022, 12, 7))),
        # przecięcie kilku granic
        (
            [(">=", "2022-12-01"), (">=", "2022-12-05"), ("<=", "2022-12-31"), ("<", "2022-12-20")],
            (D(2022, 12, 5), D(2022, 12, 19)),
        ),
        (
            [("in", ["2022-12-01", "2022-12-10"]), (">", "2022-12-03")],
            (D(2022, 12, 4), D(2022, 12, 10)),
        ),
        # != / not_in nie zawężają
        ([("==", "2022-12-05"), ("!=", "2022-12-06")], (D(2022, 12, 5), D(2022, 12, 5))),
        # brak granicy
        ([], None),
        ([(">=", "2022-12-01")], None),
        ([("<=", "2022-12-23")], None),
        ([(">", "2022-12-01"), ("!=", "2022-12-23")], None),
        ([("not_in", ["2022-12-01"])], None),
    ],
)
def test_date_range(
    models: dict[str, SemanticModel], filters: list[tuple[str, Any]], expected: Any
) -> None:
    query = plan(models, filters=date_filters(*filters))
    assert query.date_range == expected


def test_date_range_ignores_other_dimensions(models: dict[str, SemanticModel]) -> None:
    query = plan(models, filters=[{"field": "vendor_id", "op": "==", "value": "1"}])
    assert query.date_range is None


@pytest.mark.parametrize(
    "filters",
    [
        [(">=", "2022-12-10"), ("<=", "2022-12-01")],
        [(">", "2022-12-01"), ("<", "2022-12-02")],
        [("==", "2022-12-01"), ("==", "2022-12-02")],
        [("in", ["2022-12-01", "2022-12-02"]), (">", "2022-12-02")],
        [(">", "9999-12-31")],
        [("<", "0001-01-01")],
    ],
)
def test_contradictory_date_filters(
    models: dict[str, SemanticModel], filters: list[tuple[str, Any]]
) -> None:
    with pytest.raises(EmptyDateRangeError) as info:
        plan(models, filters=date_filters(*filters))
    assert info.value.field == "filters"


def test_compute_date_range_directly(nyc_model: SemanticModel) -> None:
    dim = PlannedDimension("pickup_date", nyc_model.config.dimensions["pickup_date"])
    filters = [
        PlannedFilter(dim, ">=", (D(2022, 1, 1),)),
        PlannedFilter(dim, "<=", (D(2022, 1, 31),)),
    ]
    assert compute_date_range(filters, "pickup_date") == (D(2022, 1, 1), D(2022, 1, 31))
    assert compute_date_range(filters, "other_key") is None


def test_metric_expr_is_expanded_ast(models: dict[str, SemanticModel]) -> None:
    query = plan(models, metrics=["avg_trip_speed_mph"], order_by=[])
    assert query.metrics[0].expr == BinaryOp(
        "/",
        MeasureRef("total_distance_miles"),
        BinaryOp("/", MeasureRef("total_duration_seconds"), Literal(3600.0)),
    )
