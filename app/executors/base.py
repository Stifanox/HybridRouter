"""Wspólny interfejs wykonawców zapytań (DuckDB w fazie 1, BigQuery w fazie 2).

Wykonawca przyjmuje gotowy tekst SQL w dialekcie swojego silnika (renderowany z planu
logicznego – ADR-008) i zwraca wynik jako tabelę ``pyarrow`` wraz z pomiarem czasu
wykonania. Konwersja do listy słowników (JSON odpowiedzi API) jest leniwa.

Od fazy 2 ``execute`` przyjmuje opcjonalne :class:`ExecutionOptions` (potwierdzenie wysokiego
kosztu dla strażnika kosztów hurtowni), a :class:`ExecutionResult` może nieść statystyki
hurtowni (:class:`WarehouseStats`; ``None`` dla DuckDB). Wykonawca DuckDB opcje ignoruje.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pyarrow as pa


@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    """Opcje wykonania przekazywane z żądania (``control``).

    ``confirm_high_cost`` – klient potwierdza wysoki koszt (tryb ``require_confirmation``
    strażnika kosztów, ADR-006); ignorowane przez silniki bez strażnika.
    """

    confirm_high_cost: bool = False


@dataclass(frozen=True, slots=True)
class WarehouseStats:
    """Statystyki zapytania wykonanego w hurtowni (nazwy niezależne od dostawcy, F2-9).

    Szacunek (dry-run, ``None`` gdy strażnik kosztów wyłączony i dry-run pominięto):
    ``estimated_bytes``, ``estimated_cost_usd``, ``cost_estimate_time_ms`` oraz decyzja
    strażnika (``cost_action``, ``limit_exceeded``, ``maximum_bytes_billed``).

    Zadanie: ``job_id``, ``bytes_processed`` i ``bytes_billed`` (rzeczywiste), ``cache_hit``,
    ``actual_cost_usd`` (z ``bytes_billed``).

    Rozkład czasu (F2-7), ms:

    * ``queue_ms = started − created``, ``server_execution_ms = ended − started`` – znaczniki
      serwera,
    * ``result_fetch_ms`` – pobranie wyniku po stronie klienta,
    * ``client_overhead_ms = czas klienta − (ended − created) − result_fetch_ms`` – narzut
      sieci i biblioteki (``czas klienta`` = ``ExecutionResult.execution_time_ns``, bez
      dry-runu; przybliżenie – może być nieznacznie ujemne przy zaokrągleniach znaczników
      serwera do milisekund).
    """

    engine: str
    job_id: str
    estimated_bytes: int | None
    estimated_cost_usd: float | None
    cost_estimate_time_ms: float | None
    cost_action: str
    limit_exceeded: bool
    maximum_bytes_billed: int | None
    bytes_processed: int
    bytes_billed: int
    actual_cost_usd: float
    cache_hit: bool
    queue_ms: float
    server_execution_ms: float
    result_fetch_ms: float
    client_overhead_ms: float


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Wynik zapytania.

    ``execution_time_ns`` obejmuje wykonanie zapytania i pełne pobranie wyniku do
    ``pyarrow`` (``time.perf_counter_ns``); nie obejmuje kompilacji SQL ani serializacji
    odpowiedzi, a w hurtowni – dry-runu strażnika kosztów (osobno:
    ``warehouse.cost_estimate_time_ms``). ``warehouse`` – statystyki hurtowni (``None`` dla
    DuckDB).
    """

    engine: str
    sql: str
    table: pa.Table
    execution_time_ns: int
    warehouse: WarehouseStats | None = None

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
    def execute(self, sql: str, options: ExecutionOptions | None = None) -> ExecutionResult:
        """Wykonuje zapytanie ``SELECT``.

        :param options: opcje z żądania (``None`` = domyślne :class:`ExecutionOptions`).
        :raises QueryExecutionError: silnik lokalny odrzucił lub przerwał zapytanie.

        Wykonawca hurtowni zgłasza zamiast tego wyjątki strażnika kosztów
        (``app.warehouse.cost_guard``) i bramki (``app.warehouse.gateway``).
        """

    def close(self) -> None:  # noqa: B027 - domyślnie brak zasobów do zwolnienia
        """Zwalnia zasoby silnika."""
