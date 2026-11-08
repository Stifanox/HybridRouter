"""``RollupRenderer`` – zapytanie do dziennych rollupów Parquet (DuckDB, ADR-002/003/005).

* ``FROM read_parquet(['<plik>', ...], hive_partitioning = true)`` – **jawna lista**
  niezmiennych plików z migawki indeksu partycji (``PartitionSnapshot.files_for``), nie
  wzorzec ``*``: katalog dnia może chwilowo zawierać kilka wersji pliku (stara czeka na
  usunięcie po okresie karencji), a wzorzec policzyłby je wszystkie. Kolumna partycji
  (``pickup_date``) pochodzi z nazwy katalogu Hive,
* liść ``MeasureRef`` -> funkcja re-agregacji z rejestru na kolumnie o nazwie miary,
  np. ``SUM(total_fare_amount)``, ``COALESCE(SUM(trips_count), 0)`` (``COUNT -> SUM``;
  ``COALESCE`` przywraca wynik ``COUNT`` na pustym wejściu – ``AggregationSpec.empty_input_value``),
* wymiar -> sama nazwa kolumny.

Miara holistyczna (``rollup_reaggregate = None``) lub wymiar spoza rollupu oznaczają, że
router przepuścił plan niespełniający Warunku 3 lub 1 – zgłaszany jest błąd programistyczny.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp

from app.config.schemas import BaseMeasureConfig
from app.planner.logical_plan import PlannedDimension
from app.renderers.base import SqlRenderer, aggregate
from app.renderers.errors import (
    DimensionNotInRollupError,
    NonAdditiveMeasureError,
    UnsupportedFunctionError,
)
from app.semantic.registry import get_aggregation


class RollupRenderer(SqlRenderer):
    """Renderer dla dziennych rollupów Parquet w układzie Hive.

    :param files: niepusta lista ścieżek plików rollupu (z jednej migawki indeksu).
    """

    def __init__(self, files: Sequence[str]) -> None:
        if isinstance(files, str):
            raise TypeError("RollupRenderer expects a list of file paths, not a single string")
        if not files:
            raise ValueError("RollupRenderer needs at least one rollup file")
        self.files = tuple(files)

    def source(self) -> exp.Expr:
        read_parquet = exp.ReadParquet(
            expressions=[
                exp.Array(expressions=[exp.Literal.string(path) for path in self.files]),
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
        reaggregated = aggregate(spec.rollup_reaggregate, exp.column(name))
        if spec.empty_input_value is None:
            return reaggregated
        # COUNT na pustym wejściu daje 0, a SUM(liczności) z pustego rollupu – NULL.
        return exp.Coalesce(
            this=reaggregated, expressions=[exp.Literal.number(spec.empty_input_value)]
        )
