"""Manifest cache: serializacja, walidacja, atomowy zapis."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyarrow as pa
import pytest

from app.sync.manifest import (
    MANIFEST_FILE_NAME,
    Manifest,
    ManifestError,
    PartitionEntry,
    RetiredFile,
    manifest_path,
    read_manifest,
    schema_fingerprint,
    write_manifest,
)

NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)
DAY = dt.date(2022, 12, 1)


def _manifest() -> Manifest:
    return Manifest(
        version=7,
        partition_key="pickup_date",
        published_at=NOW,
        schema=(("vendor_id", "string"), ("trips_count", "int64")),
        partitions=MappingProxyType(
            {DAY: PartitionEntry("pickup_date=2022-12-01/rollup-00000007.parquet", 32, 4400)}
        ),
        retired=(RetiredFile("pickup_date=2022-12-01/rollup-00000006.parquet", NOW),),
    )


def test_round_trip(tmp_path: Path) -> None:
    manifest = _manifest()
    path = write_manifest(tmp_path, manifest)
    assert path == manifest_path(tmp_path) == tmp_path / MANIFEST_FILE_NAME
    assert not (tmp_path / (MANIFEST_FILE_NAME + ".tmp")).exists()
    loaded = read_manifest(tmp_path, "pickup_date")
    assert loaded == manifest
    assert json.loads(path.read_text())["partitions"]["2022-12-01"]["rows"] == 32


def test_missing_manifest_means_unpublished(tmp_path: Path) -> None:
    assert read_manifest(tmp_path, "pickup_date") is None
    empty = Manifest.empty("pickup_date")
    assert empty.version == 0 and not empty.partitions and empty.referenced_files == frozenset()


def test_referenced_files_include_retired() -> None:
    assert _manifest().referenced_files == {
        "pickup_date=2022-12-01/rollup-00000007.parquet",
        "pickup_date=2022-12-01/rollup-00000006.parquet",
    }


def test_partition_key_mismatch(tmp_path: Path) -> None:
    write_manifest(tmp_path, _manifest())
    with pytest.raises(ManifestError, match="does not match 'day'"):
        read_manifest(tmp_path, "day")


def _corrupt(data: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**data, **changes}


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"format_version": 2}, "unsupported format_version"),
        ({"version": -1}, "invalid version"),
        ({"version": True}, "invalid version"),
        ({"partitions": {"2022-13-01": {"file": "a", "rows": 1, "size_bytes": 1}}}, "structure"),
        (
            {"partitions": {"2022-12-01": {"file": "/abs/x.parquet", "rows": 1, "size_bytes": 1}}},
            "relative",
        ),
        (
            {"partitions": {"2022-12-01": {"file": "../x.parquet", "rows": 1, "size_bytes": 1}}},
            "relative",
        ),
        ({"retired": [{"file": "a", "retired_at": "2026-11-08T12:00:00"}]}, "time zone"),
        ({"schema": 5}, "structure"),
    ],
)
def test_invalid_manifest_rejected(tmp_path: Path, changes: dict[str, Any], match: str) -> None:
    data = _corrupt(_manifest().to_json(), **changes)
    manifest_path(tmp_path).write_text(json.dumps(data))
    with pytest.raises(ManifestError, match=match):
        read_manifest(tmp_path, "pickup_date")


def test_missing_key_and_invalid_json(tmp_path: Path) -> None:
    data = _manifest().to_json()
    del data["partitions"]
    manifest_path(tmp_path).write_text(json.dumps(data))
    with pytest.raises(ManifestError, match="structure"):
        read_manifest(tmp_path, "pickup_date")
    manifest_path(tmp_path).write_text("{not json")
    with pytest.raises(ManifestError, match="invalid JSON"):
        read_manifest(tmp_path, "pickup_date")


def test_schema_fingerprint() -> None:
    schema = pa.schema([("vendor_id", pa.string()), ("n", pa.int64())])
    assert schema_fingerprint(schema) == (("vendor_id", "string"), ("n", "int64"))
