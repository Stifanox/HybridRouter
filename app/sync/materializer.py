"""Materializacja dziennych rollupów: zapytanie -> wynik -> publikacja w cache.

Rozdzielone są dwie odpowiedzialności:

* **skąd pochodzi wynik zapytania** – :class:`RollupSource` (protokół): dialekt SQL,
  nazwa surowej tabeli i metoda ``fetch(sql) -> pyarrow.Table``. Implementacja lokalna
  (DuckDB na tabeli ``pyarrow``) jest w ``app.sync.local_builder``; SyncWorker w fazie 2
  dostarczy źródło BigQuery (zapytanie przez Cost Guard, wynik przez Storage Read API),
* **jak publikowane są partycje** – ``app.sync.publisher`` (niezmienne pliki wersjonowane,
  atomowa podmiana manifestu, sprzątanie po okresie karencji).

:func:`materialize_rollups` jest wspólnym krokiem obu ścieżek.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pyarrow as pa

from app.config.schemas import ModelConfig
from app.planner.logical_plan import DateRange
from app.sync.atomic_writer import WrittenPartition
from app.sync.publisher import (
    DEFAULT_RETENTION,
    Clock,
    PublicationResult,
    publish_partitions,
    utc_now,
)
from app.sync.rollup_query import render_rollup_query


class RollupSource(Protocol):
    """Źródło wyniku zapytania rollupu."""

    @property
    def dialect(self) -> str:
        """Dialekt ``sqlglot`` silnika wykonującego zapytanie."""
        ...

    @property
    def source_table(self) -> str:
        """Surowa tabela faktów w notacji silnika (``FROM`` zapytania rollupu)."""
        ...

    def fetch(self, sql: str) -> pa.Table:
        """Wykonuje zapytanie i zwraca cały wynik jako tabelę ``pyarrow``."""
        ...


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    """Wynik materializacji: wykonany SQL, publikacja i liczba wierszy wyniku."""

    sql: str
    date_range: DateRange
    publication: PublicationResult
    result_rows: int

    @property
    def version(self) -> int:
        return self.publication.version

    @property
    def partitions(self) -> tuple[WrittenPartition, ...]:
        return self.publication.partitions

    @property
    def days_written(self) -> tuple[str, ...]:
        return tuple(p.day.isoformat() for p in self.partitions)

    @property
    def total_bytes(self) -> int:
        return sum(p.size_bytes for p in self.partitions)


def materialize_rollups(
    config: ModelConfig,
    source: RollupSource,
    date_range: DateRange,
    base_path: str | Path | None = None,
    *,
    retention: dt.timedelta = DEFAULT_RETENTION,
    clock: Clock = utc_now,
) -> MaterializationResult:
    """Generuje zapytanie rollupu, pobiera wynik ze ``source`` i publikuje dni jako nową
    wersję manifestu.

    :param base_path: katalog rollupów; domyślnie ``storage.local_cache.base_path`` modelu
        (parametr pozwala testom i skryptom pisać do katalogu tymczasowego).
    :param retention: okres karencji usuwania wycofanych plików (``app.sync.publisher``).
    """
    cache = config.storage.local_cache
    sql = render_rollup_query(config, source.source_table, date_range, source.dialect)
    result = source.fetch(sql)
    publication = publish_partitions(
        result,
        cache.base_path if base_path is None else base_path,
        cache.partition_key,
        date_range,
        retention=retention,
        clock=clock,
    )
    return MaterializationResult(sql, date_range, publication, result.num_rows)
