"""Wykonawca DuckDB in-process na plikach Parquet (ADR-002; dok. 03, Moduł 4).

* Jedno współdzielone połączenie ``duckdb.connect(":memory:")`` na cały proces aplikacji –
  baza w pamięci, bez pliku ``.duckdb``; dane to niemutowalne pliki Parquet czytane przez
  ``read_parquet`` (zapisuje je wyłącznie SyncWorker, atomowo – ADR-005).
* **Osobny kursor na każde wywołanie** (``connection.cursor()``) – kursor DuckDB to
  niezależne połączenie do tej samej bazy, więc zapytania z wielu wątków (pula wątków
  FastAPI) wykonują się równolegle bez wspólnego stanu wyniku. Pojedynczy obiekt połączenia
  DuckDB nie jest bezpieczny wątkowo, dlatego samo tworzenie kursora jest chronione blokadą
  (operacja krótka – blokada nie obejmuje wykonania zapytania).
* Czas mierzony ``time.perf_counter_ns`` od wysłania zapytania do pobrania pełnego wyniku
  do ``pyarrow``.

Wykonawca nie ogranicza rodzaju instrukcji – SQL pochodzi wyłącznie z ``RollupRenderer``
(plan logiczny, literały AST), nigdy bezpośrednio od klienta API.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping

import duckdb

from app.executors.base import (
    ExecutionOptions,
    ExecutionResult,
    QueryExecutionError,
    QueryExecutor,
)

ConfigValue = str | int | bool | float


class DuckDBExecutor(QueryExecutor):
    """Wykonawca zapytań DuckDB (dialekt ``duckdb``, renderer ``RollupRenderer``)."""

    engine = "duckdb"

    def __init__(self, config: Mapping[str, ConfigValue] | None = None) -> None:
        """:param config: opcje DuckDB przekazywane do ``duckdb.connect`` (np. ``threads``,
        ``memory_limit``)."""
        self._connection = duckdb.connect(":memory:", config=dict(config or {}))
        self._lock = threading.Lock()
        self._closed = False

    def execute(self, sql: str, options: ExecutionOptions | None = None) -> ExecutionResult:
        """Wykonuje zapytanie; ``options`` są ignorowane (brak strażnika kosztów)."""
        cursor = self._cursor()
        try:
            started = time.perf_counter_ns()
            table = cursor.sql(sql).to_arrow_table()
            elapsed = time.perf_counter_ns() - started
        except duckdb.Error as exc:
            raise QueryExecutionError(self.engine, sql, exc) from exc
        finally:
            cursor.close()
        return ExecutionResult(self.engine, sql, table, elapsed)

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._closed:
                raise RuntimeError("DuckDBExecutor is closed")
            return self._connection.cursor()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self) -> DuckDBExecutor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
