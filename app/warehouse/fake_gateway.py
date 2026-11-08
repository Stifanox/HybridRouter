"""``FakeBigQueryGateway`` – atrapa hurtowni: DuckDB na surowych danych syntetycznych (ADR-014).

Atrapa pozwala uruchomić całą ścieżkę hurtowni (strażnik kosztów, wykonawca, API,
synchronizator) bez konta GCP. Przyjmuje SQL **w dialekcie BigQuery** – dokładnie ten, który
system wysłałby do BigQuery (``RawTableRenderer(...).render(plan, "bigquery")``) – i:

1. parsuje go ``sqlglot.parse_one(sql, read="bigquery")``,
2. w drzewie podmienia węzeł tabeli ``dataset_table`` (z YAML) na zarejestrowaną w DuckDB
   tabelę surową :data:`RAW_TABLE_NAME` (odwołanie do innej tabeli = błąd „Not found”, jak w
   BigQuery),
3. generuje SQL ``dialect="duckdb"`` i wykonuje go.

**Wyjątek od ADR-008:** to JEDYNE dozwolone miejsce w projekcie, w którym transpilowany jest
pełny tekst zapytania. Dotyczy wyłącznie dublera testowego – ścieżka produkcyjna generuje SQL
z planu logicznego bezpośrednio w dialekcie docelowym.

Dry-run zwraca deterministyczną liczbę bajtów. Domyślne oszacowanie (:meth:`estimate_bytes`):
``wiersze źródła spełniające WHERE × liczba użytych kolumn źródła × 8 B`` – naśladuje dwie
własności BigQuery istotne dla strażnika kosztów: koszt zależy od liczby czytanych kolumn
(format kolumnowy) i od zakresu danych (przycinanie partycji, tu z dokładnością do wiersza).
W testach można wstrzyknąć stałą albo funkcję ``sql -> bajty`` (``bytes_estimator``).

Statystyki zadania są syntetyczne, ale spójne: ``created`` = chwila przyjęcia zadania (UTC),
``started`` = ``created`` (brak kolejki), ``ended`` = ``started`` + zmierzony czas wykonania
w DuckDB (z opcjonalnym sztucznym opóźnieniem ``execution_delay_s``). ``bytes_billed``
naśladuje rozliczenie on-demand: zaokrąglenie w górę do 1 MB i minimum 10 MB.
``maximum_bytes_billed`` jest egzekwowany jak w BigQuery (``bytesBilledLimitExceeded``).

Sztuczne opóźnienie dłuższe niż ``timeout_s`` powoduje odczekanie ``timeout_s``, wpis
identyfikatora zadania do rejestru :attr:`cancelled_jobs` i :class:`WarehouseTimeoutError`
(``cancelled=True``) – bez wykonywania zapytania.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.warehouse.cost_guard import MIN_BYTES_BILLED
from app.warehouse.gateway import (
    DryRunResult,
    JobStats,
    WarehouseQueryError,
    WarehouseRunResult,
    WarehouseTimeoutError,
)

RAW_TABLE_NAME = "raw_trips"
"""Nazwa tabeli surowej w bazie DuckDB atrapy."""

BYTES_PER_VALUE = 8
"""Przyjęty rozmiar jednej wartości kolumny w oszacowaniu dry-runu (INT64/FLOAT64/TIMESTAMP)."""

BILLING_ROUNDING = 1024**2
"""BigQuery zaokrągla rozliczane bajty w górę do 1 MB."""

BytesEstimator = int | Callable[[str], int]
"""Stała liczba bajtów albo funkcja ``sql (BigQuery) -> bajty``."""


@dataclass(frozen=True, slots=True)
class FakeCall:
    """Wpis historii wywołań atrapy (asercje w testach)."""

    kind: Literal["dry_run", "run"]
    sql: str
    location: str
    maximum_bytes_billed: int | None = None
    use_query_cache: bool | None = None
    timeout_s: float | None = None


class FakeBigQueryGateway:
    """Atrapa ``WarehouseGateway`` na DuckDB (bezpieczna wątkowo)."""

    def __init__(
        self,
        raw: pa.Table | str | Path,
        *,
        dataset_table: str,
        bytes_estimator: BytesEstimator | None = None,
        execution_delay_s: float = 0.0,
    ) -> None:
        """:param raw: surowa tabela ``pyarrow`` albo plik / katalog plików Parquet
            (``HYBRID_ROUTER_FAKE_RAW_PATH``) o schemacie tabeli BigQuery.
        :param dataset_table: nazwa tabeli w SQL BigQuery (``storage.bigquery.dataset_table``)
            podmieniana na dane surowe.
        :param bytes_estimator: wynik dry-runu – stała lub funkcja ``sql -> bajty``;
            domyślnie :meth:`estimate_bytes`.
        :param execution_delay_s: sztuczny czas wykonania zadania (testy limitu czasu).
        :raises FileNotFoundError: wskazany plik / katalog nie istnieje.
        """
        if execution_delay_s < 0:
            raise ValueError("execution_delay_s must be >= 0")
        self.dataset_table = dataset_table
        self._target = _table_key(exp.to_table(dataset_table, dialect="bigquery"))
        self.bytes_estimator = bytes_estimator
        self.execution_delay_s = execution_delay_s
        self._connection = duckdb.connect(":memory:")
        self._load(raw)
        self._columns = frozenset(
            name.lower()
            for (name,) in self._connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [RAW_TABLE_NAME],
            ).fetchall()
        )
        self._lock = threading.Lock()
        self._closed = False
        self._job_counter = itertools.count(1)
        self._calls: list[FakeCall] = []
        self._cancelled: list[str] = []

    def _load(self, raw: pa.Table | str | Path) -> None:
        if isinstance(raw, pa.Table):
            self._connection.register("raw_source", raw)
            self._connection.execute(f"CREATE TABLE {RAW_TABLE_NAME} AS SELECT * FROM raw_source")
            self._connection.unregister("raw_source")
            return
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"Fake BigQuery raw data not found: {path}")
        pattern = str(path / "**" / "*.parquet") if path.is_dir() else str(path)
        self._connection.execute(
            f"CREATE TABLE {RAW_TABLE_NAME} AS SELECT * FROM read_parquet(?)", [pattern]
        )

    # ------------------------------------------------------------- historia
    @property
    def calls(self) -> list[FakeCall]:
        """Kopia historii wywołań (``dry_run`` i ``run``) w kolejności."""
        with self._lock:
            return list(self._calls)

    @property
    def cancelled_jobs(self) -> list[str]:
        """Identyfikatory zadań anulowanych po przekroczeniu limitu czasu."""
        with self._lock:
            return list(self._cancelled)

    # ---------------------------------------------------------- transpilacja
    def to_duckdb(self, sql: str) -> str:
        """SQL BigQuery -> SQL DuckDB z podmienioną tabelą (jedyna transpilacja, ADR-014).

        :raises WarehouseQueryError: błąd składni lub odwołanie do nieznanej tabeli.
        """
        return self._translate(sql).sql(dialect="duckdb")

    def _translate(self, sql: str) -> exp.Expr:
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except SqlglotError as exc:
            raise WarehouseQueryError(
                f"Syntax error: {exc}", sql=sql, reason="invalidQuery", status_code=400
            ) from exc
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table in list(tree.find_all(exp.Table)):
            key = _table_key(table)
            if key == self._target:
                replacement = exp.to_table(RAW_TABLE_NAME)
                if table.alias:
                    replacement.set("alias", table.args["alias"].copy())
                table.replace(replacement)
            elif not (len(key) == 1 and key[0].lower() in ctes):
                raise WarehouseQueryError(
                    f"Not found: Table {'.'.join(key)}",
                    sql=sql,
                    reason="notFound",
                    status_code=404,
                )
        return tree

    # --------------------------------------------------------------- dry-run
    def estimate_bytes(self, sql: str) -> int:
        """Domyślne oszacowanie bajtów: wiersze spełniające ``WHERE`` zapytania głównego ×
        liczba różnych kolumn źródła użytych w zapytaniu × :data:`BYTES_PER_VALUE`.

        Dla zapytań bez ``WHERE`` liczone są wszystkie wiersze. Podzapytania i CTE nie są
        analizowane osobno (oszacowanie zgrubne – wystarczające dla testów strażnika).
        """
        tree = self._translate(sql)
        columns = {
            column.name.lower()
            for column in tree.find_all(exp.Column)
            if column.name.lower() in self._columns
        }
        if not columns:
            return 0
        if isinstance(tree, exp.Select):
            # To samo FROM (z aliasami) i WHERE; bez agregacji, grupowania i limitu.
            count = tree.copy()
            for clause in ("group", "having", "qualify", "order", "limit", "offset", "distinct"):
                count.set(clause, None)
            count.set("expressions", [exp.Count(this=exp.Star())])
        else:
            count = exp.select(exp.Count(this=exp.Star())).from_(RAW_TABLE_NAME)
        rows = self._fetch_one_int(count.sql(dialect="duckdb"), sql)
        return rows * len(columns) * BYTES_PER_VALUE

    def dry_run(self, sql: str, *, location: str) -> DryRunResult:
        started = time.perf_counter_ns()
        self._record(FakeCall("dry_run", sql, location))
        bytes_processed = self._bytes(sql)
        return DryRunResult(bytes_processed, time.perf_counter_ns() - started)

    def _bytes(self, sql: str) -> int:
        estimator = self.bytes_estimator
        if estimator is None:
            return self.estimate_bytes(sql)
        self._translate(sql)  # walidacja jak w BigQuery: błędny SQL nie przechodzi dry-runu
        value = estimator if isinstance(estimator, int) else estimator(sql)
        if value < 0:
            raise ValueError(f"bytes_estimator returned a negative value: {value}")
        return value

    # ------------------------------------------------------------------- run
    def run(
        self,
        sql: str,
        *,
        location: str,
        maximum_bytes_billed: int | None,
        use_query_cache: bool,
        timeout_s: float,
    ) -> WarehouseRunResult:
        self._record(
            FakeCall("run", sql, location, maximum_bytes_billed, use_query_cache, timeout_s)
        )
        job_id = f"fake_job_{next(self._job_counter):06d}"
        duckdb_sql = self.to_duckdb(sql)
        bytes_processed = self._bytes(sql)
        bytes_billed = billed_bytes(bytes_processed)
        if maximum_bytes_billed is not None and bytes_billed > maximum_bytes_billed:
            raise WarehouseQueryError(
                f"Query exceeded limit for bytes billed: {maximum_bytes_billed}. "
                f"{bytes_billed} or higher required.",
                sql=sql,
                job_id=job_id,
                reason="bytesBilledLimitExceeded",
                status_code=400,
            )

        created = dt.datetime.now(dt.UTC)
        clock_start = time.perf_counter_ns()
        if self.execution_delay_s > timeout_s:
            time.sleep(timeout_s)
            with self._lock:
                self._cancelled.append(job_id)
            raise WarehouseTimeoutError(timeout_s, job_id=job_id, cancelled=True)
        if self.execution_delay_s:
            time.sleep(self.execution_delay_s)
        table = self._execute(duckdb_sql, sql, job_id)
        execution_ns = time.perf_counter_ns() - clock_start

        fetch_started = time.perf_counter_ns()
        table = integer_decimals_to_int64(table)
        fetch_ns = time.perf_counter_ns() - fetch_started

        ended = created + dt.timedelta(microseconds=execution_ns // 1000)
        stats = JobStats(
            job_id=job_id,
            bytes_processed=bytes_processed,
            bytes_billed=bytes_billed,
            cache_hit=False,
            created=created,
            started=created,
            ended=ended,
            result_fetch_ns=fetch_ns,
        )
        return WarehouseRunResult(table, stats)

    def _execute(self, duckdb_sql: str, sql: str, job_id: str) -> pa.Table:
        cursor = self._cursor()
        try:
            table: pa.Table = cursor.sql(duckdb_sql).to_arrow_table()
        except duckdb.Error as exc:
            raise WarehouseQueryError(
                str(exc), sql=sql, job_id=job_id, reason="invalidQuery", status_code=400
            ) from exc
        finally:
            cursor.close()
        return table

    def _fetch_one_int(self, duckdb_sql: str, sql: str) -> int:
        cursor = self._cursor()
        try:
            row = cursor.execute(duckdb_sql).fetchone()
        except duckdb.Error as exc:
            raise WarehouseQueryError(
                str(exc), sql=sql, reason="invalidQuery", status_code=400
            ) from exc
        finally:
            cursor.close()
        return int(row[0]) if row is not None else 0

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._closed:
                raise RuntimeError("FakeBigQueryGateway is closed")
            return self._connection.cursor()

    def _record(self, call: FakeCall) -> None:
        with self._lock:
            self._calls.append(call)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()


def billed_bytes(bytes_processed: int) -> int:
    """Bajty rozliczane on-demand: zaokrąglenie w górę do 1 MB, minimum 10 MB (jedna tabela)."""
    rounded = math.ceil(bytes_processed / BILLING_ROUNDING) * BILLING_ROUNDING
    return max(rounded, MIN_BYTES_BILLED)


def integer_decimals_to_int64(table: pa.Table) -> pa.Table:
    """``SUM`` liczb całkowitych w DuckDB daje ``HUGEINT`` (Arrow: ``decimal128(38, 0)``), w
    BigQuery – ``INT64``; kolumny ``decimal(p, 0)`` są rzutowane na ``int64`` (rzutowanie
    bezpieczne), żeby wynik atrapy miał typy wyniku BigQuery. Odpowiednik funkcji
    prywatnej ``app.sync.local_builder`` – powielony, by bramka nie zależała od ``app.sync``."""
    for position, field in enumerate(table.schema):
        if pa.types.is_decimal(field.type) and field.type.scale == 0:
            column = pc.cast(table[position], pa.int64(), safe=True)
            table = table.set_column(position, pa.field(field.name, pa.int64()), column)
    return table


def _table_key(table: exp.Table) -> tuple[str, ...]:
    """Pełna nazwa tabeli jako krotka części (``projekt``, ``zbiór``, ``tabela``)."""
    return tuple(part.name for part in table.parts)
