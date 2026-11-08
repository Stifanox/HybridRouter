"""Lokalne budowanie rollupów i wspólny krok materializacji."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.semantic.resolver import SemanticModel
from app.sync.local_builder import DuckDBArrowSource, build_local_rollups
from app.sync.manifest import read_manifest
from app.sync.materializer import MaterializationResult, materialize_rollups
from tests.conftest import RAW_TABLE, SYNTH_GAP, SYNTH_RANGE, SYNTH_START


def test_partitions_written_for_days_with_data(synthetic_rollups: MaterializationResult) -> None:
    days = [p.day for p in synthetic_rollups.partitions]
    assert SYNTH_GAP not in days
    assert len(days) == 11
    assert days == sorted(days)
    assert synthetic_rollups.date_range == SYNTH_RANGE
    assert synthetic_rollups.result_rows == sum(p.rows for p in synthetic_rollups.partitions)
    assert synthetic_rollups.total_bytes == sum(
        p.path.stat().st_size for p in synthetic_rollups.partitions
    )
    assert synthetic_rollups.days_written[0] == SYNTH_START.isoformat()
    assert synthetic_rollups.version == 1
    assert "FROM raw_trips" in synthetic_rollups.sql


def test_rollup_file_schema(synthetic_rollups: MaterializationResult) -> None:
    schema = pq.read_schema(synthetic_rollups.partitions[0].path)
    assert schema.names == [
        "vendor_id",
        "payment_type",
        "rate_code",
        "trips_count",
        "total_fare_amount",
        "total_tip_amount",
        "total_revenue",
        "total_distance_miles",
        "total_passenger_count",
        "total_duration_seconds",
    ]
    # SUM liczb całkowitych: HUGEINT w DuckDB -> int64 (jak INT64 w BigQuery).
    assert schema.field("total_passenger_count").type == pa.int64()
    assert schema.field("total_duration_seconds").type == pa.int64()
    assert schema.field("trips_count").type == pa.int64()
    assert schema.field("vendor_id").type == pa.string()


def test_rollup_is_much_smaller_than_raw(
    synthetic_raw: pa.Table, synthetic_rollups: MaterializationResult
) -> None:
    assert synthetic_rollups.result_rows < synthetic_raw.num_rows / 3


def test_rollup_preserves_totals(
    synthetic_raw: pa.Table, synthetic_rollups: MaterializationResult
) -> None:
    raw = duckdb.connect()
    raw.register("t", synthetic_raw)
    expected = raw.execute("SELECT COUNT(*), SUM(passenger_count) FROM t").fetchone()
    files = [str(p.path) for p in synthetic_rollups.partitions]
    actual = duckdb.execute(
        "SELECT SUM(trips_count), SUM(total_passenger_count) FROM read_parquet($files)",
        {"files": files},
    ).fetchone()
    assert actual == expected


def test_partial_range_and_rebuild(
    tmp_path: Path, synthetic_raw: pa.Table, nyc_model: SemanticModel
) -> None:
    first = (SYNTH_START, SYNTH_START + dt.timedelta(days=1))
    result = build_local_rollups(synthetic_raw, nyc_model.config, first, tmp_path)
    assert result.version == 1
    assert [p.day for p in result.partitions] == list(first)
    again = build_local_rollups(synthetic_raw, nyc_model.config, first, tmp_path)
    assert again.version == 2
    assert [p.path.name for p in again.partitions] == ["rollup-00000002.parquet"] * 2
    # Ta sama zawartość (deterministyczne zapytanie), nowy plik; stary czeka na karencję.
    assert pq.read_table(again.partitions[0].path).equals(pq.read_table(result.partitions[0].path))
    assert result.partitions[0].path.exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == [
        "pickup_date=2022-12-01",
        "pickup_date=2022-12-02",
    ]


def test_range_without_data_publishes_empty_manifest(
    tmp_path: Path, synthetic_raw: pa.Table, nyc_model: SemanticModel
) -> None:
    result = build_local_rollups(synthetic_raw, nyc_model.config, (SYNTH_GAP, SYNTH_GAP), tmp_path)
    assert result.partitions == ()
    assert result.result_rows == 0
    manifest = read_manifest(tmp_path, "pickup_date")
    assert manifest is not None and manifest.version == 1 and not manifest.partitions


def test_retention_and_clock_are_passed_through(
    tmp_path: Path, synthetic_raw: pa.Table, nyc_model: SemanticModel
) -> None:
    day = (SYNTH_START, SYNTH_START)
    t0 = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    first = build_local_rollups(synthetic_raw, nyc_model.config, day, tmp_path, clock=lambda: t0)
    second = build_local_rollups(
        synthetic_raw,
        nyc_model.config,
        day,
        tmp_path,
        retention=dt.timedelta(0),
        clock=lambda: t0,
    )
    assert second.publication.collected_files == (first.partitions[0].path,)


class _RecordingSource:
    """Źródło zastępcze – sprawdza oddzielenie „skąd wynik” od „jak zapisuję”."""

    dialect = "bigquery"
    source_table = "project.dataset.trips"

    def __init__(self, result: pa.Table) -> None:
        self.result = result
        self.queries: list[str] = []

    def fetch(self, sql: str) -> pa.Table:
        self.queries.append(sql)
        return self.result


def test_materialize_with_custom_source(tmp_path: Path, nyc_model: SemanticModel) -> None:
    result = pa.table(
        {
            "pickup_date": pa.array([SYNTH_START, SYNTH_START], pa.date32()),
            "vendor_id": ["1", "2"],
            "trips_count": pa.array([3, 4], pa.int64()),
        }
    )
    source = _RecordingSource(result)
    outcome = materialize_rollups(nyc_model.config, source, SYNTH_RANGE, tmp_path)
    assert len(source.queries) == 1
    assert "FROM project.dataset.trips" in source.queries[0]
    assert "TIMESTAMP_DIFF" in source.queries[0]  # dialekt źródła
    assert outcome.sql == source.queries[0]
    assert outcome.days_written == (SYNTH_START.isoformat(),)
    assert pq.read_table(outcome.partitions[0].path).num_rows == 2


def test_materialize_defaults_to_configured_base_path(
    tmp_path: Path, nyc_model: SemanticModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = pa.table({"pickup_date": pa.array([SYNTH_START], pa.date32()), "x": [1]})
    outcome = materialize_rollups(nyc_model.config, _RecordingSource(result), SYNTH_RANGE)
    expected = Path("data/nyc_taxi/pickup_date=2022-12-01/rollup-00000001.parquet")
    assert outcome.partitions[0].path == expected
    assert (tmp_path / expected).is_file()


def test_duckdb_arrow_source(synthetic_raw: pa.Table) -> None:
    source = DuckDBArrowSource(synthetic_raw)
    assert source.dialect == "duckdb"
    assert source.source_table == RAW_TABLE
    table = source.fetch(f"SELECT SUM(passenger_count) AS s, COUNT(*) AS c FROM {RAW_TABLE}")
    assert table.schema.field("s").type == pa.int64()  # decimal(38,0) -> int64
    assert table["c"][0].as_py() == synthetic_raw.num_rows
    custom = DuckDBArrowSource(synthetic_raw, "trips")
    assert custom.fetch("SELECT 1.5::DECIMAL(10,2) AS d FROM trips LIMIT 1").schema.field(
        "d"
    ).type == pa.decimal128(10, 2)
