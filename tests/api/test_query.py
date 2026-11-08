"""``POST /api/v1/query``: sukces, telemetria, błędy walidacji, routing (400 / 501), 500."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import service as service_module
from app.api.service import QueryService
from app.config.errors import ConfigIssue, ConfigValidationError
from app.config.settings import Settings
from app.executors.base import ExecutionResult, QueryExecutionError, QueryExecutor
from app.main import create_app
from app.renderers.errors import NonAdditiveMeasureError
from tests.api.conftest import CONFIG_DIR, COVERED_FILTERS, GAP_FILTERS, BodyFactory

QUERY = "/api/v1/query"

TELEMETRY_KEYS = {
    "engine_used",
    "routing_mode",
    "routing_reason",
    "ast_compilation_time_ms",
    "execution_time_ms",
    "total_time_ms",
    "bytes_scanned",
    "estimated_cost_usd",
    "compiled_sql",
}


def _assert_error(response: Any, status: int, error_type: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    payload: dict[str, Any] = response.json()
    assert set(payload) == {"status", "error_type", "message", "details"}
    assert payload["status"] == "error"
    assert payload["error_type"] == error_type
    assert payload["message"]
    return payload


# --------------------------------------------------------------------------- #
# Sukces
# --------------------------------------------------------------------------- #


def test_success_with_telemetry(client: TestClient, body: BodyFactory) -> None:
    response = client.post(QUERY, json=body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"status", "data", "telemetry"}
    assert payload["status"] == "success"

    rows = payload["data"]
    assert rows
    assert list(rows[0]) == [
        "pickup_date",
        "vendor_id",
        "trips_count",
        "total_revenue",
        "tip_to_fare_ratio_pct",
        "avg_trip_speed_mph",
    ]
    assert {row["pickup_date"] for row in rows} == {f"2022-12-{d:02d}" for d in range(5, 13)}
    assert [row["pickup_date"] for row in rows] == sorted(row["pickup_date"] for row in rows)
    assert all(isinstance(row["trips_count"], int) and row["trips_count"] > 0 for row in rows)
    assert all(isinstance(row["total_revenue"], float) for row in rows)

    telemetry = payload["telemetry"]
    assert set(telemetry) == TELEMETRY_KEYS
    assert telemetry["engine_used"] == "duckdb"
    assert telemetry["routing_mode"] == "auto"
    assert telemetry["routing_reason"] == (
        "All dimensions in rollup and date range [2022-12-05..2022-12-12] "
        "available in local Parquet cache."
    )
    assert telemetry["bytes_scanned"] == 0
    assert telemetry["estimated_cost_usd"] == 0.0
    for key in ("ast_compilation_time_ms", "execution_time_ms", "total_time_ms"):
        assert telemetry[key] >= 0
    assert telemetry["total_time_ms"] >= telemetry["execution_time_ms"]
    assert telemetry["total_time_ms"] >= telemetry["ast_compilation_time_ms"]

    sql = telemetry["compiled_sql"]
    assert "COALESCE(SUM(trips_count), 0) AS trips_count" in sql
    assert "SUM(total_tip_amount) / NULLIF(SUM(total_fare_amount), 0)" in sql
    files = re.findall(r"pickup_date=(\d{4}-\d{2}-\d{2})/rollup-\d+\.parquet", sql)
    assert files == [f"2022-12-{d:02d}" for d in range(5, 13)]  # jawna lista plików migawki
    assert "pickup_date=*" not in sql
    assert sql.endswith("ORDER BY pickup_date ASC LIMIT 1000")


def test_success_without_telemetry_has_no_telemetry_key(
    client: TestClient, body: BodyFactory
) -> None:
    with_telemetry = client.post(QUERY, json=body()).json()
    response = client.post(QUERY, json=body(telemetry=False))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"status", "data"}
    assert "telemetry" not in response.text
    key = ("pickup_date", "vendor_id")
    assert sorted(payload["data"], key=lambda r: tuple(r[k] for k in key)) == sorted(
        with_telemetry["data"], key=lambda r: tuple(r[k] for k in key)
    )


def test_default_control_is_auto_without_telemetry(client: TestClient, body: BodyFactory) -> None:
    data = body()
    del data["control"]
    response = client.post(QUERY, json=data)
    assert response.status_code == 200
    assert "telemetry" not in response.json()


def test_without_telemetry_no_extended_measurements(
    client: TestClient, body: BodyFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``include_telemetry=false`` – serwis nie odczytuje zegara (brak narzutu pomiarowego)."""
    calls: list[int] = []

    def counting_clock() -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(service_module, "time", SimpleNamespace(perf_counter_ns=counting_clock))
    assert client.post(QUERY, json=body(telemetry=False)).status_code == 200
    assert calls == []
    assert client.post(QUERY, json=body(telemetry=True)).status_code == 200
    assert len(calls) == 5


def test_force_duckdb_success(client: TestClient, body: BodyFactory) -> None:
    response = client.post(QUERY, json=body(mode="force_duckdb"))
    assert response.status_code == 200
    telemetry = response.json()["telemetry"]
    assert telemetry["engine_used"] == "duckdb"
    assert telemetry["routing_mode"] == "force_duckdb"
    assert telemetry["routing_reason"].startswith("Forced by execution_mode 'force_duckdb'")


def test_null_values_and_empty_groups(client: TestClient, body: BodyFactory) -> None:
    """Grupa z zerową sumą opłat (payment_type 3) -> ``null`` w JSON (NULLIF), nie błąd."""
    data = body(
        dimensions=["payment_type"],
        metrics=["trips_count", "tip_to_fare_ratio_pct"],
        filters=COVERED_FILTERS,
        order_by=[{"field": "payment_type", "direction": "ASC"}],
    )
    rows = client.post(QUERY, json=data).json()["data"]
    by_type = {row["payment_type"]: row for row in rows}
    assert by_type["3"]["tip_to_fare_ratio_pct"] is None
    assert by_type["3"]["trips_count"] > 0


def test_no_dimensions_single_row(client: TestClient, body: BodyFactory) -> None:
    data = body(dimensions=[], order_by=[], filters=COVERED_FILTERS)
    rows = client.post(QUERY, json=data).json()["data"]
    assert len(rows) == 1
    assert set(rows[0]) == {
        "trips_count",
        "total_revenue",
        "tip_to_fare_ratio_pct",
        "avg_trip_speed_mph",
    }


# --------------------------------------------------------------------------- #
# Błędy walidacji -> 400
# --------------------------------------------------------------------------- #

VALIDATION_CASES: list[tuple[dict[str, Any], str, str]] = [
    ({"model": "nyc_yellow_taxi"}, "UnknownModelError", "model"),
    ({"dimensions": ["vendor"]}, "UnknownDimensionError", "dimensions[0]"),
    ({"metrics": ["trip_count"]}, "UnknownMetricError", "metrics[0]"),
    ({"metrics": []}, "EmptyMetricsError", "metrics"),
    ({"metrics": ["trips_count", "trips_count"]}, "DuplicateFieldError", "metrics[1]"),
    ({"limit": 0}, "InvalidLimitError", "limit"),
    (
        {"filters": [{"field": "trips_count", "op": ">", "value": 1}]},
        "FilterOnMetricError",
        "filters[0].field",
    ),
    (
        {"filters": [{"field": "vendor_id", "op": "LIKE", "value": "1"}]},
        "UnsupportedOperatorError",
        "filters[0].op",
    ),
    (
        {"filters": [{"field": "pickup_date", "op": ">=", "value": "01.12.2022"}]},
        "InvalidFilterValueError",
        "filters[0].value",
    ),
    (
        {"filters": [{"field": "vendor_id", "op": "in", "value": "1"}]},
        "InvalidFilterValueError",
        "filters[0].value",
    ),
    (
        {
            "filters": [
                {"field": "pickup_date", "op": ">=", "value": "2022-12-10"},
                {"field": "pickup_date", "op": "<=", "value": "2022-12-01"},
            ]
        },
        "EmptyDateRangeError",
        "filters",
    ),
    (
        {"order_by": [{"field": "rate_code", "direction": "ASC"}]},
        "OrderByFieldNotSelectedError",
        "order_by[0].field",
    ),
]


@pytest.mark.parametrize("mode", ["auto", "force_duckdb", "force_bigquery"])
@pytest.mark.parametrize(
    ("overrides", "error_type", "field"),
    VALIDATION_CASES,
    ids=[f"{case[1]}-{case[2]}" for case in VALIDATION_CASES],
)
def test_validation_errors(
    client: TestClient,
    body: BodyFactory,
    overrides: dict[str, Any],
    error_type: str,
    field: str,
    mode: str,
) -> None:
    """Walidacja poprzedza routing – ten sam błąd 400 w każdym trybie."""
    payload = _assert_error(client.post(QUERY, json=body(mode=mode, **overrides)), 400, error_type)
    assert payload["details"]["field"].startswith(field)


def test_validation_error_has_suggestion(client: TestClient, body: BodyFactory) -> None:
    payload = _assert_error(
        client.post(QUERY, json=body(metrics=["trip_count"])), 400, "UnknownMetricError"
    )
    assert payload["message"] == "unknown metric 'trip_count'; did you mean 'trips_count'?"


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown_key": 1},
        {"limit": "10"},
        {"metrics": "trips_count"},
        {"filters": [{"field": "vendor_id", "op": "==", "value": True}]},
        {"control": {"execution_mode": "force_sqlite"}},
    ],
)
def test_malformed_request(
    client: TestClient, body: BodyFactory, overrides: dict[str, Any]
) -> None:
    payload = _assert_error(
        client.post(QUERY, json=body(**overrides)), 400, "RequestValidationError"
    )
    assert payload["details"]["errors"]
    assert all({"loc", "msg", "type"} == set(err) for err in payload["details"]["errors"])


def test_invalid_json(client: TestClient) -> None:
    response = client.post(
        QUERY, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _assert_error(response, 400, "RequestValidationError")


def test_unknown_path_and_method(client: TestClient) -> None:
    _assert_error(client.get("/api/v1/nope"), 404, "NotFound")
    _assert_error(client.get(QUERY), 405, "MethodNotAllowed")


# --------------------------------------------------------------------------- #
# force_duckdb: Warunki 1–3 -> 400
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        {"dimensions": ["pickup_date", "dropoff_location_id"]},
        {
            "filters": [
                *COVERED_FILTERS,
                {"field": "dropoff_location_id", "op": "==", "value": "132"},
            ]
        },
        {
            "dimensions": ["pickup_date", "dropoff_location_id"],
            "order_by": [{"field": "dropoff_location_id", "direction": "DESC"}],
        },
    ],
    ids=["dimensions", "filters", "order_by"],
)
def test_force_duckdb_granularity(
    client: TestClient, body: BodyFactory, overrides: dict[str, Any]
) -> None:
    payload = _assert_error(
        client.post(QUERY, json=body(mode="force_duckdb", **overrides)),
        400,
        "GranularityNotSupportedInLocalCacheError",
    )
    assert payload["message"] == "Dimension 'dropoff_location_id' not present in local rollup"
    assert payload["details"] == {"dimensions": ["dropoff_location_id"]}


def test_force_duckdb_range_with_gap(client: TestClient, body: BodyFactory) -> None:
    payload = _assert_error(
        client.post(QUERY, json=body(mode="force_duckdb", filters=GAP_FILTERS)),
        400,
        "CacheMissInForcedModeError",
    )
    assert payload["details"] == {
        "date_range": ["2022-12-03", "2022-12-05"],
        "missing_days": ["2022-12-04"],
        "missing_day_count": 1,
    }
    assert "2022-12-04" in payload["message"]


def test_force_duckdb_range_beyond_cache(client: TestClient, body: BodyFactory) -> None:
    filters = [
        {"field": "pickup_date", "op": ">=", "value": "2022-12-10"},
        {"field": "pickup_date", "op": "<", "value": "2022-12-16"},
    ]
    payload = _assert_error(
        client.post(QUERY, json=body(mode="force_duckdb", filters=filters)),
        400,
        "CacheMissInForcedModeError",
    )
    assert payload["details"]["missing_days"] == ["2022-12-13", "2022-12-14", "2022-12-15"]


def test_force_duckdb_unbounded_range(client: TestClient, body: BodyFactory) -> None:
    filters = [{"field": "pickup_date", "op": ">=", "value": "2022-12-05"}]
    payload = _assert_error(
        client.post(QUERY, json=body(mode="force_duckdb", filters=filters)),
        400,
        "CacheMissInForcedModeError",
    )
    assert payload["details"]["date_range"] is None
    assert "not bounded" in payload["message"]


def test_force_duckdb_holistic_measure(client: TestClient, body: BodyFactory) -> None:
    payload = _assert_error(
        client.post(
            QUERY, json=body(mode="force_duckdb", metrics=["trips_count", "unique_pickup_zones"])
        ),
        400,
        "NonAdditiveMeasureInLocalCacheError",
    )
    assert payload["details"] == {"measures": ["unique_pickup_zones"]}
    assert "COUNT_DISTINCT" in payload["message"]


# --------------------------------------------------------------------------- #
# BigQuery (faza 2) -> 501
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "reason", "condition"),
    [
        (
            {"dimensions": ["pickup_date", "dropoff_location_id"]},
            "Dimension 'dropoff_location_id' not present in local rollup",
            "granularity",
        ),
        ({"filters": GAP_FILTERS}, "Local cache is missing 1 day(s)", "time_coverage"),
        (
            {"metrics": ["unique_pickup_zones"]},
            "Measure 'unique_pickup_zones' (COUNT_DISTINCT) cannot be re-aggregated",
            "additivity",
        ),
        ({"filters": []}, "not bounded on both sides", "time_coverage"),
    ],
    ids=["granularity", "gap", "holistic", "unbounded"],
)
def test_auto_ineligible_returns_501_with_reason(
    client: TestClient, body: BodyFactory, overrides: dict[str, Any], reason: str, condition: str
) -> None:
    payload = _assert_error(
        client.post(QUERY, json=body(**overrides)), 501, "BigQueryNotAvailableYet"
    )
    details = payload["details"]
    assert details["engine"] == "bigquery"
    assert details["routing_mode"] == "auto"
    assert details["routing_reason"].startswith("Full fallback to BigQuery: ")
    assert reason in details["routing_reason"]
    assert details["conditions"][condition]["satisfied"] is False


def test_auto_gap_lists_missing_days(client: TestClient, body: BodyFactory) -> None:
    payload = client.post(QUERY, json=body(filters=GAP_FILTERS)).json()
    assert payload["details"]["missing_days"] == ["2022-12-04"]


def test_force_bigquery_returns_501(client: TestClient, body: BodyFactory) -> None:
    payload = _assert_error(
        client.post(QUERY, json=body(mode="force_bigquery")), 501, "BigQueryNotAvailableYet"
    )
    assert payload["details"] == {
        "engine": "bigquery",
        "routing_mode": "force_bigquery",
        "routing_reason": "Forced by execution_mode 'force_bigquery'.",
    }


# --------------------------------------------------------------------------- #
# 500 bez wycieku szczegółów
# --------------------------------------------------------------------------- #


class _FailingExecutor(QueryExecutor):
    engine = "duckdb"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, sql: str) -> ExecutionResult:
        raise self.error


@pytest.fixture
def failing_client(settings: Settings) -> Iterator[Callable[[Exception], TestClient]]:
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:

        def use(error: Exception) -> TestClient:
            service: QueryService = app.state.service
            app.state.service = QueryService(
                service.models, service.indexes, _FailingExecutor(error)
            )
            return test_client

        yield use


def test_unhandled_error_is_500_without_traceback(
    failing_client: Callable[[Exception], TestClient], body: BodyFactory
) -> None:
    client = failing_client(RuntimeError("secret internal detail"))
    response = client.post(QUERY, json=body())
    payload = _assert_error(response, 500, "InternalServerError")
    assert payload["message"] == "Internal server error"
    assert payload["details"] == {}
    assert "secret" not in response.text
    assert "Traceback" not in response.text


def test_engine_error_is_500_without_sql(
    failing_client: Callable[[Exception], TestClient], body: BodyFactory
) -> None:
    error = QueryExecutionError("duckdb", "SELECT secret_sql", ValueError("boom"))
    client = failing_client(error)
    response = client.post(QUERY, json=body())
    payload = _assert_error(response, 500, "QueryExecutionError")
    assert payload["message"] == "Query execution failed in engine 'duckdb'"
    assert "secret_sql" not in response.text


def test_corrupted_manifest_keeps_last_snapshot(
    tmp_path: Path, data_root: Path, body: BodyFactory
) -> None:
    """Uszkodzony ``manifest.json`` po starcie: zapytania korzystają z ostatniej poprawnej
    migawki, ``/health`` raportuje ``degraded``."""
    root = tmp_path / "data"
    shutil.copytree(data_root, root)
    with TestClient(create_app(Settings(config_dir=CONFIG_DIR, data_root=root))) as client:
        (root / "nyc_taxi" / "manifest.json").write_text("{broken", encoding="utf-8")
        response = client.post(QUERY, json=body(mode="force_duckdb"))
        assert response.status_code == 200
        assert response.json()["data"]
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        cache = health["models"]["nyc_yellow_taxi_analytics"]
        assert cache["status"] == "degraded"
        assert cache["partitions"] == 11
        assert cache["error"].startswith("manifest error")


@pytest.mark.parametrize(
    ("error", "error_type", "message"),
    [
        (
            NonAdditiveMeasureError("m", "COUNT_DISTINCT"),
            "NonAdditiveMeasureError",
            "SQL compilation failed",
        ),
        (
            ConfigValidationError("cfg.yaml", [ConfigIssue("a.b", "broken")]),
            "ConfigValidationError",
            "Semantic model configuration is invalid",
        ),
    ],
)
def test_internal_domain_errors_are_500(
    failing_client: Callable[[Exception], TestClient],
    body: BodyFactory,
    error: Exception,
    error_type: str,
    message: str,
) -> None:
    payload = _assert_error(failing_client(error).post(QUERY, json=body()), 500, error_type)
    assert payload["message"] == message


def test_config_error_details(
    failing_client: Callable[[Exception], TestClient], body: BodyFactory
) -> None:
    error = ConfigValidationError("cfg.yaml", [ConfigIssue("a.b", "broken")])
    payload = failing_client(error).post(QUERY, json=body()).json()
    assert payload["details"] == {"source": "cfg.yaml", "issues": ["a.b: broken"]}


def test_http_error_with_nonstandard_status() -> None:
    from starlette.requests import Request

    from app.api.errors import _http

    exc = StarletteHTTPException(599, "odd", headers={"X-Test": "1"})
    response = asyncio.run(_http(Request({"type": "http", "method": "GET", "path": "/"}), exc))
    assert response.status_code == 599
    assert json.loads(bytes(response.body)) == {
        "status": "error",
        "error_type": "HttpError",
        "message": "odd",
        "details": {},
    }
    assert response.headers["X-Test"] == "1"
