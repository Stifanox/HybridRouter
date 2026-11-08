"""``GoogleBigQueryGateway`` – implementacja bramki na bibliotece ``google-cloud-bigquery``.

**Jedyny moduł w ``app/`` importujący ``google.cloud.bigquery``** (ADR-014, zasady wspólne
pkt 7). Pozostała część aplikacji zna wyłącznie protokół
:class:`~app.warehouse.gateway.WarehouseGateway` i wyjątki bramki.

* Klient (``bigquery.Client(project=project_id)``) – jeden na proces, bezpieczny wątkowo;
  poświadczenia wyłącznie z Application Default Credentials (F2-6). Brak poświadczeń przy
  tworzeniu klienta -> :class:`WarehouseUnavailableError`.
* ``dry_run``: ``QueryJobConfig(dry_run=True, use_query_cache=False)`` – zadanie nie jest
  wykonywane ani rozliczane; ``total_bytes_processed`` (dok. 03, Moduł 3).
* ``run``: ``QueryJobConfig(use_query_cache=..., maximum_bytes_billed=..., job_timeout_ms=...)``
  w regionie ``location``. Czas oczekiwania ogranicza ``job.result(timeout=...)``; po jego
  przekroczeniu zadanie jest anulowane (``job.cancel()``), żeby nie płacić za porzucone
  zapytanie (F2-10). ``timeout`` biblioteki dotyczy pojedynczych wywołań HTTP i pętli
  oczekiwania – nie jest ścisłym limitem całkowitym, dlatego ``job_timeout_ms`` (ten sam
  limit) jest bezpiecznikiem po stronie serwera: BigQuery przerwie zadanie nawet wtedy, gdy
  klient przestanie czekać albo żądanie anulowania nie dotrze.
* Wynik: ``RowIterator.to_arrow(create_bqstorage_client=...)``. Domyślnie **bez** BigQuery
  Storage Read API – wyniki zapytań API są małe (zagregowane), a REST
  (``jobs.getQueryResults``) nie wymaga tworzenia sesji odczytu (mniejsze opóźnienie).
  Synchronizator (B3) może włączyć Storage API parametrem ``use_storage_api`` (wymaga
  pakietu ``google-cloud-bigquery-storage``).

Mapowanie wyjątków:

=====================================================  ==============================
źródło                                                 wyjątek bramki
=====================================================  ==============================
``google.auth`` ``DefaultCredentialsError``,            :class:`WarehouseUnavailableError`
``RefreshError``, API ``Unauthenticated`` (401),
``RetryError`` (wyczerpane ponowienia – brak łączności)
``concurrent.futures.TimeoutError`` z ``result()``,     :class:`WarehouseTimeoutError`
``DeadlineExceeded``, błąd zadania z ``reason``
z :data:`SERVER_TIMEOUT_REASONS` (``job_timeout_ms``)
pozostałe ``GoogleAPICallError`` (400 ``BadRequest``,   :class:`WarehouseQueryError`
403 ``Forbidden``, 404 ``NotFound``, 5xx, ...)          (komunikat i ``reason`` BigQuery)
=====================================================  ==============================
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import time
from collections.abc import Iterator
from contextlib import contextmanager

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import bigquery

from app.warehouse.gateway import (
    DryRunResult,
    JobStats,
    WarehouseQueryError,
    WarehouseRunResult,
    WarehouseTimeoutError,
    WarehouseUnavailableError,
)

SERVER_TIMEOUT_REASONS = frozenset({"timeout", "jobTimeout"})
"""Kody przyczyny, którymi BigQuery może oznaczyć zadanie przerwane po ``job_timeout_ms``
(do potwierdzenia na prawdziwej usłudze w B4)."""


class GoogleBigQueryGateway:
    """Bramka do Google BigQuery (``WarehouseGateway``)."""

    def __init__(
        self,
        project_id: str,
        *,
        client: bigquery.Client | None = None,
        use_storage_api: bool = False,
    ) -> None:
        """:param project_id: projekt rozliczeniowy zadań (``storage.bigquery.project_id``).
        :param client: gotowy klient (testy z obiektem zastępczym); domyślnie tworzony z
            Application Default Credentials.
        :param use_storage_api: pobieranie wyniku przez BigQuery Storage Read API (duże
            wyniki – synchronizator, B3).
        :raises WarehouseUnavailableError: brak poświadczeń / nie można utworzyć klienta.
        """
        self.project_id = project_id
        self.use_storage_api = use_storage_api
        if client is None:
            try:
                client = bigquery.Client(project=project_id)
            except (auth_exceptions.GoogleAuthError, OSError) as exc:
                raise WarehouseUnavailableError(
                    f"Cannot create BigQuery client for project {project_id!r}: {exc}. "
                    "Configure Application Default Credentials "
                    "(gcloud auth application-default login)."
                ) from exc
        self._client = client

    # ------------------------------------------------------------------ dry-run
    def dry_run(self, sql: str, *, location: str) -> DryRunResult:
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        started = time.perf_counter_ns()
        with _mapped_errors(sql):
            job = self._client.query(sql, job_config=job_config, location=location)
        elapsed = time.perf_counter_ns() - started
        return DryRunResult(bytes_processed=int(job.total_bytes_processed or 0), elapsed_ns=elapsed)

    # ---------------------------------------------------------------------- run
    def run(
        self,
        sql: str,
        *,
        location: str,
        maximum_bytes_billed: int | None,
        use_query_cache: bool,
        timeout_s: float,
    ) -> WarehouseRunResult:
        job_config = bigquery.QueryJobConfig(use_query_cache=use_query_cache)
        if maximum_bytes_billed is not None:
            # Konstruktor z ``maximum_bytes_billed=None`` zapisałby napis "None" (biblioteka
            # serializuje wartość przez str) – limit ustawiany tylko, gdy istnieje.
            job_config.maximum_bytes_billed = maximum_bytes_billed
        job_config.job_timeout_ms = max(1, round(timeout_s * 1000))
        with _mapped_errors(sql):
            job = self._client.query(sql, job_config=job_config, location=location)
        with _mapped_errors(sql, job_id=job.job_id):
            try:
                rows = job.result(timeout=timeout_s)
            except (concurrent.futures.TimeoutError, api_exceptions.DeadlineExceeded) as exc:
                raise WarehouseTimeoutError(
                    timeout_s, job_id=job.job_id, cancelled=self._cancel(job)
                ) from exc
            except api_exceptions.GoogleAPICallError as exc:
                if _reason(exc) not in SERVER_TIMEOUT_REASONS:
                    raise
                # Zadanie przerwane przez serwer po job_timeout_ms – nie nalicza już kosztu.
                raise WarehouseTimeoutError(timeout_s, job_id=job.job_id, cancelled=True) from exc
            fetch_started = time.perf_counter_ns()
            table = rows.to_arrow(create_bqstorage_client=self.use_storage_api)
            fetch_ns = time.perf_counter_ns() - fetch_started
        return WarehouseRunResult(table=table, stats=_job_stats(job, fetch_ns))

    @staticmethod
    def _cancel(job: bigquery.QueryJob) -> bool:
        """Anuluje zadanie; ``True``, gdy BigQuery przyjęło żądanie anulowania."""
        try:
            return bool(job.cancel())
        except (api_exceptions.GoogleAPIError, auth_exceptions.GoogleAuthError):
            return False

    def close(self) -> None:
        self._client.close()  # type: ignore[no-untyped-call]  # brak adnotacji w bibliotece


def _job_stats(job: bigquery.QueryJob, fetch_ns: int) -> JobStats:
    created, started, ended = job.created, job.started, job.ended
    if created is None or started is None or ended is None:
        # Zakończone zadanie zawsze ma znaczniki; brak oznacza niespójną odpowiedź API.
        raise WarehouseQueryError(
            "Finished job has no server timestamps (created/started/ended)", job_id=job.job_id
        )
    return JobStats(
        job_id=str(job.job_id),
        bytes_processed=int(job.total_bytes_processed or 0),
        bytes_billed=int(job.total_bytes_billed or 0),
        cache_hit=bool(job.cache_hit),
        created=_utc(created),
        started=_utc(started),
        ended=_utc(ended),
        result_fetch_ns=fetch_ns,
    )


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _reason(exc: api_exceptions.GoogleAPICallError) -> str | None:
    """Kod przyczyny BigQuery (``errors[0]["reason"]``), np. ``bytesBilledLimitExceeded``."""
    for error in exc.errors or ():
        if isinstance(error, dict) and isinstance(error.get("reason"), str):
            return str(error["reason"])
    return None


@contextmanager
def _mapped_errors(sql: str, *, job_id: str | None = None) -> Iterator[None]:
    """Zamienia wyjątki biblioteki Google na wyjątki bramki (tabela w docstringu modułu)."""
    try:
        yield
    except (
        auth_exceptions.DefaultCredentialsError,
        auth_exceptions.RefreshError,
        api_exceptions.Unauthenticated,
    ) as exc:
        raise WarehouseUnavailableError(f"BigQuery authentication failed: {exc}") from exc
    except api_exceptions.RetryError as exc:
        raise WarehouseUnavailableError(f"BigQuery unreachable (retries exhausted): {exc}") from exc
    except api_exceptions.GoogleAPICallError as exc:
        raise WarehouseQueryError(
            exc.message,
            sql=sql,
            job_id=job_id,
            reason=_reason(exc),
            status_code=int(exc.code) if exc.code is not None else None,
        ) from exc
