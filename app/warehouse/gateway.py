"""Protokół bramki hurtowni (``WarehouseGateway``), wyniki zadań i wspólne wyjątki (ADR-014).

Bramka jest cienką warstwą nad klientem hurtowni z dwiema operacjami:

* :meth:`WarehouseGateway.dry_run` – walidacja zapytania i liczba bajtów do przetworzenia
  (w BigQuery bez kosztu),
* :meth:`WarehouseGateway.run` – wykonanie zapytania z twardym limitem rozliczanych bajtów
  (``maximum_bytes_billed``) i limitem czasu; wynik jako tabela ``pyarrow`` + statystyki
  zadania (:class:`JobStats`).

Nazwy są niezależne od dostawcy (F2-9) – implementacje: ``GoogleBigQueryGateway``
(:mod:`app.warehouse.google_gateway`) i ``FakeBigQueryGateway``
(:mod:`app.warehouse.fake_gateway`). Implementacje zamieniają błędy swoich bibliotek na
wyjątki z tego modułu, dzięki czemu wyższe warstwy (wykonawca, API, synchronizator) nie
zależą od biblioteki Google.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa


def _ms(nanoseconds: int) -> float:
    return nanoseconds / 1_000_000


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """Wynik dry-runu: bajty, które przetworzyłoby zapytanie, i czas wywołania po stronie
    klienta (``time.perf_counter_ns`` wokół całego wywołania API – z siecią)."""

    bytes_processed: int
    elapsed_ns: int

    @property
    def elapsed_ms(self) -> float:
        return _ms(self.elapsed_ns)


@dataclass(frozen=True, slots=True)
class JobStats:
    """Statystyki zadania hurtowni.

    ``created`` ≤ ``started`` ≤ ``ended`` to znaczniki czasu z **serwera** (UTC, ze strefą);
    ich różnice nie zależą od zegara klienta. ``result_fetch_ns`` to czas pobrania wyniku do
    ``pyarrow`` mierzony **po stronie klienta** (po zakończeniu zadania).
    ``bytes_billed`` uwzględnia minimum rozliczeniowe (BigQuery on-demand: 10 MB na tabelę)
    i wynosi 0 przy trafieniu w cache wyników (``cache_hit``).
    """

    job_id: str
    bytes_processed: int
    bytes_billed: int
    cache_hit: bool
    created: dt.datetime
    started: dt.datetime
    ended: dt.datetime
    result_fetch_ns: int

    @property
    def queue_ms(self) -> float:
        """Oczekiwanie w kolejce: ``started − created``."""
        return (self.started - self.created) / dt.timedelta(milliseconds=1)

    @property
    def server_execution_ms(self) -> float:
        """Wykonanie po stronie serwera: ``ended − started``."""
        return (self.ended - self.started) / dt.timedelta(milliseconds=1)

    @property
    def server_total_ms(self) -> float:
        """Czas życia zadania na serwerze: ``ended − created``."""
        return (self.ended - self.created) / dt.timedelta(milliseconds=1)

    @property
    def result_fetch_ms(self) -> float:
        return _ms(self.result_fetch_ns)


@dataclass(frozen=True, slots=True)
class WarehouseRunResult:
    """Wynik wykonania zapytania: tabela ``pyarrow`` i statystyki zadania."""

    table: pa.Table
    stats: JobStats


class WarehouseGateway(Protocol):
    """Port hurtowni danych (wzorzec *ports and adapters*). Implementacje muszą być
    bezpieczne wątkowo – jedna instancja obsługuje wszystkie wątki puli API (F2-10)."""

    def dry_run(self, sql: str, *, location: str) -> DryRunResult:
        """Waliduje zapytanie i zwraca liczbę bajtów do przetworzenia (bez wykonania).

        :raises WarehouseQueryError: hurtownia odrzuciła zapytanie (składnia, uprawnienia,
            brak tabeli).
        :raises WarehouseUnavailableError: brak klienta lub poświadczeń.
        """
        ...

    def run(
        self,
        sql: str,
        *,
        location: str,
        maximum_bytes_billed: int | None,
        use_query_cache: bool,
        timeout_s: float,
    ) -> WarehouseRunResult:
        """Wykonuje zapytanie i pobiera pełny wynik.

        :param maximum_bytes_billed: twardy limit rozliczanych bajtów egzekwowany przez
            hurtownię (``None`` – bez limitu); przekroczenie kończy zadanie błędem.
        :param timeout_s: limit czasu oczekiwania na zakończenie zadania; po przekroczeniu
            zadanie jest anulowane.
        :raises WarehouseQueryError: błąd zgłoszony przez hurtownię (w tym przekroczony
            ``maximum_bytes_billed``).
        :raises WarehouseTimeoutError: przekroczono ``timeout_s``.
        :raises WarehouseUnavailableError: brak klienta lub poświadczeń.
        """
        ...

    def close(self) -> None:
        """Zwalnia zasoby klienta (połączenia HTTP, bazę atrapy)."""
        ...


# --------------------------------------------------------------------------- #
# Wyjątki bramki
# --------------------------------------------------------------------------- #


class WarehouseError(Exception):
    """Bazowy wyjątek bramki hurtowni."""


class WarehouseQueryError(WarehouseError):
    """Błąd zgłoszony przez hurtownię: składnia, uprawnienia, brak tabeli, przekroczony
    ``maximum_bytes_billed`` itp. (HTTP 502 w API, F2-10).

    ``message`` to komunikat hurtowni; ``reason`` – kod przyczyny dostawcy, jeśli znany
    (BigQuery: ``invalidQuery``, ``accessDenied``, ``bytesBilledLimitExceeded``, ...);
    ``status_code`` – kod HTTP odpowiedzi API hurtowni, jeśli znany.
    """

    def __init__(
        self,
        message: str,
        *,
        sql: str | None = None,
        job_id: str | None = None,
        reason: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message
        self.sql = sql
        self.job_id = job_id
        self.reason = reason
        self.status_code = status_code
        details = ", ".join(
            f"{name}={value}"
            for name, value in (("reason", reason), ("status", status_code), ("job", job_id))
            if value is not None
        )
        super().__init__(
            f"Warehouse rejected the query: {message}" + (f" ({details})" if details else "")
        )


class WarehouseTimeoutError(WarehouseError):
    """Zadanie nie zakończyło się w ``timeout_s`` (HTTP 504). ``cancelled`` mówi, czy
    hurtownia przyjęła żądanie anulowania (``False`` – anulowanie nie powiodło się albo
    zadanie nie zostało utworzone; może jeszcze naliczać koszt)."""

    def __init__(self, timeout_s: float, *, job_id: str | None, cancelled: bool) -> None:
        self.timeout_s = timeout_s
        self.job_id = job_id
        self.cancelled = cancelled
        state = "cancelled" if cancelled else "NOT cancelled"
        super().__init__(f"Warehouse job {job_id} exceeded timeout of {timeout_s:g} s ({state})")


class WarehouseUnavailableError(WarehouseError):
    """Hurtownia niedostępna dla aplikacji: brak lub nieważne poświadczenia, brak klienta,
    brak łączności (HTTP 503)."""
