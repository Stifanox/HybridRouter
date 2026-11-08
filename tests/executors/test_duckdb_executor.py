"""Wykonawca DuckDB: wynik, pomiar czasu, błędy, współbieżność."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pytest

from app.executors.base import (
    ExecutionOptions,
    ExecutionResult,
    QueryExecutionError,
    QueryExecutor,
)
from app.executors.duckdb_executor import DuckDBExecutor
from app.renderers.rollup import RollupRenderer
from app.sync.partition_index import PartitionSnapshot


@pytest.fixture
def executor() -> Iterator[DuckDBExecutor]:
    with DuckDBExecutor() as instance:
        yield instance


def test_result_and_timing(executor: DuckDBExecutor) -> None:
    result = executor.execute("SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2, NULL ORDER BY a")
    assert isinstance(executor, QueryExecutor)
    assert result.engine == "duckdb"
    assert result.sql.startswith("SELECT 1")
    assert isinstance(result.table, pa.Table)
    assert result.columns == ["a", "b"]
    assert result.row_count == 2
    assert result.to_rows() == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
    assert result.execution_time_ns > 0
    assert result.execution_time_ms == result.execution_time_ns / 1_000_000
    assert result.warehouse is None


@pytest.mark.parametrize("options", [None, ExecutionOptions(), ExecutionOptions(True)])
def test_execution_options_are_ignored(
    executor: DuckDBExecutor, options: ExecutionOptions | None
) -> None:
    result = executor.execute("SELECT 42 AS answer", options)
    assert result.to_rows() == [{"answer": 42}]
    assert result.warehouse is None


def test_empty_result(executor: DuckDBExecutor) -> None:
    result = executor.execute("SELECT 1 AS a WHERE FALSE")
    assert result.row_count == 0
    assert result.to_rows() == []
    assert result.columns == ["a"]


def test_sql_error_is_wrapped(executor: DuckDBExecutor) -> None:
    with pytest.raises(QueryExecutionError, match="duckdb query failed") as info:
        executor.execute("SELECT * FROM no_such_table")
    assert info.value.engine == "duckdb"
    assert info.value.sql == "SELECT * FROM no_such_table"
    assert info.value.cause is not None


def test_missing_parquet_files_is_wrapped(executor: DuckDBExecutor, tmp_path: Path) -> None:
    with pytest.raises(QueryExecutionError):
        executor.execute(f"SELECT * FROM read_parquet('{tmp_path}/x=*/rollup.parquet')")


def test_cursors_are_isolated_but_share_database(executor: DuckDBExecutor) -> None:
    # Kursor = połączenie do tej samej bazy w pamięci: tabela utworzona w jednym wywołaniu
    # jest widoczna w kolejnym (rollupy i tak są czytane z plików, nie z tabel).
    executor.execute("CREATE TABLE t AS SELECT 42 AS v; SELECT * FROM t")
    assert executor.execute("SELECT v FROM t").to_rows() == [{"v": 42}]


def test_config_is_applied() -> None:
    with DuckDBExecutor({"threads": 2}) as executor:
        assert executor.execute("SELECT current_setting('threads') AS t").to_rows() == [{"t": 2}]


def test_closed_executor_rejects_queries() -> None:
    executor = DuckDBExecutor()
    executor.close()
    executor.close()  # idempotentne
    with pytest.raises(RuntimeError, match="closed"):
        executor.execute("SELECT 1")


def test_parallel_queries_from_many_threads(executor: DuckDBExecutor) -> None:
    def run(i: int) -> tuple[int, ExecutionResult]:
        return i, executor.execute(f"SELECT {i} AS i, SUM(range) AS s FROM range({i * 1000})")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(run, range(1, 201)))
    for i, result in results:
        n = i * 1000
        assert result.to_rows() == [{"i": i, "s": n * (n - 1) // 2}]


def test_parallel_queries_on_rollups(
    executor: DuckDBExecutor, rollup_snapshot: PartitionSnapshot
) -> None:
    source = RollupRenderer(rollup_snapshot.files_for(None)).source().sql("duckdb")
    sql = (
        f"SELECT pickup_date, SUM(trips_count) AS n FROM {source} "
        "GROUP BY pickup_date ORDER BY pickup_date"
    )
    expected = executor.execute(sql).to_rows()
    assert len(expected) == 11

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: executor.execute(sql).to_rows(), range(64)))
    assert all(rows == expected for rows in results)
