"""Atomowy zapis niezmiennych plików rollupu (ADR-005)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import BinaryIO

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.sync.atomic_writer import (
    TMP_SUFFIX,
    atomic_write,
    partition_dir,
    rollup_file_name,
    split_by_partition,
    write_partition,
)

DAY = dt.date(2022, 12, 5)


def _table(rows: int, with_key: bool = False, day: dt.date = DAY) -> pa.Table:
    data: dict[str, pa.Array] = {
        "vendor_id": pa.array([str(i % 3) for i in range(rows)]),
        "trips_count": pa.array(range(rows), pa.int64()),
    }
    if with_key:
        data = {"pickup_date": pa.array([day] * rows, pa.date32()), **data}
    return pa.table(data)


def test_rollup_file_name() -> None:
    assert rollup_file_name(42) == "rollup-00000042.parquet"
    for invalid in (0, -1, 10**8):
        with pytest.raises(ValueError):
            rollup_file_name(invalid)


def test_writes_versioned_file_without_partition_column(tmp_path: Path) -> None:
    written = write_partition(_table(5, with_key=True), tmp_path, "pickup_date", DAY, 3)
    assert written.path == tmp_path / "pickup_date=2022-12-05" / "rollup-00000003.parquet"
    assert written.day == DAY
    assert written.rows == 5
    assert written.size_bytes == written.path.stat().st_size > 0
    assert pq.read_table(written.path).column_names == ["vendor_id", "trips_count"]
    assert pq.ParquetFile(written.path).metadata.row_group(0).column(0).compression == "SNAPPY"


def test_files_are_immutable(tmp_path: Path) -> None:
    write_partition(_table(5), tmp_path, "pickup_date", DAY, 1)
    with pytest.raises(FileExistsError, match="immutable"):
        write_partition(_table(7), tmp_path, "pickup_date", DAY, 1)
    second = write_partition(_table(7), tmp_path, "pickup_date", DAY, 2)
    directory = partition_dir(tmp_path, "pickup_date", DAY)
    assert sorted(p.name for p in directory.iterdir()) == [
        "rollup-00000001.parquet",
        "rollup-00000002.parquet",
    ]  # brak plików .tmp, stara wersja nietknięta
    assert pq.read_table(directory / "rollup-00000001.parquet").num_rows == 5
    assert pq.read_table(second.path).num_rows == 7


def test_partition_column_restored_by_hive_partitioning(tmp_path: Path) -> None:
    written = write_partition(_table(3, with_key=True), tmp_path, "pickup_date", DAY, 1)
    rows = duckdb.execute(
        "SELECT DISTINCT pickup_date, typeof(pickup_date) FROM "
        f"read_parquet(['{written.path}'], hive_partitioning = true)"
    ).fetchall()
    assert rows == [(DAY, "DATE")]


def test_partition_column_must_match_day(tmp_path: Path) -> None:
    table = _table(3, with_key=True, day=DAY + dt.timedelta(days=1))
    with pytest.raises(ValueError, match="other 'pickup_date' values"):
        write_partition(table, tmp_path, "pickup_date", DAY, 1)
    assert not partition_dir(tmp_path, "pickup_date", DAY).exists()


def test_atomic_write_failure_keeps_previous_content_and_removes_tmp(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    atomic_write(target, lambda handle: handle.write(b"old"))

    def broken(handle: BinaryIO) -> None:
        handle.write(b"partial")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, broken)
    assert target.read_bytes() == b"old"
    assert not target.with_name(target.name + TMP_SUFFIX).exists()


def test_failed_parquet_write_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_write(table: pa.Table, where: BinaryIO, **kwargs: object) -> None:
        where.write(b"PAR1 partial garbage")
        raise OSError("disk full")

    monkeypatch.setattr(pq, "write_table", broken_write)
    with pytest.raises(OSError, match="disk full"):
        write_partition(_table(9), tmp_path, "pickup_date", DAY, 1)
    assert list(partition_dir(tmp_path, "pickup_date", DAY).iterdir()) == []


def test_split_by_partition() -> None:
    days = [DAY, DAY + dt.timedelta(days=2), DAY]
    table = pa.table({"pickup_date": pa.array(days, pa.date32()), "x": [1, 2, 3]})
    parts = list(split_by_partition(table, "pickup_date"))
    assert [day for day, _ in parts] == [DAY, DAY + dt.timedelta(days=2)]
    assert parts[0][1]["x"].to_pylist() == [1, 3]
    assert parts[1][1]["x"].to_pylist() == [2]


def test_split_by_partition_errors() -> None:
    with pytest.raises(KeyError):
        list(split_by_partition(pa.table({"x": [1]}), "pickup_date"))
    with_null = pa.table({"pickup_date": pa.array([DAY, None], pa.date32())})
    with pytest.raises(ValueError, match="NULL"):
        list(split_by_partition(with_null, "pickup_date"))
    as_text = pa.table({"pickup_date": ["2022-12-05"]})
    with pytest.raises(ValueError, match="DATE values"):
        list(split_by_partition(as_text, "pickup_date"))
