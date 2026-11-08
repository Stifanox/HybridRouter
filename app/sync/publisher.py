"""Publikacja dziennych rollupów: niezmienne pliki + atomowa podmiana manifestu (ADR-005).

Przebieg jednej publikacji (pod wyłączną blokadą pliku ``.publish.lock`` – jeden pisarz
naraz):

1. odczyt bieżącego manifestu (wersja ``N``, brak pliku = wersja 0),
2. zapis **nowych** plików dni z wyniku zapytania jako ``rollup-<N+1>.parquet`` (żaden
   istniejący plik nie jest modyfikowany),
3. wyznaczenie nowego stanu:

   - dni z wyniku dostają nowe pliki (poprzednie pliki trafiają do ``retired``),
   - dni z zakresu publikacji **nieobecne w wyniku** są usuwane z manifestu (źródło nie ma
     już dla nich danych – router potraktuje je jako brakujące),
   - przy **zmianie schematu** rollupu dni spoza zakresu publikacji są usuwane z manifestu
     (nie wolno łączyć w jednym zapytaniu plików o różnych schematach; zapytania o te dni
     trafią do BigQuery do czasu ich ponownej synchronizacji),

4. atomowa podmiana ``manifest.json`` (wersja ``N+1``) – **moment publikacji**,
5. sprzątanie (GC): usunięcie plików wycofanych dawniej niż ``retention`` temu oraz
   plików osieroconych (niepublikowanych, np. po awarii w trakcie kroku 2) starszych niż
   ``retention``.

Okres karencji ``retention`` musi być dłuższy niż czas życia migawki indeksu po stronie
aplikacji: maksymalny czas zapytania + interwał sprawdzania manifestu
(``PartitionIndex.check_interval``). Zapytanie, które pobrało migawkę tuż przed publikacją,
czyta wtedy wciąż istniejące pliki.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pyarrow as pa

from app.planner.logical_plan import DateRange
from app.sync.atomic_writer import (
    ROLLUP_FILE_PATTERN,
    TMP_SUFFIX,
    WrittenPartition,
    split_by_partition,
    write_partition,
)
from app.sync.manifest import (
    Manifest,
    PartitionEntry,
    RetiredFile,
    read_manifest,
    schema_fingerprint,
    write_manifest,
)

logger = logging.getLogger(__name__)

DEFAULT_RETENTION = dt.timedelta(hours=1)
"""Domyślny okres karencji usuwania wycofanych plików (z dużym zapasem wobec czasu zapytań
DuckDB na rollupach – milisekundy – i interwału odświeżania indeksu)."""
LOCK_FILE_NAME = ".publish.lock"

Clock = Callable[[], dt.datetime]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """Wynik publikacji."""

    version: int
    manifest_path: Path
    partitions: tuple[WrittenPartition, ...]
    removed_days: tuple[dt.date, ...]
    """Dni z zakresu publikacji, których nie było w wyniku – usunięte z manifestu."""
    invalidated_days: tuple[dt.date, ...]
    """Dni spoza zakresu usunięte z powodu zmiany schematu rollupu."""
    collected_files: tuple[Path, ...]
    """Pliki usunięte przez GC w tej publikacji."""


def publish_partitions(
    table: pa.Table,
    base_path: str | Path,
    partition_key: str,
    date_range: DateRange,
    *,
    retention: dt.timedelta = DEFAULT_RETENTION,
    clock: Clock = utc_now,
) -> PublicationResult:
    """Publikuje wynik zapytania rollupu dla dni ``date_range`` jako nową wersję manifestu.

    :raises ValueError: wynik zawiera dzień spoza ``date_range`` lub zakres jest pusty.
    :raises ManifestError: istniejący manifest jest uszkodzony.
    """
    start, end = date_range
    if start > end:
        raise ValueError(f"empty date range: {start.isoformat()} > {end.isoformat()}")
    _check_retention(retention)
    base = Path(base_path)
    base.mkdir(parents=True, exist_ok=True)

    with _publication_lock(base):
        current = read_manifest(base, partition_key) or Manifest.empty(partition_key)
        version = current.version + 1
        days = list(split_by_partition(table, partition_key))
        outside = [day for day, _ in days if not start <= day <= end]
        if outside:
            raise ValueError(f"result contains days outside {start}..{end}: {outside[:5]}")

        written = tuple(
            write_partition(day_table, base, partition_key, day, version) for day, day_table in days
        )
        now = clock()
        fingerprint = schema_fingerprint(_data_schema(table, partition_key))
        partitions = dict(current.partitions)
        retired = list(current.retired)

        def retire(day: dt.date) -> None:
            retired.append(RetiredFile(partitions.pop(day).file, now))

        invalidated: list[dt.date] = []
        if current.schema and current.schema != fingerprint:
            invalidated = sorted(d for d in partitions if not start <= d <= end)
            for day in invalidated:
                retire(day)
            logger.warning(
                "rollup schema changed; %d partition(s) outside %s..%s invalidated",
                len(invalidated),
                start,
                end,
            )

        new_days = {w.day for w in written}
        removed = sorted(d for d in partitions if start <= d <= end and d not in new_days)
        for day in [*removed, *(w.day for w in written if w.day in partitions)]:
            retire(day)
        for w in written:
            partitions[w.day] = PartitionEntry(_relative_to(w.path, base), w.rows, w.size_bytes)

        expired, kept = _split_expired(retired, now - retention)
        manifest = Manifest(
            version=version,
            partition_key=partition_key,
            published_at=now,
            schema=fingerprint,
            partitions=MappingProxyType(partitions),
            retired=tuple(kept),
        )
        path = write_manifest(base, manifest)
        collected = _delete(base, expired) + _collect_orphans(base, manifest, now - retention)

    return PublicationResult(
        version=version,
        manifest_path=path,
        partitions=written,
        removed_days=tuple(removed),
        invalidated_days=tuple(invalidated),
        collected_files=tuple(collected),
    )


def collect_garbage(
    base_path: str | Path,
    partition_key: str,
    *,
    retention: dt.timedelta = DEFAULT_RETENTION,
    clock: Clock = utc_now,
) -> tuple[Path, ...]:
    """Samodzielne sprzątanie (bez publikacji danych): usuwa wycofane pliki starsze niż
    ``retention`` i pliki osierocone. Zapisuje manifest tylko wtedy, gdy zmieniła się lista
    ``retired`` (bez zmiany wersji – stan danych się nie zmienia)."""
    _check_retention(retention)
    base = Path(base_path)
    if not base.is_dir():
        return ()
    with _publication_lock(base):
        current = read_manifest(base, partition_key)
        if current is None:
            return ()
        now = clock()
        expired, kept = _split_expired(list(current.retired), now - retention)
        manifest = current
        if expired:
            manifest = Manifest(
                version=current.version,
                partition_key=current.partition_key,
                published_at=current.published_at,
                schema=current.schema,
                partitions=current.partitions,
                retired=tuple(kept),
            )
            write_manifest(base, manifest)
        return tuple(_delete(base, expired) + _collect_orphans(base, manifest, now - retention))


# --------------------------------------------------------------------------- #
# Elementy pomocnicze
# --------------------------------------------------------------------------- #


@contextmanager
def _publication_lock(base: Path) -> Iterator[None]:
    """Wyłączna blokada ``flock`` – drugi pisarz czeka (POSIX; system docelowy: Linux)."""
    with (base / LOCK_FILE_NAME).open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_retention(retention: dt.timedelta) -> None:
    if retention < dt.timedelta(0):
        raise ValueError("retention must not be negative")


def _data_schema(table: pa.Table, partition_key: str) -> pa.Schema:
    schema: pa.Schema = table.schema
    index = schema.get_field_index(partition_key)
    return schema.remove(index) if index >= 0 else schema


def _relative_to(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _split_expired(
    retired: Sequence[RetiredFile], deadline: dt.datetime
) -> tuple[list[RetiredFile], list[RetiredFile]]:
    expired = [r for r in retired if r.retired_at <= deadline]
    kept = [r for r in retired if r.retired_at > deadline]
    return expired, kept


def _delete(base: Path, files: Sequence[RetiredFile]) -> list[Path]:
    deleted: list[Path] = []
    for retired in files:
        path = base / retired.file
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(path)
        _remove_if_empty(path.parent)
    return deleted


def _collect_orphans(base: Path, manifest: Manifest, deadline: dt.datetime) -> list[Path]:
    """Pliki rollupu i ``.tmp`` w katalogach partycji, których manifest nie zna (np. zapis
    przerwany awarią), starsze niż ``deadline``."""
    referenced = manifest.referenced_files
    cutoff = deadline.timestamp()
    deleted: list[Path] = []
    prefix = f"{manifest.partition_key}="
    for directory in base.iterdir():
        if not directory.is_dir() or not directory.name.startswith(prefix):
            continue
        for path in directory.iterdir():
            is_candidate = ROLLUP_FILE_PATTERN.fullmatch(path.name) or path.name.endswith(
                TMP_SUFFIX
            )
            if not is_candidate or not path.is_file():
                continue
            if path.relative_to(base).as_posix() in referenced:
                continue
            if path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
                deleted.append(path)
        _remove_if_empty(directory)
    return deleted


def _remove_if_empty(directory: Path) -> None:
    with suppress(OSError):  # katalog niepusty (lub już usunięty)
        directory.rmdir()
