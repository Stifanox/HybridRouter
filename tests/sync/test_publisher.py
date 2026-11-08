"""Publikacja rollupów: wersjonowanie, manifest, wycofywanie, GC, współbieżność."""

from __future__ import annotations

import datetime as dt
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from app.sync.manifest import read_manifest
from app.sync.partition_index import PartitionIndex
from app.sync.publisher import collect_garbage, publish_partitions
from tests.sync.helpers import day_table, publish_days

D1, D2, D3 = dt.date(2022, 12, 1), dt.date(2022, 12, 2), dt.date(2022, 12, 3)
T0 = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


class FakeClock:
    def __init__(self, now: dt.datetime = T0) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _files(base: Path) -> list[str]:
    return sorted(p.relative_to(base).as_posix() for p in base.glob("*=*/*"))


def test_first_publication(tmp_path: Path) -> None:
    result = publish_days(tmp_path, [D1, D2], clock=FakeClock())
    assert result.version == 1
    assert [p.day for p in result.partitions] == [D1, D2]
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None
    assert manifest.version == 1
    assert manifest.published_at == T0
    assert manifest.schema == (("x", "int64"),)
    assert {d: e.file for d, e in manifest.partitions.items()} == {
        D1: "pickup_date=2022-12-01/rollup-00000001.parquet",
        D2: "pickup_date=2022-12-02/rollup-00000001.parquet",
    }
    assert manifest.retired == ()
    assert result.removed_days == result.invalidated_days == result.collected_files == ()


def test_republish_creates_new_file_and_retires_old(tmp_path: Path) -> None:
    clock = FakeClock()
    publish_days(tmp_path, [D1, D2], clock=clock)
    clock.now = T0 + dt.timedelta(minutes=5)
    result = publish_days(tmp_path, [D2], value=2, clock=clock)
    assert result.version == 2
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None
    assert manifest.partitions[D1].file.endswith("rollup-00000001.parquet")  # bez zmian
    assert manifest.partitions[D2].file.endswith("rollup-00000002.parquet")
    assert [(r.file, r.retired_at) for r in manifest.retired] == [
        ("pickup_date=2022-12-02/rollup-00000001.parquet", clock.now)
    ]
    # Stary plik istnieje do końca okresu karencji (mogą go czytać trwające zapytania).
    assert "pickup_date=2022-12-02/rollup-00000001.parquet" in _files(tmp_path)


def test_retired_files_collected_only_after_retention(tmp_path: Path) -> None:
    clock = FakeClock()
    publish_days(tmp_path, [D1], clock=clock)
    clock.now = T0 + dt.timedelta(minutes=1)
    publish_days(tmp_path, [D1], value=2, clock=clock)  # v1 wycofany o T0+1 min
    old = tmp_path / "pickup_date=2022-12-01/rollup-00000001.parquet"

    clock.now = T0 + dt.timedelta(minutes=1) + HOUR - dt.timedelta(seconds=1)
    publish_days(tmp_path, [D2], clock=clock, date_range=(D2, D2))
    assert old.exists()

    clock.now = T0 + dt.timedelta(minutes=1) + HOUR
    result = publish_days(tmp_path, [D3], clock=clock, date_range=(D3, D3))
    assert result.collected_files == (old,)
    assert not old.exists()
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None and manifest.retired == ()


def test_days_without_data_in_range_are_removed(tmp_path: Path) -> None:
    clock = FakeClock()
    publish_days(tmp_path, [D1, D2, D3], clock=clock)
    result = publish_days(tmp_path, [D1], date_range=(D1, D2), clock=clock)
    assert result.removed_days == (D2,)
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None
    assert set(manifest.partitions) == {D1, D3}  # D3 poza zakresem – bez zmian
    assert "pickup_date=2022-12-02/rollup-00000001.parquet" in manifest.referenced_files


def test_empty_result_removes_whole_range(tmp_path: Path) -> None:
    publish_days(tmp_path, [D1, D2])
    result = publish_partitions(day_table([]), tmp_path, "pickup_date", (D1, D2))
    assert result.partitions == ()
    assert result.removed_days == (D1, D2)
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None and manifest.version == 2 and not manifest.partitions


def test_schema_change_invalidates_days_outside_range(tmp_path: Path) -> None:
    publish_days(tmp_path, [D1, D2, D3])
    wider = day_table([D2]).append_column("y", pa.array([5], pa.int64()))
    result = publish_days(tmp_path, [D2], table=wider)
    assert result.invalidated_days == (D1, D3)
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None
    assert set(manifest.partitions) == {D2}
    assert manifest.schema == (("x", "int64"), ("y", "int64"))


def test_result_outside_range_rejected_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        publish_partitions(day_table([D1, D3]), tmp_path, "pickup_date", (D1, D2))
    assert read_manifest(tmp_path, "pickup_date") is None
    assert _files(tmp_path) == []


@pytest.mark.parametrize(
    ("date_range", "retention", "match"),
    [((D2, D1), HOUR, "empty date range"), ((D1, D1), -HOUR, "retention")],
)
def test_invalid_arguments(
    tmp_path: Path, date_range: tuple[dt.date, dt.date], retention: dt.timedelta, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        publish_partitions(
            day_table([D1]), tmp_path, "pickup_date", date_range, retention=retention
        )


def test_orphans_collected_after_retention(tmp_path: Path) -> None:
    clock = FakeClock()
    publish_days(tmp_path, [D1], clock=clock)
    directory = tmp_path / "pickup_date=2022-12-01"
    stale_orphan = directory / "rollup-00000099.parquet"  # np. przerwana publikacja
    stale_tmp = directory / "rollup-00000098.parquet.tmp"
    fresh_orphan = directory / "rollup-00000097.parquet"
    foreign = directory / "notes.txt"
    for path in (stale_orphan, stale_tmp, fresh_orphan, foreign):
        path.write_bytes(b"x")
    old = (T0 - 2 * HOUR).timestamp()
    for path in (stale_orphan, stale_tmp, foreign):
        os.utime(path, (old, old))
    os.utime(fresh_orphan, (T0.timestamp(), T0.timestamp()))
    empty_dir = tmp_path / "pickup_date=2022-11-30"
    empty_dir.mkdir()

    collected = collect_garbage(tmp_path, "pickup_date", clock=clock)
    assert set(collected) == {stale_orphan, stale_tmp}
    assert fresh_orphan.exists() and foreign.exists()
    assert (directory / "rollup-00000001.parquet").exists()  # opublikowany – nietknięty
    assert not empty_dir.exists()


def test_collect_garbage_rewrites_manifest_without_new_version(tmp_path: Path) -> None:
    clock = FakeClock()
    publish_days(tmp_path, [D1], clock=clock)
    publish_days(tmp_path, [D1], value=2, clock=clock)
    clock.now = T0 + 2 * HOUR
    collected = collect_garbage(tmp_path, "pickup_date", clock=clock)
    assert [p.name for p in collected] == ["rollup-00000001.parquet"]
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None and manifest.version == 2 and manifest.retired == ()
    assert collect_garbage(tmp_path, "pickup_date", clock=clock) == ()


def test_collect_garbage_on_unpublished_cache(tmp_path: Path) -> None:
    assert collect_garbage(tmp_path / "missing", "pickup_date") == ()
    assert collect_garbage(tmp_path, "pickup_date") == ()
    with pytest.raises(ValueError, match="retention"):
        collect_garbage(tmp_path, "pickup_date", retention=-HOUR)


def test_concurrent_publishers_are_serialized(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: publish_days(tmp_path, [D1], value=i), range(16)))
    assert sorted(r.version for r in results) == list(range(1, 17))
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None and manifest.version == 16
    assert len(manifest.retired) == 15


def test_duckdb_reader_never_fails_during_republications(tmp_path: Path) -> None:
    """Rozwiązanie problemu z A4: zapytania DuckDB na plikach z migawki indeksu, wykonywane
    równolegle z wielokrotną ponowną publikacją tego samego dnia (pliki 1 000 / 20 000
    wierszy), nigdy nie kończą się błędem i zawsze zwracają wynik jednej wersji.

    Przy nadpisywaniu pliku pod stałą nazwą ten sam scenariusz dawał błąd w ok. 20%
    odczytów (notatka A4, „Napotkane problemy”)."""
    sizes = (1_000, 20_000)
    tables = {n: day_table([D1], value=n, rows_per_day=n) for n in sizes}
    publish_partitions(tables[sizes[0]], tmp_path, "pickup_date", (D1, D1))
    index = PartitionIndex(tmp_path, "pickup_date")
    valid = {(n, n * n) for n in sizes}  # (COUNT, SUM(x)) dla każdej wersji
    stop = threading.Event()
    results: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    def reader() -> None:
        connection = duckdb.connect()
        try:
            while not stop.is_set():
                files = index.snapshot().files_for(None)
                row = connection.execute(
                    "SELECT COUNT(*), SUM(x) FROM read_parquet($files, hive_partitioning = true)",
                    {"files": files},
                ).fetchone()
                assert row is not None
                results.append((row[0], row[1]))
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        for i in range(150):
            publish_partitions(tables[sizes[i % 2]], tmp_path, "pickup_date", (D1, D1))
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=60)
    assert not errors, errors[0]
    assert results
    assert set(results) <= valid


def test_already_deleted_retired_file_is_skipped(tmp_path: Path) -> None:
    clock = FakeClock()
    first = publish_days(tmp_path, [D1], clock=clock)
    publish_days(tmp_path, [D1], value=2, clock=clock)
    first.partitions[0].path.unlink()  # usunięty ręcznie przed końcem karencji
    clock.now = T0 + 2 * HOUR
    assert collect_garbage(tmp_path, "pickup_date", clock=clock) == ()
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None and manifest.retired == ()
