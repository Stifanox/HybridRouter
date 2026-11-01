"""Modele żądania API (dok. 03, sekcja 4) – kształt JSON, wartości domyślne."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.api.models import ExecutionControl, Filter, OrderBy, QueryRequest
from tests.conftest import EXAMPLE_REQUEST


def test_example_request_parses() -> None:
    request = QueryRequest.model_validate(EXAMPLE_REQUEST)
    assert request.dimensions == ["pickup_date", "vendor_id"]
    assert request.filters[2] == Filter(field="payment_type", op="==", value="1")
    assert request.order_by == [OrderBy(field="pickup_date", direction="ASC")]
    assert request.limit == 1000
    assert request.control == ExecutionControl(
        execution_mode="auto", include_telemetry=True, confirm_high_cost=False
    )


def test_defaults() -> None:
    request = QueryRequest.model_validate({"model": "m", "metrics": ["x"]})
    assert request.dimensions == []
    assert request.filters == []
    assert request.order_by == []
    assert request.limit is None
    assert request.control == ExecutionControl()
    assert request.control.execution_mode == "auto"
    assert request.control.include_telemetry is False
    assert request.control.confirm_high_cost is False
    assert OrderBy(field="x").direction == "ASC"


@pytest.mark.parametrize("value", ["1", 1, 1.5, ["a", "b"], [1, 2.5]])
def test_filter_values_accepted(value: Any) -> None:
    assert Filter(field="f", op="==", value=value).value == value


@pytest.mark.parametrize(
    "data",
    [
        {"model": "m", "metrics": ["x"], "unknown": 1},  # extra="forbid"
        {"metrics": ["x"]},  # brak model
        {"model": "m", "metrics": "x"},  # nie lista
        {"model": "m", "metrics": ["x"], "limit": "10"},  # ścisły int
        {"model": "m", "metrics": ["x"], "limit": 1.5},
        {"model": "m", "metrics": ["x"], "filters": [{"field": "f", "op": "==", "value": True}]},
        {"model": "m", "metrics": ["x"], "filters": [{"field": "f", "op": "==", "value": None}]},
        {"model": "m", "metrics": ["x"], "filters": [{"field": "f", "op": "==", "value": [[1]]}]},
        {"model": "m", "metrics": ["x"], "filters": [{"field": "f", "value": 1}]},  # brak op
        {"model": "m", "metrics": ["x"], "order_by": [{"field": "f", "direction": "UP"}]},
        {"model": "m", "metrics": ["x"], "control": {"execution_mode": "force_sqlite"}},
        {"model": "m", "metrics": ["x"], "control": {"force_engine": "duckdb"}},
    ],
)
def test_malformed_requests_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(data)


def test_request_is_immutable() -> None:
    request = QueryRequest.model_validate({"model": "m", "metrics": ["x"]})
    with pytest.raises(ValidationError):
        request.limit = 5  # type: ignore[misc]
