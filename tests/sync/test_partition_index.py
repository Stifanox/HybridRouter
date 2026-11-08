"""Indeks dostępnych partycji: migawki z manifestu (Warunek 2)."""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pytest

from app.semantic.resolver import SemanticModel
from app.sync.manifest import ManifestError, manifest_path
from app.sync.materializer import MaterializationResult
from app.sync.partition_index import PartitionIndex, PartitionSnapshot
from tests.conftest import SYNTH_END, SYNTH_GAP, SYNTH_START
from tests.sync.helpers import publish_days

D = dt.date(2022, 12, 1)


def _day(offset: int) -> dt.date:
    return D + dt.timedelta(days=offset)


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_gap_in_synthetic_rollups(
    rollup_snapshot: PartitionSnapshot, synthetic_rollups: MaterializationResult
) -> None:
    assert rollup_snapshot.version == 1
    assert len(rollup_snapshot) == len(synthetic_rollups.partitions) == 11
    assert SYNTH_GAP not in rollup_snapshot
    assert rollup_snapshot.missing_days(SYNTH_START, SYNTH_END) == [SYNTH_GAP]
    assert rollup_snapshot.missing_days(SYNTH_START, SYNTH_GAP - dt.timedelta(days=1)) == []
    assert rollup_snapshot.count_missing(SYNTH_START, SYNTH_END) == 1


def test_files_for(tmp_path: Path) -> None:
    publish_days(tmp_path, [_day(0), _day(1), _day(3)])
    snapshot = PartitionIndex(tmp_path, "pickup_date").snapshot()
    assert snapshot.days == frozenset({_day(0), _day(1), _day(3)})
    assert snapshot.files_for((_day(1), _day(5))) == [
        (tmp_path / "pickup_date=2022-12-02/rollup-00000001.parquet").as_posix(),
        (tmp_path / "pickup_date=2022-12-04/rollup-00000001.parquet").as_posix(),
    ]
    assert len(snapshot.files_for(None)) == 3
    assert snapshot.files_for((_day(10), _day(11))) == []


def test_only_manifest_counts(tmp_path: Path) -> None:
    """Pliki spoza manifestu (osierocone, .tmp, ręcznie wgrane) nie są partycjami."""
    publish_days(tmp_path, [_day(0)])
    stray = tmp_path / "pickup_date=2022-12-02"
    stray.mkdir()
    (stray / "rollup-00000005.parquet").write_bytes(b"PAR1")
    (stray / "rollup.parquet").write_bytes(b"PAR1")
    assert PartitionIndex(tmp_path, "pickup_date").snapshot().days == frozenset({_day(0)})


def test_snapshot_is_stable_while_new_versions_are_published(tmp_path: Path) -> None:
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex(tmp_path, "pickup_date")
    first = index.snapshot()
    publish_days(tmp_path, [_day(0), _day(1)], value=2)
    second = index.snapshot()
    assert (first.version, len(first)) == (1, 1)  # migawka trwającego zapytania – bez zmian
    assert (second.version, len(second)) == (2, 2)
    assert first.files_for(None) != second.files_for(None)[:1]  # nowy plik dnia 0
    assert Path(first.files_for(None)[0]).exists()  # stary plik czeka na karencję


def test_check_interval_throttles_manifest_checks(tmp_path: Path) -> None:
    clock = FakeMonotonic()
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex(tmp_path, "pickup_date", check_interval=60, monotonic=clock)
    publish_days(tmp_path, [_day(1)], date_range=(_day(1), _day(1)))
    clock.now = 59.0
    assert index.snapshot().version == 1  # w oknie – bez sprawdzania
    clock.now = 60.0
    assert index.snapshot().version == 2
    assert index.current.version == 2


def test_zero_interval_checks_every_call_but_reloads_only_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex(tmp_path, "pickup_date")
    loads: list[int] = []
    original = index._load

    def counting_load() -> PartitionSnapshot:
        loads.append(1)
        return original()

    monkeypatch.setattr(index, "_load", counting_load)
    first = index.snapshot()
    assert index.snapshot() is first  # manifest niezmieniony – ta sama migawka
    assert loads == []
    publish_days(tmp_path, [_day(1)], date_range=(_day(1), _day(1)))
    assert index.snapshot().version == 2
    assert loads == [1]


def test_unpublished_cache_is_empty(tmp_path: Path) -> None:
    index = PartitionIndex(tmp_path / "not-there", "pickup_date")
    snapshot = index.snapshot()
    assert (snapshot.version, len(snapshot)) == (0, 0)
    assert snapshot.missing_days(_day(0), _day(1)) == [_day(0), _day(1)]


def test_scan_can_be_deferred(tmp_path: Path) -> None:
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex(tmp_path, "pickup_date", scan=False)
    assert len(index.current) == 0
    assert len(index.snapshot()) == 1


def test_missing_file_referenced_by_manifest_is_skipped(tmp_path: Path) -> None:
    result = publish_days(tmp_path, [_day(0), _day(1)])
    result.partitions[0].path.unlink()
    assert PartitionIndex(tmp_path, "pickup_date").snapshot().days == frozenset({_day(1)})


def test_corrupt_manifest_keeps_previous_snapshot(tmp_path: Path) -> None:
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex(tmp_path, "pickup_date")
    manifest_path(tmp_path).write_text("{broken")
    with pytest.raises(ManifestError):
        index.refresh()
    assert index.current.version == 1


def test_for_cache(tmp_path: Path, nyc_model: SemanticModel) -> None:
    cache = nyc_model.config.storage.local_cache.model_copy(update={"base_path": str(tmp_path)})
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex.for_cache(cache, check_interval=5)
    assert index.base_path == tmp_path
    assert index.partition_key == "pickup_date"
    assert index.check_interval == 5
    assert index.snapshot().days == frozenset({_day(0)})


def test_negative_check_interval_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="check_interval"):
        PartitionIndex(tmp_path, "pickup_date", check_interval=-1)


def test_concurrent_snapshots_during_publications(tmp_path: Path) -> None:
    publish_days(tmp_path, [_day(0)])
    index = PartitionIndex(tmp_path, "pickup_date")
    stop = threading.Event()
    errors: list[BaseException] = []
    versions: list[int] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                snapshot = index.snapshot()
                assert len(snapshot) >= 1
                versions.append(snapshot.version)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for i in range(30):
        publish_days(tmp_path, [_day(0)], value=i)
    stop.set()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors[0]
    assert max(versions) <= 31


def test_missing_days_limit() -> None:
    snapshot = PartitionSnapshot(Path("."), "pickup_date")
    huge = snapshot.missing_days(dt.date(1, 1, 1), dt.date.max, limit=3)
    assert huge == [dt.date(1, 1, 1), dt.date(1, 1, 2), dt.date(1, 1, 3)]
    assert snapshot.missing_days(_day(0), _day(5), limit=0) == []
    total = (dt.date.max - dt.date.min).days + 1
    assert snapshot.count_missing(dt.date(1, 1, 1), dt.date.max) == total


def test_missing_days_up_to_date_max() -> None:
    snapshot = PartitionSnapshot(Path("."), "pickup_date")
    last = dt.date.max
    previous = last - dt.timedelta(days=1)
    assert snapshot.missing_days(previous, last) == [previous, last]


@pytest.mark.parametrize("method", ["missing_days", "count_missing"])
def test_empty_range_rejected(method: str) -> None:
    snapshot = PartitionSnapshot(Path("."), "pickup_date")
    with pytest.raises(ValueError, match="empty date range"):
        getattr(snapshot, method)(_day(1), _day(0))


def test_negative_limit_rejected() -> None:
    with pytest.raises(ValueError, match="limit"):
        PartitionSnapshot(Path("."), "pickup_date").missing_days(_day(0), _day(1), limit=-1)
