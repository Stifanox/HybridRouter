"""``GoogleBigQueryGateway`` z obiektem zastępczym klienta (F2-11, warstwa 2).

Żaden test nie łączy się z GCP: klient ``bigquery.Client`` jest zastąpiony ``MagicMock``,
a sprawdzane są parametry zadań, anulowanie po przekroczeniu limitu czasu i mapowanie
wyjątków ``google.api_core`` / ``google.auth`` na wyjątki bramki.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import bigquery

from app.warehouse import google_gateway
from app.warehouse.gateway import (
    WarehouseQueryError,
    WarehouseTimeoutError,
    WarehouseUnavailableError,
)
from app.warehouse.google_gateway import GoogleBigQueryGateway

SQL = "SELECT 1 AS x"
T0 = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.UTC)
RESULT = pa.table({"x": [1]})


def make_job(**overrides: Any) -> MagicMock:
    job = MagicMock(spec=bigquery.QueryJob)
    job.job_id = "job_123"
    job.total_bytes_processed = 1000
    job.total_bytes_billed = 10 * 1024**2
    job.cache_hit = False
    job.created = T0
    job.started = T0 + dt.timedelta(milliseconds=30)
    job.ended = T0 + dt.timedelta(milliseconds=530)
    rows = MagicMock()
    rows.to_arrow.return_value = RESULT
    job.result.return_value = rows
    for name, value in overrides.items():
        setattr(job, name, value)
    return job


def make_gateway(job: MagicMock | None = None, **kwargs: Any) -> tuple[GoogleBigQueryGateway, Any]:
    client = MagicMock(spec=bigquery.Client)
    client.query.return_value = job if job is not None else make_job()
    return GoogleBigQueryGateway("proj", client=client, **kwargs), client


def run(gateway: GoogleBigQueryGateway, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "location": "US",
        "maximum_bytes_billed": 5_000_000_000,
        "use_query_cache": False,
        "timeout_s": 30.0,
    }
    kwargs.update(overrides)
    return gateway.run(SQL, **kwargs)


# --------------------------------------------------------------------------- #
# Klient
# --------------------------------------------------------------------------- #


def test_client_created_with_project(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = MagicMock()
    monkeypatch.setattr(google_gateway.bigquery, "Client", factory)
    gateway = GoogleBigQueryGateway("my-project")
    factory.assert_called_once_with(project="my-project")
    gateway.close()
    factory.return_value.close.assert_called_once_with()


@pytest.mark.parametrize(
    "error",
    [auth_exceptions.DefaultCredentialsError("no ADC"), OSError("credentials file missing")],
)
def test_missing_credentials_make_gateway_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(google_gateway.bigquery, "Client", MagicMock(side_effect=error))
    with pytest.raises(WarehouseUnavailableError, match="Application Default Credentials"):
        GoogleBigQueryGateway("my-project")


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #


def test_dry_run_job_config_and_location() -> None:
    gateway, client = make_gateway(make_job(total_bytes_processed=987_654))
    result = gateway.dry_run(SQL, location="EU")

    assert result.bytes_processed == 987_654
    assert result.elapsed_ns >= 0
    (sql,), kwargs = client.query.call_args
    assert sql == SQL
    assert kwargs["location"] == "EU"
    config = kwargs["job_config"]
    assert config.dry_run is True
    assert config.use_query_cache is False
    assert config.maximum_bytes_billed is None


def test_dry_run_without_bytes_reports_zero() -> None:
    gateway, _ = make_gateway(make_job(total_bytes_processed=None))
    assert gateway.dry_run(SQL, location="US").bytes_processed == 0


# --------------------------------------------------------------------------- #
# Wykonanie
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("use_cache", "max_billed"), [(False, 123_456_789), (True, None)])
def test_run_job_config(use_cache: bool, max_billed: int | None) -> None:
    job = make_job()
    gateway, client = make_gateway(job)
    run(gateway, use_query_cache=use_cache, maximum_bytes_billed=max_billed, timeout_s=12.5)

    kwargs = client.query.call_args.kwargs
    assert kwargs["location"] == "US"
    config = kwargs["job_config"]
    assert not config.dry_run
    assert config.use_query_cache is use_cache
    assert config.maximum_bytes_billed == max_billed
    assert (
        int(config.job_timeout_ms) == 12_500
    )  # biblioteka przechowuje int64 jako napis (JSON API)
    job.result.assert_called_once_with(timeout=12.5)


def test_run_returns_table_and_server_stats() -> None:
    job = make_job()
    gateway, _ = make_gateway(job)
    result = run(gateway)

    assert result.table is RESULT
    stats = result.stats
    assert stats.job_id == "job_123"
    assert (stats.bytes_processed, stats.bytes_billed, stats.cache_hit) == (
        1000,
        10 * 1024**2,
        False,
    )
    assert (stats.created, stats.started, stats.ended) == (job.created, job.started, job.ended)
    assert stats.queue_ms == 30.0
    assert stats.server_execution_ms == 500.0
    assert stats.server_total_ms == 530.0
    assert stats.result_fetch_ns >= 0


def test_cache_hit_has_no_billed_bytes() -> None:
    gateway, _ = make_gateway(make_job(cache_hit=True, total_bytes_billed=None))
    stats = run(gateway, use_query_cache=True).stats
    assert stats.cache_hit is True
    assert stats.bytes_billed == 0


def test_naive_server_timestamps_are_treated_as_utc() -> None:
    naive = T0.replace(tzinfo=None)
    gateway, _ = make_gateway(make_job(created=naive, started=naive, ended=naive))
    assert run(gateway).stats.created.tzinfo is dt.UTC


def test_missing_server_timestamps_are_an_error() -> None:
    gateway, _ = make_gateway(make_job(ended=None))
    with pytest.raises(WarehouseQueryError, match="timestamps"):
        run(gateway)


@pytest.mark.parametrize("storage_api", [False, True])
def test_result_fetch_uses_rest_by_default(storage_api: bool) -> None:
    job = make_job()
    gateway, _ = make_gateway(job, use_storage_api=storage_api)
    run(gateway)
    job.result.return_value.to_arrow.assert_called_once_with(create_bqstorage_client=storage_api)


# --------------------------------------------------------------------------- #
# Limit czasu
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "timeout_error",
    [concurrent.futures.TimeoutError(), api_exceptions.DeadlineExceeded("deadline")],
)
def test_timeout_cancels_job(timeout_error: Exception) -> None:
    job = make_job()
    job.result.side_effect = timeout_error
    job.cancel.return_value = True
    gateway, _ = make_gateway(job)

    with pytest.raises(WarehouseTimeoutError) as exc_info:
        run(gateway, timeout_s=2.0)

    job.cancel.assert_called_once_with()
    error = exc_info.value
    assert (error.job_id, error.cancelled, error.timeout_s) == ("job_123", True, 2.0)
    job.result.return_value.to_arrow.assert_not_called()


def test_failed_cancel_is_reported() -> None:
    job = make_job()
    job.result.side_effect = concurrent.futures.TimeoutError()
    job.cancel.side_effect = api_exceptions.ServiceUnavailable("cannot cancel")
    gateway, _ = make_gateway(job)

    with pytest.raises(WarehouseTimeoutError) as exc_info:
        run(gateway)
    assert exc_info.value.cancelled is False
    assert "NOT cancelled" in str(exc_info.value)


def test_server_side_job_timeout_maps_to_timeout() -> None:
    job = make_job()
    job.result.side_effect = api_exceptions.BadRequest(
        "Job timed out", errors=[{"reason": "timeout", "message": "Job timed out"}]
    )
    gateway, _ = make_gateway(job)

    with pytest.raises(WarehouseTimeoutError) as exc_info:
        run(gateway)
    assert exc_info.value.cancelled is True
    job.cancel.assert_not_called()


# --------------------------------------------------------------------------- #
# Mapowanie wyjątków
# --------------------------------------------------------------------------- #

API_ERRORS = [
    pytest.param(
        api_exceptions.BadRequest(
            "Syntax error: Unexpected keyword", errors=[{"reason": "invalidQuery"}]
        ),
        400,
        "invalidQuery",
        id="bad-request",
    ),
    pytest.param(
        api_exceptions.Forbidden("Access Denied", errors=[{"reason": "accessDenied"}]),
        403,
        "accessDenied",
        id="forbidden",
    ),
    pytest.param(
        api_exceptions.NotFound("Not found: Table x", errors=[{"reason": "notFound"}]),
        404,
        "notFound",
        id="not-found",
    ),
    pytest.param(
        api_exceptions.InternalServerError("backend error", errors=["not a dict"]),
        500,
        None,
        id="internal",
    ),
    pytest.param(api_exceptions.ServiceUnavailable("try later"), 503, None, id="unavailable"),
]


@pytest.mark.parametrize(("error", "status", "reason"), API_ERRORS)
def test_dry_run_api_errors_map_to_query_error(
    error: Exception, status: int, reason: str | None
) -> None:
    gateway, client = make_gateway()
    client.query.side_effect = error

    with pytest.raises(WarehouseQueryError) as exc_info:
        gateway.dry_run(SQL, location="US")

    mapped = exc_info.value
    assert (mapped.status_code, mapped.reason, mapped.sql) == (status, reason, SQL)
    assert mapped.message == error.message  # type: ignore[attr-defined]
    assert mapped.__cause__ is error


@pytest.mark.parametrize(("error", "status", "reason"), API_ERRORS)
def test_job_errors_map_to_query_error_with_job_id(
    error: Exception, status: int, reason: str | None
) -> None:
    job = make_job()
    job.result.side_effect = error
    gateway, _ = make_gateway(job)

    with pytest.raises(WarehouseQueryError) as exc_info:
        run(gateway)
    assert (exc_info.value.status_code, exc_info.value.job_id) == (status, "job_123")


def test_bytes_billed_limit_exceeded_is_a_query_error() -> None:
    job = make_job()
    job.result.side_effect = api_exceptions.BadRequest(
        "Query exceeded limit for bytes billed: 10485760. 20971520 or higher required.",
        errors=[{"reason": "bytesBilledLimitExceeded"}],
    )
    gateway, _ = make_gateway(job)

    with pytest.raises(WarehouseQueryError) as exc_info:
        run(gateway)
    assert exc_info.value.reason == "bytesBilledLimitExceeded"
    assert "bytes billed" in str(exc_info.value)


@pytest.mark.parametrize(
    "error",
    [
        api_exceptions.Unauthenticated("token expired"),
        auth_exceptions.RefreshError("refresh failed"),
        auth_exceptions.DefaultCredentialsError("no ADC"),
        api_exceptions.RetryError("Deadline of 600s exceeded", cause=ConnectionError("down")),
    ],
)
def test_auth_and_connectivity_errors_map_to_unavailable(error: Exception) -> None:
    gateway, client = make_gateway()
    client.query.side_effect = error

    with pytest.raises(WarehouseUnavailableError):
        gateway.dry_run(SQL, location="US")
    with pytest.raises(WarehouseUnavailableError):
        run(gateway)


def test_unrelated_exceptions_are_not_swallowed() -> None:
    gateway, client = make_gateway()
    client.query.side_effect = RuntimeError("bug")
    with pytest.raises(RuntimeError, match="bug"):
        gateway.dry_run(SQL, location="US")
