"""``BigQueryExecutor`` na atrapie: dry-run -> strażnik kosztów -> wykonanie (Moduł 3)."""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from app.config.loader import load_model_dict
from app.config.schemas import ModelConfig
from app.executors import bigquery_executor
from app.executors.base import ExecutionOptions
from app.executors.bigquery_executor import BigQueryExecutor
from app.planner.builder import plan_query
from app.renderers.raw_table import RawTableRenderer
from app.semantic.resolver import SemanticModel
from app.warehouse.cost_guard import (
    BYTES_PER_TIB,
    MIN_BYTES_BILLED,
    CostConfirmationRequiredException,
    CostTooHighForQueryException,
)
from app.warehouse.fake_gateway import BytesEstimator, FakeBigQueryGateway
from app.warehouse.gateway import (
    DryRunResult,
    JobStats,
    WarehouseQueryError,
    WarehouseRunResult,
    WarehouseTimeoutError,
)
from tests.conftest import make_request

GIB = 1024**3
SMALL = 1024**2  # 1 MiB – poniżej limitu 10 GiB
LARGE = 20 * GIB  # powyżej limitu 10 GiB


def model_config(raw: dict[str, Any], **cost_guard: Any) -> ModelConfig:
    raw["cost_guard"].update(cost_guard)
    return load_model_dict(raw)


@pytest.fixture
def make_executor(nyc_raw: dict[str, Any], synthetic_raw: pa.Table) -> Iterator[Any]:
    gateways: list[FakeBigQueryGateway] = []

    def build(
        bytes_estimator: BytesEstimator | None = SMALL,
        *,
        delay_s: float = 0.0,
        timeout_s: float | None = None,
        **cost_guard: Any,
    ) -> tuple[BigQueryExecutor, FakeBigQueryGateway]:
        raw = nyc_raw
        if timeout_s is not None:
            raw["storage"]["bigquery"]["job_timeout_s"] = timeout_s
        config = model_config(raw, **cost_guard)
        gateway = FakeBigQueryGateway(
            synthetic_raw,
            dataset_table=config.storage.bigquery.dataset_table,
            bytes_estimator=bytes_estimator,
            execution_delay_s=delay_s,
        )
        gateways.append(gateway)
        return BigQueryExecutor(gateway, config), gateway

    yield build
    for gateway in gateways:
        gateway.close()


@pytest.fixture
def sql(nyc_model: SemanticModel) -> str:
    plan = plan_query(make_request(), nyc_model)
    return RawTableRenderer(nyc_model.config.storage.bigquery.dataset_table).render(
        plan, "bigquery"
    )


def kinds(gateway: FakeBigQueryGateway) -> list[str]:
    return [call.kind for call in gateway.calls]


# --------------------------------------------------------------------------- #
# Akcje strażnika
# --------------------------------------------------------------------------- #


def test_allow(make_executor: Any, sql: str) -> None:
    executor, gateway = make_executor(SMALL)
    result = executor.execute(sql)

    assert result.engine == executor.engine == "bigquery"
    assert result.sql == sql
    assert result.row_count > 0
    assert set(result.columns) >= {"pickup_date", "vendor_id", "trips_count"}
    assert kinds(gateway) == ["dry_run", "run"]
    run_call = gateway.calls[-1]
    assert run_call.location == "US"
    assert run_call.maximum_bytes_billed == 10 * GIB
    assert run_call.use_query_cache is False
    assert run_call.timeout_s == 120

    stats = result.warehouse
    assert stats is not None
    assert stats.engine == "bigquery"
    assert stats.job_id.startswith("fake_job_")
    assert (stats.cost_action, stats.limit_exceeded) == ("allow", False)
    assert stats.estimated_bytes == SMALL
    assert stats.estimated_cost_usd == pytest.approx(SMALL / BYTES_PER_TIB * 6.25)
    assert stats.maximum_bytes_billed == 10 * GIB
    assert stats.bytes_processed == SMALL
    assert stats.bytes_billed == MIN_BYTES_BILLED
    # Koszt rzeczywisty z bytes_billed (minimum 10 MB) > koszt szacowany.
    assert stats.actual_cost_usd == pytest.approx(MIN_BYTES_BILLED / BYTES_PER_TIB * 6.25)
    assert stats.actual_cost_usd > stats.estimated_cost_usd
    assert stats.cache_hit is False
    assert stats.cost_estimate_time_ms is not None and stats.cost_estimate_time_ms >= 0


@pytest.mark.parametrize("confirm", [False, True])
def test_reject_ignores_confirmation(make_executor: Any, sql: str, confirm: bool) -> None:
    executor, gateway = make_executor(LARGE, action_on_exceed="raise_error")
    with pytest.raises(CostTooHighForQueryException) as exc_info:
        executor.execute(sql, ExecutionOptions(confirm_high_cost=confirm))

    error = exc_info.value
    assert (error.bytes_processed, error.sql) == (LARGE, sql)
    assert error.estimated_cost_usd == pytest.approx(LARGE / BYTES_PER_TIB * 6.25)
    assert error.limits == {"max_bytes_per_query": 10 * GIB, "max_cost_usd_per_query": 0.0625}
    assert kinds(gateway) == ["dry_run"]  # zapytanie nie zostało wykonane


def test_require_confirmation_then_confirmed(make_executor: Any, sql: str) -> None:
    executor, gateway = make_executor(LARGE, action_on_exceed="require_confirmation")

    with pytest.raises(CostConfirmationRequiredException) as exc_info:
        executor.execute(sql)
    assert (exc_info.value.bytes_processed, exc_info.value.sql) == (LARGE, sql)
    assert exc_info.value.exceeded_limits == ("max_bytes_per_query", "max_cost_usd_per_query")
    assert kinds(gateway) == ["dry_run"]

    result = executor.execute(sql, ExecutionOptions(confirm_high_cost=True))
    # Ponowienie wykonuje dry-run ponownie (bez buforowania, F2-9).
    assert kinds(gateway) == ["dry_run", "dry_run", "run"]
    expected_limit = math.ceil(LARGE * 1.1)
    assert gateway.calls[-1].maximum_bytes_billed == expected_limit
    assert result.warehouse is not None
    assert result.warehouse.cost_action == "allow_over_limit"
    assert result.warehouse.limit_exceeded is True
    assert result.warehouse.maximum_bytes_billed == expected_limit


def test_confirmation_below_limit_keeps_hard_limit(make_executor: Any, sql: str) -> None:
    executor, gateway = make_executor(SMALL)
    executor.execute(sql, ExecutionOptions(confirm_high_cost=True))
    assert gateway.calls[-1].maximum_bytes_billed == 10 * GIB


def test_silent_allow_logs_warning(
    make_executor: Any, sql: str, caplog: pytest.LogCaptureFixture
) -> None:
    executor, gateway = make_executor(LARGE, action_on_exceed="silent_allow")
    with caplog.at_level(logging.WARNING, logger=bigquery_executor.__name__):
        result = executor.execute(sql)

    assert result.row_count > 0
    assert kinds(gateway) == ["dry_run", "run"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "silent_allow" in message
    assert f"bytes_processed={LARGE}" in message
    assert f"estimated_cost_usd={LARGE / BYTES_PER_TIB * 6.25:.6f}" in message
    assert result.warehouse is not None and result.warehouse.limit_exceeded is True


def test_confirmed_over_limit_is_logged_at_info(
    make_executor: Any, sql: str, caplog: pytest.LogCaptureFixture
) -> None:
    executor, _ = make_executor(LARGE)
    with caplog.at_level(logging.INFO, logger=bigquery_executor.__name__):
        executor.execute(sql, ExecutionOptions(confirm_high_cost=True))
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert "confirmed by client" in caplog.records[0].getMessage()


def test_silent_allow_below_limit_does_not_log(
    make_executor: Any, sql: str, caplog: pytest.LogCaptureFixture
) -> None:
    executor, _ = make_executor(SMALL, action_on_exceed="silent_allow")
    with caplog.at_level(logging.DEBUG, logger=bigquery_executor.__name__):
        executor.execute(sql)
    assert caplog.records == []


@pytest.mark.parametrize("action", ["raise_error", "require_confirmation", "silent_allow"])
def test_disabled_guard_skips_dry_run_and_limit(make_executor: Any, sql: str, action: str) -> None:
    executor, gateway = make_executor(LARGE, enabled=False, action_on_exceed=action)
    result = executor.execute(sql)

    assert kinds(gateway) == ["run"]
    assert gateway.calls[0].maximum_bytes_billed is None
    stats = result.warehouse
    assert stats is not None
    assert stats.cost_action == "allow"
    assert stats.estimated_bytes is None
    assert stats.estimated_cost_usd is None
    assert stats.cost_estimate_time_ms is None
    assert stats.maximum_bytes_billed is None
    assert stats.bytes_processed == LARGE


# --------------------------------------------------------------------------- #
# Błędy bramki
# --------------------------------------------------------------------------- #


def test_timeout_cancels_job(make_executor: Any, sql: str) -> None:
    executor, gateway = make_executor(SMALL, delay_s=10, timeout_s=0.05)
    with pytest.raises(WarehouseTimeoutError) as exc_info:
        executor.execute(sql)
    assert exc_info.value.cancelled is True
    assert gateway.cancelled_jobs == [exc_info.value.job_id]
    assert gateway.calls[-1].timeout_s == 0.05


def test_warehouse_errors_propagate(make_executor: Any) -> None:
    executor, gateway = make_executor(None)
    with pytest.raises(WarehouseQueryError, match="Not found"):
        executor.execute("SELECT 1 FROM project.dataset.unknown")
    assert kinds(gateway) == ["dry_run"]


def test_max_bytes_billed_violation_is_a_warehouse_error(make_executor: Any, sql: str) -> None:
    # Dry-run zaniżony względem wykonania: twardy limit zadania chroni budżet.
    estimates = iter([SMALL, 11 * GIB])
    executor, _ = make_executor(lambda _sql: next(estimates))
    with pytest.raises(WarehouseQueryError) as exc_info:
        executor.execute(sql)
    assert exc_info.value.reason == "bytesBilledLimitExceeded"


# --------------------------------------------------------------------------- #
# estimate() – szacunek bez wykonania (/api/v1/dry-run, B2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("bytes_processed", "action", "exceeded", "expected_action"),
    [
        (SMALL, "raise_error", False, "allow"),
        (LARGE, "raise_error", True, "reject"),
        (LARGE, "require_confirmation", True, "require_confirmation"),
        (LARGE, "silent_allow", True, "allow_over_limit"),
    ],
)
def test_estimate_never_raises_or_runs(
    make_executor: Any,
    sql: str,
    bytes_processed: int,
    action: str,
    exceeded: bool,
    expected_action: str,
) -> None:
    executor, gateway = make_executor(bytes_processed, action_on_exceed=action)
    estimate = executor.estimate(sql)

    assert kinds(gateway) == ["dry_run"]
    assert estimate.sql == sql
    assert estimate.bytes_processed == bytes_processed
    assert estimate.estimated_cost_usd == pytest.approx(bytes_processed / BYTES_PER_TIB * 6.25)
    assert estimate.would_exceed_limit is exceeded
    assert estimate.action == expected_action
    assert estimate.cost_estimate_time_ms >= 0


def test_estimate_with_disabled_guard_still_dry_runs(make_executor: Any, sql: str) -> None:
    executor, gateway = make_executor(LARGE, enabled=False)
    estimate = executor.estimate(sql)
    assert kinds(gateway) == ["dry_run"]
    assert estimate.action == "allow"
    assert estimate.would_exceed_limit is True  # informacyjnie: limit przekroczony
    assert estimate.decision.maximum_bytes_billed is None


# --------------------------------------------------------------------------- #
# Rozkład czasu (F2-7)
# --------------------------------------------------------------------------- #

T0 = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)


class _StubGateway:
    """Bramka o znanych znacznikach serwera: kolejka 5 ms, wykonanie 100 ms, pobranie 7 ms."""

    def dry_run(self, sql: str, *, location: str) -> DryRunResult:
        return DryRunResult(bytes_processed=SMALL, elapsed_ns=180_400_000)

    def run(self, sql: str, **_: Any) -> WarehouseRunResult:
        stats = JobStats(
            job_id="job_x",
            bytes_processed=SMALL,
            bytes_billed=MIN_BYTES_BILLED,
            cache_hit=False,
            created=T0,
            started=T0 + dt.timedelta(milliseconds=5),
            ended=T0 + dt.timedelta(milliseconds=105),
            result_fetch_ns=7_000_000,
        )
        return WarehouseRunResult(pa.table({"x": [1]}), stats)

    def close(self) -> None:
        pass


def test_time_breakdown(monkeypatch: pytest.MonkeyPatch, nyc_raw: dict[str, Any], sql: str) -> None:
    # Czas klienta wokół gateway.run: 0 ns -> 200 ms.
    clock = iter([0, 200_000_000])
    monkeypatch.setattr(
        bigquery_executor, "time", SimpleNamespace(perf_counter_ns=lambda: next(clock))
    )
    executor = BigQueryExecutor(_StubGateway(), model_config(nyc_raw))
    result = executor.execute(sql)

    assert result.execution_time_ns == 200_000_000
    stats = result.warehouse
    assert stats is not None
    assert stats.cost_estimate_time_ms == pytest.approx(180.4)
    assert stats.queue_ms == pytest.approx(5.0)
    assert stats.server_execution_ms == pytest.approx(100.0)
    assert stats.result_fetch_ms == pytest.approx(7.0)
    # 200 − (105 − 0) − 7 = 88 ms narzutu klienta i sieci.
    assert stats.client_overhead_ms == pytest.approx(88.0)


def test_time_breakdown_on_fake_is_consistent(make_executor: Any, sql: str) -> None:
    executor, _ = make_executor(SMALL, delay_s=0.02)
    result = executor.execute(sql)
    stats = result.warehouse
    assert stats is not None
    assert stats.queue_ms == 0
    assert stats.server_execution_ms >= 20
    total = stats.queue_ms + stats.server_execution_ms + stats.result_fetch_ms
    assert total + stats.client_overhead_ms == pytest.approx(result.execution_time_ms)
    assert stats.client_overhead_ms >= -1  # zaokrąglenie znaczników do mikrosekund


def test_close_does_not_close_shared_gateway(make_executor: Any, sql: str) -> None:
    executor, gateway = make_executor(SMALL)
    executor.close()
    gateway.dry_run(sql, location="US")  # bramka nadal działa
