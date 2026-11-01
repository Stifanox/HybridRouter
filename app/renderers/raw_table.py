"""``RawTableRenderer`` – zapytanie do surowej tabeli faktów (BigQuery lub surowe dane testowe).

* liść ``MeasureRef`` -> ``AGG(<sql z YAML>)``, np. ``total_fare_amount`` ->
  ``SUM(fare_amount)``, ``unique_pickup_zones`` -> ``COUNT(DISTINCT pickup_location_id)``,
* wymiar -> wyrażenie ``sql`` z YAML aliasowane nazwą wymiaru, np.
  ``CAST(vendor_id AS STRING) AS vendor_id``; w ``WHERE`` i ``GROUP BY`` występuje pełne
  wyrażenie (alias z ``SELECT`` nie jest tam przenośny między silnikami).

Fragmenty ``sql`` są w dialekcie BigQuery i są parsowane ``read="bigquery"``, dzięki czemu
ten sam renderer daje poprawny SQL także dla DuckDB (testy różnicowe, ADR-012).
"""

from __future__ import annotations

from sqlglot import exp

from app.config.schemas import BaseMeasureConfig
from app.planner.logical_plan import PlannedDimension
from app.renderers.base import SOURCE_DIALECT, SqlRenderer, aggregate, parse_fragment


class RawTableRenderer(SqlRenderer):
    """Renderer dla surowej tabeli; ``source_table`` w notacji BigQuery
    (``projekt.zbiór.tabela``) lub nazwa lokalnej tabeli."""

    def __init__(self, source_table: str) -> None:
        self.source_table = source_table
        self._table = exp.to_table(source_table, dialect=SOURCE_DIALECT)

    def source(self) -> exp.Expr:
        return self._table.copy()

    def dimension_expr(self, dimension: PlannedDimension) -> exp.Expr:
        return parse_fragment(dimension.definition.sql, dimension.name)

    def measure_expr(self, name: str, measure: BaseMeasureConfig) -> exp.Expr:
        return aggregate(measure.aggregation, parse_fragment(measure.sql, name))
