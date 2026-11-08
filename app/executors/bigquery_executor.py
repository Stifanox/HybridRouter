"""Wykonawca BigQuery: dry-run -> strażnik kosztów -> wykonanie (ADR-006; dok. 03, Moduł 3).

Wykonawca nie importuje biblioteki Google – wywołania hurtowni idą przez
:class:`~app.warehouse.gateway.WarehouseGateway` (ADR-014), a decyzję kosztową podejmuje
czysta funkcja :func:`~app.warehouse.cost_guard.evaluate_cost`.

Przebieg :meth:`BigQueryExecutor.execute`:

1. ``cost_guard.enabled`` -> ``gateway.dry_run`` (bez buforowania – ponowienie po żądaniu
   potwierdzenia wykonuje dry-run ponownie, F2-9) -> ``evaluate_cost`` -> ``enforce``
   (wyjątek 403/409); ``silent_allow`` z przekroczeniem – ``logging.warning`` z bajtami
   i kosztem. Strażnik wyłączony -> brak dry-runu i limitu bajtów.
2. ``gateway.run`` z ``maximum_bytes_billed`` z decyzji, ``location``, ``use_query_cache``
   i ``job_timeout_s`` z YAML.
3. :class:`~app.executors.base.ExecutionResult` z :class:`~app.executors.base.WarehouseStats`
   (koszt szacowany i rzeczywisty, rozkład czasu F2-7).

Wyjątki bramki (``WarehouseQueryError``, ``WarehouseTimeoutError``,
``WarehouseUnavailableError``) i strażnika (``CostTooHighForQueryException``,
``CostConfirmationRequiredException``) są przekazywane bez opakowania – mapowanie na HTTP
502/504/503/403/409 należy do API (B2).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config.schemas import ModelConfig
from app.executors.base import (
    ExecutionOptions,
    ExecutionResult,
    QueryExecutor,
    WarehouseStats,
)
from app.warehouse.cost_guard import CostDecision, actual_cost_usd, enforce, evaluate_cost
from app.warehouse.gateway import DryRunResult, WarehouseGateway, WarehouseRunResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Szacunek kosztu bez wykonania (endpoint ``/api/v1/dry-run``, B2)."""

    sql: str
    dry_run: DryRunResult
    decision: CostDecision

    @property
    def bytes_processed(self) -> int:
        return self.dry_run.bytes_processed

    @property
    def estimated_cost_usd(self) -> float:
        assert self.decision.estimated_cost_usd is not None  # dry-run zawsze wykonany
        return self.decision.estimated_cost_usd

    @property
    def would_exceed_limit(self) -> bool:
        return self.decision.exceeded

    @property
    def action(self) -> str:
        return self.decision.action

    @property
    def cost_estimate_time_ms(self) -> float:
        return self.dry_run.elapsed_ms


class BigQueryExecutor(QueryExecutor):
    """Wykonawca zapytań w hurtowni (dialekt ``bigquery``, renderer ``RawTableRenderer``).

    Bezpieczny wątkowo, o ile bramka jest bezpieczna wątkowo (wykonawca nie ma stanu
    zmiennego). Bramka należy do wywołującego – :meth:`close` jej nie zamyka (jeden klient na
    proces, współdzielony np. z synchronizatorem; zamykany w ``lifespan``).
    """

    engine = "bigquery"

    def __init__(self, gateway: WarehouseGateway, config: ModelConfig) -> None:
        self.gateway = gateway
        self.config = config

    def estimate(self, sql: str) -> CostEstimate:
        """Dry-run + ``evaluate_cost`` bez wykonania i bez zgłaszania wyjątków strażnika
        (``confirmed=False``). Dry-run jest wykonywany także przy wyłączonym strażniku –
        szacunek ma charakter informacyjny.

        :raises WarehouseQueryError: hurtownia odrzuciła zapytanie (dry-run waliduje SQL).
        :raises WarehouseUnavailableError: brak klienta lub poświadczeń.
        """
        dry_run = self.gateway.dry_run(sql, location=self.config.storage.bigquery.location)
        decision = evaluate_cost(dry_run.bytes_processed, self.config.cost_guard, confirmed=False)
        return CostEstimate(sql, dry_run, decision)

    def execute(self, sql: str, options: ExecutionOptions | None = None) -> ExecutionResult:
        """Wykonuje zapytanie przez strażnika kosztów.

        :raises CostTooHighForQueryException: przekroczony limit, ``raise_error``.
        :raises CostConfirmationRequiredException: przekroczony limit,
            ``require_confirmation`` bez ``options.confirm_high_cost``.
        :raises WarehouseQueryError, WarehouseTimeoutError, WarehouseUnavailableError: błędy
            bramki.
        """
        opts = options or ExecutionOptions()
        bigquery = self.config.storage.bigquery
        guard = self.config.cost_guard

        dry_run: DryRunResult | None = None
        if guard.enabled:
            dry_run = self.gateway.dry_run(sql, location=bigquery.location)
            decision = evaluate_cost(
                dry_run.bytes_processed, guard, confirmed=opts.confirm_high_cost
            )
            enforce(decision, sql)
            self._log_over_limit(decision)
        else:
            decision = evaluate_cost(None, guard, confirmed=opts.confirm_high_cost)

        started = time.perf_counter_ns()
        run = self.gateway.run(
            sql,
            location=bigquery.location,
            maximum_bytes_billed=decision.maximum_bytes_billed,
            use_query_cache=bigquery.use_query_cache,
            timeout_s=bigquery.job_timeout_s,
        )
        client_ns = time.perf_counter_ns() - started
        stats = self._stats(decision, dry_run, run, client_ns)
        return ExecutionResult(self.engine, sql, run.table, client_ns, warehouse=stats)

    def _log_over_limit(self, decision: CostDecision) -> None:
        if decision.action != "allow_over_limit":
            return
        mode = self.config.cost_guard.action_on_exceed
        log = logger.warning if mode == "silent_allow" else logger.info
        log(
            "Cost guard limit exceeded (%s, model %s): bytes_processed=%s "
            "estimated_cost_usd=%.6f exceeded=%s limits=%s maximum_bytes_billed=%s",
            "silent_allow" if mode == "silent_allow" else "confirmed by client",
            self.config.model_name,
            decision.bytes_processed,
            decision.estimated_cost_usd,
            ",".join(decision.exceeded_limits),
            decision.limits,
            decision.maximum_bytes_billed,
        )

    def _stats(
        self,
        decision: CostDecision,
        dry_run: DryRunResult | None,
        run: WarehouseRunResult,
        client_ns: int,
    ) -> WarehouseStats:
        job = run.stats
        client_ms = client_ns / 1_000_000
        return WarehouseStats(
            engine=self.engine,
            job_id=job.job_id,
            estimated_bytes=decision.bytes_processed,
            estimated_cost_usd=decision.estimated_cost_usd,
            cost_estimate_time_ms=dry_run.elapsed_ms if dry_run is not None else None,
            cost_action=decision.action,
            limit_exceeded=decision.exceeded,
            maximum_bytes_billed=decision.maximum_bytes_billed,
            bytes_processed=job.bytes_processed,
            bytes_billed=job.bytes_billed,
            actual_cost_usd=actual_cost_usd(job.bytes_billed, self.config.cost_guard),
            cache_hit=job.cache_hit,
            queue_ms=job.queue_ms,
            server_execution_ms=job.server_execution_ms,
            result_fetch_ms=job.result_fetch_ms,
            client_overhead_ms=client_ms - job.server_total_ms - job.result_fetch_ms,
        )
