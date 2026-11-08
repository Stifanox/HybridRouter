"""Wspólny interfejs wykonawców zapytań (DuckDB w fazie 1, BigQuery w fazie 2).

Wykonawca przyjmuje gotowy tekst SQL w dialekcie swojego silnika (renderowany z planu
logicznego – ADR-008) i zwraca wynik jako tabelę ``pyarrow`` wraz z pomiarem czasu
wykonania. Konwersja do listy słowników (JSON odpowiedzi API) jest leniwa.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pyarrow as pa


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Wynik zapytania.

    ``execution_time_ns`` obejmuje wykonanie zapytania i pełne pobranie wyniku do
    ``pyarrow`` (``time.perf_counter_ns``); nie obejmuje kompilacji SQL ani serializacji
    odpowiedzi.
    """

    engine: str
    sql: str
    table: pa.Table
    execution_time_ns: int

    @property
    def execution_time_ms(self) -> float:
        return self.execution_time_ns / 1_000_000

    @property
    def row_count(self) -> int:
        return int(self.table.num_rows)

    @property
    def columns(self) -> list[str]:
        return list(self.table.column_names)

    def to_rows(self) -> list[dict[str, Any]]:
        """Wiersze jako lista słowników ``kolumna -> wartość`` (typy Pythona)."""
        rows: list[dict[str, Any]] = self.table.to_pylist()
        return rows


class QueryExecutionError(Exception):
    """Błąd wykonania zapytania w silniku (SQL odrzucony, brak plików, ...)."""

    def __init__(self, engine: str, sql: str, cause: Exception) -> None:
        self.engine = engine
        self.sql = sql
        self.cause = cause
        super().__init__(f"{engine} query failed: {type(cause).__name__}: {cause}")


class QueryExecutor(ABC):
    """Wykonawca zapytań jednego silnika; implementacje muszą być bezpieczne wątkowo."""

    engine: str

    @abstractmethod
    def execute(self, sql: str) -> ExecutionResult:
        """Wykonuje zapytanie ``SELECT``.

        :raises QueryExecutionError: silnik odrzucił lub przerwał zapytanie.
        """

    def close(self) -> None:  # noqa: B027 - domyślnie brak zasobów do zwolnienia
        """Zwalnia zasoby silnika."""
