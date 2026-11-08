"""Obsługa zapytań niezależna od HTTP: potok kompilacji, routing, wykonanie, telemetria.

Endpointy (``app.api.routes``) są cienką warstwą nad :class:`QueryService` – ten sam
obiekt może być użyty bez serwera (np. przez skrypt benchmarków w fazie 2).

Potok jednego zapytania (ADR-008, dok. 03 Moduł 2)::

    QueryRequest -> build_logical_query  (QueryValidationError -> 400)
                 -> index.snapshot()     (jedna migawka na decyzję i listę plików)
                 -> route_query          (RoutingError -> 400 w force_duckdb)
                 -> engine == bigquery   (BigQueryNotAvailableYet -> 501, faza 2)
                 -> RollupRenderer(snapshot.files_for(range)).render(plan, "duckdb")
                 -> DuckDBExecutor.execute -> wiersze JSON (+ Telemetry)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from app.api.errors import BigQueryNotAvailableYet
from app.api.models import (
    BaseMeasureInfo,
    CacheHealth,
    DerivedMetricInfo,
    DimensionInfo,
    HealthResponse,
    ModelInfo,
    QueryRequest,
    QueryResponse,
    SchemaResponse,
    Telemetry,
)
from app.executors.base import QueryExecutor
from app.planner.builder import build_logical_query
from app.renderers.rollup import RollupRenderer
from app.router.decision_engine import route_query
from app.semantic.resolver import SemanticModel
from app.sync.manifest import ManifestError
from app.sync.partition_index import PartitionIndex, PartitionSnapshot

logger = logging.getLogger(__name__)

LOCAL_DIALECT = "duckdb"


def _ms(nanoseconds: int) -> float:
    return round(nanoseconds / 1_000_000, 3)


@dataclass(frozen=True, slots=True)
class QueryService:
    """Stan aplikacji tworzony raz w ``lifespan``: modele, indeksy partycji, wykonawca.

    Obiekt jest niemutowalny i bezpieczny wątkowo (indeks i wykonawca są bezpieczne
    wątkowo, modele – niemutowalne), więc obsługuje równoległe żądania z puli wątków.
    """

    models: Mapping[str, SemanticModel]
    indexes: Mapping[str, PartitionIndex]
    local_executor: QueryExecutor

    # ------------------------------------------------------------------ query
    def execute(self, request: QueryRequest) -> QueryResponse:
        telemetry_on = request.control.include_telemetry
        clock = time.perf_counter_ns
        started = clock() if telemetry_on else 0

        plan = build_logical_query(request, self.models)
        planned = clock() if telemetry_on else 0

        model = self.models[plan.model_name]
        snapshot = self._snapshot(plan.model_name)
        decision = route_query(plan, model, request.control.execution_mode, snapshot)
        if decision.engine != LOCAL_DIALECT:
            # Punkt rozszerzenia fazy 2: BigQueryExecutor + Cost Guard.
            raise BigQueryNotAvailableYet(decision)

        routed = clock() if telemetry_on else 0
        sql = RollupRenderer(snapshot.files_for(plan.date_range)).render(plan, LOCAL_DIALECT)
        rendered = clock() if telemetry_on else 0

        result = self.local_executor.execute(sql)
        rows = to_json_rows(result.table)
        if not telemetry_on:
            return QueryResponse(data=rows)

        finished = clock()
        return QueryResponse(
            data=rows,
            telemetry=Telemetry(
                engine_used=decision.engine,
                routing_mode=decision.mode,
                routing_reason=decision.reason,
                ast_compilation_time_ms=_ms((planned - started) + (rendered - routed)),
                execution_time_ms=round(result.execution_time_ms, 3),
                total_time_ms=_ms(finished - started),
                bytes_scanned=0,
                estimated_cost_usd=0.0,
                compiled_sql=sql,
            ),
        )

    def _snapshot(self, model_name: str) -> PartitionSnapshot:
        """Migawka indeksu; przy uszkodzonym manifeście – ostatnia poprawna migawka."""
        index = self.indexes[model_name]
        try:
            return index.snapshot()
        except ManifestError:
            logger.exception("cannot refresh cache index of '%s'; using last snapshot", model_name)
            return index.current

    # ----------------------------------------------------------------- schema
    def schema(self) -> SchemaResponse:
        return SchemaResponse(models=[_model_info(model) for model in self.models.values()])

    # ----------------------------------------------------------------- health
    def health(self) -> HealthResponse:
        caches = {name: self._cache_health(index) for name, index in self.indexes.items()}
        healthy = all(c.status == "ok" for c in caches.values())
        return HealthResponse(status="ok" if healthy else "degraded", models=caches)

    @staticmethod
    def _cache_health(index: PartitionIndex) -> CacheHealth:
        error: str | None = None
        try:
            snapshot = index.snapshot()
        except ManifestError as exc:
            snapshot, error = index.current, f"manifest error: {exc}"
        days = sorted(snapshot.days)
        return CacheHealth(
            status="ok" if error is None else "degraded",
            partitions=len(days),
            manifest_version=snapshot.version,
            first_day=days[0].isoformat() if days else None,
            last_day=days[-1].isoformat() if days else None,
            error=error,
        )


def _model_info(model: SemanticModel) -> ModelInfo:
    config = model.config
    return ModelInfo(
        name=model.name,
        description=config.description,
        partition_key=config.storage.local_cache.partition_key,
        dimensions=[
            DimensionInfo(name=name, type=d.type, in_rollup=d.in_rollup)
            for name, d in config.dimensions.items()
        ],
        base_measures=[
            BaseMeasureInfo(
                name=name,
                aggregation=measure.aggregation,
                additive=model.metrics[name].additive,
            )
            for name, measure in config.base_measures.items()
        ],
        derived_metrics=[
            DerivedMetricInfo(
                name=name,
                formula=metric.formula,
                description=metric.description,
                additive=model.metrics[name].additive,
                base_measures=sorted(model.metrics[name].base_measures),
            )
            for name, metric in config.derived_metrics.items()
        ],
    )


def to_json_rows(table: pa.Table) -> list[dict[str, Any]]:
    """Wiersze wyniku z typami zgodnymi z JSON.

    ``SUM`` liczb całkowitych w DuckDB ma typ ``HUGEINT`` (Arrow ``decimal128(38, 0)``), a
    kwoty ``NUMERIC`` – ``decimal128(p, s)``; Pydantic serializowałby ``Decimal`` jako
    napis. Kolumny ``decimal(p, 0)`` są rzutowane na ``int64`` (rzutowanie bezpieczne),
    pozostałe ``decimal`` – na ``float64`` (liczba JSON). Daty serializuje Pydantic (ISO).
    """
    for position, field in enumerate(table.schema):
        if pa.types.is_decimal(field.type):
            target = pa.int64() if field.type.scale == 0 else pa.float64()
            column = pc.cast(table[position], target, safe=True)
            table = table.set_column(position, pa.field(field.name, target), column)
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows
