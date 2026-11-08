"""Lokalne budowanie rollupów: DuckDB na surowej tabeli ``pyarrow`` (ADR-012).

Surowe dane (np. syntetyczne z ``tests/fixtures/synthetic.py``) są rejestrowane w
osobnym, krótkotrwałym połączeniu DuckDB jako tabela :data:`RAW_TABLE_NAME`, a zapytanie
rollupu – generowane tym samym kodem co w SyncWorkerze – wykonywane jest w dialekcie
``duckdb``. Publikację partycji realizuje wspólny :func:`materialize_rollups`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from app.config.schemas import ModelConfig
from app.planner.logical_plan import DateRange
from app.sync.materializer import MaterializationResult, materialize_rollups
from app.sync.publisher import DEFAULT_RETENTION, Clock, utc_now

RAW_TABLE_NAME = "raw_trips"


def _integer_decimals_to_int64(table: pa.Table) -> pa.Table:
    """``SUM`` liczb całkowitych w DuckDB daje ``HUGEINT`` (eksportowany do Arrow jako
    ``decimal128(38, 0)``), w BigQuery – ``INT64``. Kolumny ``decimal(p, 0)`` są rzutowane
    na ``int64`` (rzutowanie bezpieczne – przepełnienie zgłasza błąd), żeby rollup lokalny
    miał typy rollupu z BigQuery."""
    for position, field in enumerate(table.schema):
        if pa.types.is_decimal(field.type) and field.type.scale == 0:
            column = pc.cast(table[position], pa.int64(), safe=True)
            table = table.set_column(position, pa.field(field.name, pa.int64()), column)
    return table


class DuckDBArrowSource:
    """:class:`~app.sync.materializer.RollupSource` – DuckDB na tabeli ``pyarrow``."""

    dialect = "duckdb"

    def __init__(self, raw: pa.Table, table_name: str = RAW_TABLE_NAME) -> None:
        self._raw = raw
        self._table_name = table_name

    @property
    def source_table(self) -> str:
        return self._table_name

    def fetch(self, sql: str) -> pa.Table:
        with duckdb.connect(":memory:") as connection:
            connection.register(self._table_name, self._raw)
            result = connection.sql(sql).to_arrow_table()
        return _integer_decimals_to_int64(result)


def build_local_rollups(
    raw: pa.Table,
    config: ModelConfig,
    date_range: DateRange,
    base_path: str | Path | None = None,
    *,
    retention: dt.timedelta = DEFAULT_RETENTION,
    clock: Clock = utc_now,
) -> MaterializationResult:
    """Buduje i publikuje dzienne rollupy Parquet z surowej tabeli dla dni ``date_range``."""
    return materialize_rollups(
        config, DuckDBArrowSource(raw), date_range, base_path, retention=retention, clock=clock
    )
