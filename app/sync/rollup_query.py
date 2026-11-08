"""Generator zapytania materializującego dzienny rollup (dok. 03, Moduł 5 pkt 2).

Zapytanie rollupu jest zwykłym planem logicznym (:class:`LogicalQuery`) renderowanym przez
:class:`RawTableRenderer` – generator nie zawiera własnej logiki SQL, więc fragmenty ``sql``
z YAML, agregacje, filtry dat i transpilacja dialektu są **dokładnie te same**, co w
zapytaniach użytkownika kierowanych do surowej tabeli (ADR-008, ADR-012):

* wymiary: wszystkie z ``in_rollup: true`` (w kolejności z YAML), aliasowane nazwą wymiaru,
* metryki: wszystkie **addytywne** miary bazowe z ich **oryginalną** agregacją
  (``COUNT(1) AS trips_count``); miary holistyczne (``COUNT_DISTINCT``) są pomijane
  (ADR-011),
* filtr: ``partition_key`` w domkniętym przedziale ``[start, end]``,
* sortowanie po wszystkich wymiarach (deterministyczna zawartość plików Parquet).

Ten sam generator służy SyncWorkerowi (BigQuery, faza 2) i lokalnemu budowaniu rollupów
w testach (DuckDB na danych syntetycznych).
"""

from __future__ import annotations

from types import MappingProxyType

from app.config.schemas import BaseMeasureConfig, ModelConfig
from app.planner.logical_plan import (
    DateRange,
    LogicalQuery,
    PlannedDimension,
    PlannedFilter,
    PlannedMetric,
    PlannedOrder,
)
from app.renderers.raw_table import RawTableRenderer
from app.semantic.ast_nodes import MeasureRef
from app.semantic.registry import is_additive


def rollup_dimensions(config: ModelConfig) -> tuple[PlannedDimension, ...]:
    """Wymiary materializowane w rollupie (``in_rollup: true``), w kolejności z YAML."""
    return tuple(
        PlannedDimension(name, definition)
        for name, definition in config.dimensions.items()
        if definition.in_rollup
    )


def rollup_measures(config: ModelConfig) -> dict[str, BaseMeasureConfig]:
    """Miary bazowe materializowane w rollupie – tylko addytywne (ADR-011)."""
    return {
        name: measure
        for name, measure in config.base_measures.items()
        if is_additive(measure.aggregation)
    }


def build_rollup_plan(config: ModelConfig, date_range: DateRange) -> LogicalQuery:
    """Plan logiczny zapytania rollupu dla dni ``date_range`` (domknięty przedział)."""
    start, end = date_range
    if start > end:
        raise ValueError(f"empty date range: {start.isoformat()} > {end.isoformat()}")
    measures = rollup_measures(config)
    if not measures:
        raise ValueError(f"model '{config.model_name}' has no additive base measures to roll up")

    dimensions = rollup_dimensions(config)
    partition_key = config.storage.local_cache.partition_key
    partition = next(d for d in dimensions if d.name == partition_key)  # gwarantuje loader
    return LogicalQuery(
        model_name=config.model_name,
        partition_key=partition_key,
        dimensions=dimensions,
        metrics=tuple(
            PlannedMetric(name, MeasureRef(name), frozenset({name}), additive=True)
            for name in measures
        ),
        filters=(
            PlannedFilter(partition, ">=", (start,)),
            PlannedFilter(partition, "<=", (end,)),
        ),
        order_by=tuple(PlannedOrder(d.name, "ASC") for d in dimensions),
        limit=None,
        date_range=(start, end),
        base_measures=MappingProxyType(measures),
        used_dimensions=MappingProxyType({d.name: d for d in dimensions}),
    )


def render_rollup_query(
    config: ModelConfig,
    source_table: str,
    date_range: DateRange,
    dialect: str,
    *,
    pretty: bool = False,
) -> str:
    """Tekst SQL zapytania rollupu.

    :param source_table: surowa tabela faktów – ``projekt.zbiór.tabela`` (BigQuery) lub
        nazwa tabeli lokalnej (DuckDB).
    :param date_range: dni do zmaterializowania (domknięty przedział).
    :param dialect: dialekt ``sqlglot`` silnika wykonującego (``bigquery`` / ``duckdb``).
    """
    plan = build_rollup_plan(config, date_range)
    return RawTableRenderer(source_table).render(plan, dialect, pretty=pretty)
