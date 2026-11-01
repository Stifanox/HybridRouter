"""``RollupRenderer`` – zapytanie do dziennych rollupów Parquet (DuckDB, ADR-002/003).

* ``FROM read_parquet('<glob>', hive_partitioning = true)`` – kolumna partycji
  (``pickup_date``) pochodzi z nazwy katalogu Hive,
* liść ``MeasureRef`` -> funkcja re-agregacji z rejestru na kolumnie o nazwie miary,
  np. ``SUM(total_fare_amount)``, ``SUM(trips_count)`` (``COUNT -> SUM``),
* wymiar -> sama nazwa kolumny.

Miara holistyczna (``rollup_reaggregate = None``) lub wymiar spoza rollupu oznaczają, że
router przepuścił plan niespełniający Warunku 3 lub 1 – zgłaszany jest błąd programistyczny.
"""

from __future__ import annotations

from sqlglot import exp

from app.config.schemas import BaseMeasureConfig, LocalCacheConfig
from app.planner.logical_plan import PlannedDimension
from app.renderers.base import SqlRenderer, aggregate
from app.renderers.errors import (
    DimensionNotInRollupError,
    NonAdditiveMeasureError,
    UnsupportedFunctionError,
)
from app.semantic.registry import get_aggregation

ROLLUP_FILE_NAME = "rollup.parquet"


def rollup_glob(base_path: str, partition_key: str) -> str:
    """Wzorzec plików rollupu: ``{base_path}/{partition_key}=*/rollup.parquet``.

    Wzorzec jawnie wskazuje ``rollup.parquet``, więc pliki ``.tmp`` (ADR-005) nigdy nie są
    czytane.
    """
    return f"{base_path.rstrip('/')}/{partition_key}=*/{ROLLUP_FILE_NAME}"


def rollup_glob_for(cache: LocalCacheConfig) -> str:
    """Wzorzec plików rollupu dla sekcji ``storage.local_cache`` modelu."""
    return rollup_glob(cache.base_path, cache.partition_key)


class RollupRenderer(SqlRenderer):
    """Renderer dla dziennych rollupów Parquet w układzie Hive."""

    def __init__(self, parquet_glob: str) -> None:
        self.parquet_glob = parquet_glob

    def source(self) -> exp.Expr:
        read_parquet = exp.ReadParquet(
            expressions=[
                exp.Literal.string(self.parquet_glob),
                exp.EQ(this=exp.column("hive_partitioning"), expression=exp.true()),
            ]
        )
        return exp.Table(this=read_parquet)

    def dimension_expr(self, dimension: PlannedDimension) -> exp.Expr:
        if not dimension.in_rollup:
            raise DimensionNotInRollupError(dimension.name)
        return exp.column(dimension.name)

    def measure_expr(self, name: str, measure: BaseMeasureConfig) -> exp.Expr:
        spec = get_aggregation(measure.aggregation)
        if spec is None:
            raise UnsupportedFunctionError(f"unknown aggregation '{measure.aggregation}'")
        if spec.rollup_reaggregate is None:
            raise NonAdditiveMeasureError(name, spec.name)
        return aggregate(spec.rollup_reaggregate, exp.column(name))
