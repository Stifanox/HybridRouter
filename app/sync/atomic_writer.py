"""Atomowy zapis plików rollupu (ADR-003, ADR-005; dok. 03, Moduł 5).

Układ Hive z **niezmiennymi plikami wersjonowanymi**::

    {base_path}/{partition_key}=YYYY-MM-DD/rollup-<wersja>.parquet

Raz opublikowany plik nigdy nie jest nadpisywany – nowa wersja dnia dostaje nową nazwę
(numer wersji manifestu, w której powstała), a o tym, który plik jest aktualny, decyduje
``manifest.json`` (``app.sync.manifest``, ``app.sync.publisher``). Dzięki temu zapytanie
DuckDB, które otwiera plik po ścieżce więcej niż raz (metadane w fazie wiązania, dane w
fazie skanowania), zawsze czyta ten sam plik.

Każdy plik (Parquet i manifest) powstaje jako ``<nazwa>.tmp`` w katalogu docelowym, jest
zapisywany w całości, synchronizowany na dysk (``fsync``), zamykany i dopiero wtedy
przemianowywany przez ``os.replace`` – pod ostateczną nazwą nigdy nie istnieje plik
częściowy.

Kolumna partycji **nie** jest zapisywana w pliku – jej wartość niesie nazwa katalogu, a
``read_parquet(..., hive_partitioning = true)`` udostępnia ją jako kolumnę typu ``DATE``.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

TMP_SUFFIX = ".tmp"
COMPRESSION = "snappy"
ROLLUP_FILE_PATTERN = re.compile(r"rollup-(\d{8})\.parquet")
"""Nazwa pliku rollupu: ``rollup-`` + numer wersji manifestu (8 cyfr) + ``.parquet``."""


@dataclass(frozen=True, slots=True)
class WrittenPartition:
    """Zapisany plik dnia: dzień, ścieżka, liczba wierszy i rozmiar w bajtach."""

    day: dt.date
    path: Path
    rows: int
    size_bytes: int


def rollup_file_name(version: int) -> str:
    """Nazwa pliku rollupu dla wersji manifestu, np. ``rollup-00000042.parquet``."""
    if not 0 < version < 10**8:
        raise ValueError(f"rollup version out of range: {version}")
    return f"rollup-{version:08d}.parquet"


def partition_dir(base_path: str | Path, partition_key: str, day: dt.date) -> Path:
    """Katalog partycji Hive: ``{base_path}/{partition_key}=YYYY-MM-DD``."""
    return Path(base_path) / f"{partition_key}={day.isoformat()}"


def atomic_write(path: Path, write: Callable[[BinaryIO], object]) -> None:
    """Zapisuje plik przez ``<path>.tmp`` + ``fsync`` + ``os.replace``.

    Przy błędzie plik tymczasowy jest usuwany, a ewentualna poprzednia zawartość ``path``
    pozostaje nietknięta.
    """
    tmp_file = path.with_name(path.name + TMP_SUFFIX)
    try:
        with tmp_file.open("wb") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_file, path)
    except BaseException:
        tmp_file.unlink(missing_ok=True)
        raise


def fsync_dir(path: Path) -> None:
    """Utrwala wpisy katalogu (np. po ``os.replace``) – trwałość publikacji po awarii."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_partition(
    table: pa.Table, base_path: str | Path, partition_key: str, day: dt.date, version: int
) -> WrittenPartition:
    """Atomowo zapisuje **nowy** plik rollupu dnia w wersji ``version``.

    Jeśli ``table`` zawiera kolumnę ``partition_key``, wszystkie jej wartości muszą być równe
    ``day`` – kolumna jest usuwana przed zapisem.

    :raises FileExistsError: plik tej wersji już istnieje (pliki są niezmienne).
    :raises ValueError: kolumna partycji zawiera inną wartość niż ``day`` (lub ``NULL``).
    """
    if partition_key in table.column_names:
        _check_partition_values(table[partition_key], day, partition_key)
        table = table.drop_columns([partition_key])

    target_dir = partition_dir(base_path, partition_key, day)
    target_dir.mkdir(parents=True, exist_ok=True)
    final_file = target_dir / rollup_file_name(version)
    if final_file.exists():
        raise FileExistsError(f"immutable rollup file already exists: {final_file}")
    atomic_write(final_file, lambda handle: pq.write_table(table, handle, compression=COMPRESSION))
    return WrittenPartition(day, final_file, table.num_rows, final_file.stat().st_size)


def split_by_partition(table: pa.Table, partition_key: str) -> Iterator[tuple[dt.date, pa.Table]]:
    """Dzieli wynik zapytania rollupu na tabele dzienne (rosnąco po dniu).

    :raises KeyError: brak kolumny ``partition_key``.
    :raises ValueError: kolumna zawiera ``NULL`` lub wartości niebędące datą.
    """
    if partition_key not in table.column_names:
        raise KeyError(f"result has no partition column '{partition_key}'")
    column = table[partition_key]
    if column.null_count:
        raise ValueError(f"partition column '{partition_key}' contains NULL values")
    for day in sorted(pc.unique(column).to_pylist()):
        if not isinstance(day, dt.date) or isinstance(day, dt.datetime):
            raise ValueError(
                f"partition column '{partition_key}' must have DATE values, got {day!r}"
            )
        yield day, table.filter(pc.equal(column, pa.scalar(day, column.type)))


def _check_partition_values(column: pa.ChunkedArray, day: dt.date, partition_key: str) -> None:
    expected = pa.scalar(day, column.type)
    matches = pc.equal(column, expected)
    if column.null_count or not pc.all(matches).as_py():
        raise ValueError(
            f"table for partition {partition_key}={day.isoformat()} contains other "
            f"'{partition_key}' values"
        )
