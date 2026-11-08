"""Testy różnicowe: ścieżka surowa vs ścieżka rollupowa (ADR-012).

Metodyka:

1. deterministyczny generator (``tests/fixtures/synthetic.py``) tworzy surowe przejazdy z
   przypadkami brzegowymi (zerowe opłaty, zerowy czas, ``NULL`` w miarach i wymiarze,
   przejazd przez północ, dzień bez danych),
2. z tych danych budowane są dzienne rollupy Parquet – zapytaniem z ``app.sync.rollup_query``
   (tym samym, którego użyje SyncWorker), wykonanym w DuckDB i zapisanym atomowo,
3. dla każdego przypadku macierzy {metryka addytywna z ``nyc_taxi.yaml``} × {zestaw wymiarów
   rollupowych} × {scenariusz filtrów} to samo żądanie przechodzi przez builder planu i jest
   renderowane dwukrotnie: ``RawTableRenderer@duckdb`` na surowej tabeli oraz
   ``RollupRenderer@duckdb`` na plikach Parquet (przez ``DuckDBExecutor``),
4. wyniki są porównywane jako mapy ``krotka wymiarów -> wartość``: ten sam zbiór grup, a
   wartości równe z tolerancją ``rel_tol=1e-9``, ``abs_tol=1e-9`` (suma zmiennoprzecinkowa
   zależy od kolejności dodawania; ``abs_tol`` obejmuje różnice bliskie zera, np.
   ``non_fare_surcharges`` liczone jako różnica sum); ``NULL`` jest równy tylko ``NULL``.

Scenariusze filtrów nie są ograniczone do zapytań kwalifikujących się do cache (np. zakres
z dziurą, zakres otwarty) – porównywana jest semantyka obu renderów, a nie decyzja routera.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from app.config.loader import load_model_file
from app.executors.duckdb_executor import DuckDBExecutor
from app.planner.builder import plan_query
from app.planner.logical_plan import LogicalQuery
from app.renderers.raw_table import RawTableRenderer
from app.renderers.rollup import RollupRenderer
from app.semantic.resolver import SemanticModel, build_semantic_model
from app.sync.local_builder import build_local_rollups
from app.sync.partition_index import PartitionIndex
from tests.conftest import (
    MODEL_NAME,
    NYC_TAXI_YAML,
    RAW_TABLE,
    SYNTH_END,
    SYNTH_GAP,
    SYNTH_RANGE,
    SYNTH_START,
    make_request,
)
from tests.fixtures.synthetic import to_decimal_money

REL_TOL = 1e-9
ABS_TOL = 1e-9

_MODEL = build_semantic_model(load_model_file(NYC_TAXI_YAML))
ADDITIVE_METRICS: list[str] = [name for name, m in _MODEL.metrics.items() if m.additive]

DIMENSION_SETS: dict[str, list[str]] = {
    "none": [],
    "date": ["pickup_date"],
    "vendor": ["vendor_id"],
    "payment": ["payment_type"],
    "rate": ["rate_code"],
    "date_vendor": ["pickup_date", "vendor_id"],
    "vendor_payment_rate": ["vendor_id", "payment_type", "rate_code"],
    "all_rollup": ["pickup_date", "vendor_id", "payment_type", "rate_code"],
}


def _day(offset: int) -> str:
    return (SYNTH_START + dt.timedelta(days=offset)).isoformat()


def _between(start: str, end: str) -> list[dict[str, Any]]:
    return [
        {"field": "pickup_date", "op": ">=", "value": start},
        {"field": "pickup_date", "op": "<=", "value": end},
    ]


FULL = _between(SYNTH_START.isoformat(), SYNTH_END.isoformat())

SCENARIOS: dict[str, list[dict[str, Any]]] = {
    # Cały zakres danych (z dziurą 2022-12-04 w środku).
    "full_range": FULL,
    # Jeden dzień; zawiera przejazd przez północ (należy do dnia odbioru).
    "single_day": [{"field": "pickup_date", "op": "==", "value": _day(1)}],
    # Zakres obejmujący dzień bez danych.
    "across_gap": _between(_day(2), _day(5)),
    # Wyłącznie dzień bez danych: pusty wynik (bez wymiarów: COUNT = 0, SUM = NULL).
    "gap_day_only": [{"field": "pickup_date", "op": "==", "value": SYNTH_GAP.isoformat()}],
    # Zakres otwarty z jednej strony (router nie użyłby cache – semantyka ma być ta sama).
    "open_range": [{"field": "pickup_date", "op": ">", "value": _day(5)}],
    # Brak filtrów.
    "no_filters": [],
    "card_payments": [*FULL, {"field": "payment_type", "op": "==", "value": "1"}],
    # payment_type 3: suma fare_amount = 0 -> NULLIF -> NULL w tip_to_fare_ratio_pct.
    "zero_fare_group": [*FULL, {"field": "payment_type", "op": "in", "value": ["3"]}],
    # payment_type 4: zerowy czas przejazdu -> NULL w avg_trip_speed_mph.
    "zero_duration_group": [
        *_between(_day(0), _day(6)),
        {"field": "payment_type", "op": "==", "value": "4"},
    ],
    # != i not_in (wiersze z rate_code = NULL odpadają w obu ścieżkach).
    "exclusions": [
        *_between(_day(1), _day(9)),
        {"field": "vendor_id", "op": "!=", "value": "2"},
        {"field": "rate_code", "op": "not_in", "value": ["2", "5"]},
    ],
}
EMPTY_SCENARIOS = frozenset({"gap_day_only"})

CASES = [
    pytest.param(metric, dims, scenario, id=f"{metric}-{dims}-{scenario}")
    for metric in ADDITIVE_METRICS
    for dims in DIMENSION_SETS
    for scenario in SCENARIOS
]


# --------------------------------------------------------------------------- #
# Wykonanie obu ścieżek
# --------------------------------------------------------------------------- #


class DifferentialRunner:
    """Wykonuje plan na surowej tabeli i na rollupach (DuckDB) – obie ścieżki."""

    def __init__(self, raw: pa.Table, rollup_base: Path, model: SemanticModel) -> None:
        self.model = model
        self.raw_connection = duckdb.connect(":memory:")
        self.raw_connection.register(RAW_TABLE, raw)
        self.executor = DuckDBExecutor()
        self.raw_renderer = RawTableRenderer(RAW_TABLE)
        cache = model.config.storage.local_cache
        self.snapshot = PartitionIndex(rollup_base, cache.partition_key).snapshot()

    def plan(self, dimensions: list[str], metrics: list[str], filters: list[Any]) -> LogicalQuery:
        request = make_request(
            model=MODEL_NAME,
            dimensions=dimensions,
            metrics=metrics,
            filters=filters,
            order_by=[],
            limit=None,
        )
        return plan_query(request, self.model)

    def raw(self, plan: LogicalQuery) -> list[dict[str, Any]]:
        sql = self.raw_renderer.render(plan, "duckdb")
        rows: list[dict[str, Any]] = self.raw_connection.sql(sql).to_arrow_table().to_pylist()
        return rows

    def rollup(self, plan: LogicalQuery) -> list[dict[str, Any]]:
        # Pliki z migawki dla zakresu planu (jak w routerze); gdy zakres nie ma żadnego
        # pliku (scenariusz „tylko dzień-dziura”), czytane są wszystkie pliki – filtr WHERE
        # i tak ogranicza wynik, a porównywana jest semantyka zapytania.
        files = self.snapshot.files_for(plan.date_range) or self.snapshot.files_for(None)
        sql = RollupRenderer(files).render(plan, "duckdb")
        return self.executor.execute(sql).to_rows()

    def close(self) -> None:
        self.raw_connection.close()
        self.executor.close()


@pytest.fixture(scope="module")
def runner(
    synthetic_raw: pa.Table, rollup_base_path: Path, nyc_model: SemanticModel
) -> Iterator[DifferentialRunner]:
    instance = DifferentialRunner(synthetic_raw, rollup_base_path, nyc_model)
    yield instance
    instance.close()


def _number(value: Any) -> float:
    return float(value) if isinstance(value, Decimal | int | float) else math.nan


def values_equal(raw: Any, rollup: Any) -> bool:
    """Równość wartości z tolerancją; ``NULL`` równy wyłącznie ``NULL``."""
    if raw is None or rollup is None:
        return raw is None and rollup is None
    return math.isclose(_number(raw), _number(rollup), rel_tol=REL_TOL, abs_tol=ABS_TOL)


def keyed(
    rows: list[dict[str, Any]], dimensions: list[str], metric: str
) -> dict[tuple[Any, ...], Any]:
    result = {tuple(row[d] for d in dimensions): row[metric] for row in rows}
    assert len(result) == len(rows), "duplicate dimension keys in result"
    return result


def assert_same_result(
    raw_rows: list[dict[str, Any]], rollup_rows: list[dict[str, Any]], dims: list[str], metric: str
) -> None:
    raw = keyed(raw_rows, dims, metric)
    rollup = keyed(rollup_rows, dims, metric)
    assert raw.keys() == rollup.keys(), (
        f"groups differ: only raw {sorted(raw.keys() - rollup.keys(), key=repr)[:5]}, "
        f"only rollup {sorted(rollup.keys() - raw.keys(), key=repr)[:5]}"
    )
    mismatches = [
        (key, raw[key], rollup[key]) for key in raw if not values_equal(raw[key], rollup[key])
    ]
    assert not mismatches, f"{len(mismatches)} mismatching groups, e.g. {mismatches[:3]}"


# --------------------------------------------------------------------------- #
# Macierz przypadków
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("metric", "dims", "scenario"), CASES)
def test_raw_equals_rollup(
    runner: DifferentialRunner, metric: str, dims: str, scenario: str
) -> None:
    dimensions = DIMENSION_SETS[dims]
    plan = runner.plan(dimensions, [metric], SCENARIOS[scenario])
    raw_rows = runner.raw(plan)
    if scenario not in EMPTY_SCENARIOS:
        assert raw_rows, "non-empty scenario returned no rows – the comparison would be vacuous"
    assert_same_result(raw_rows, runner.rollup(plan), dimensions, metric)


def test_matrix_covers_all_additive_metrics(nyc_model: SemanticModel) -> None:
    additive = {n for n, m in nyc_model.metrics.items() if m.additive}
    assert set(ADDITIVE_METRICS) == additive
    assert len(ADDITIVE_METRICS) == 13
    assert "unique_pickup_zones" not in additive
    assert len(CASES) == len(ADDITIVE_METRICS) * len(DIMENSION_SETS) * len(SCENARIOS)


def test_all_metrics_in_one_query_match(runner: DifferentialRunner) -> None:
    """Wszystkie metryki addytywne w jednym zapytaniu (wspólne liście w drzewie SQL)."""
    dims = DIMENSION_SETS["all_rollup"]
    plan = runner.plan(dims, ADDITIVE_METRICS, FULL)
    raw_rows, rollup_rows = runner.raw(plan), runner.rollup(plan)
    for metric in ADDITIVE_METRICS:
        assert_same_result(raw_rows, rollup_rows, dims, metric)


# --------------------------------------------------------------------------- #
# Przypadki brzegowe są rzeczywiście osiągane (testy nie są puste)
# --------------------------------------------------------------------------- #


def test_zero_fare_group_yields_null_ratio_in_both_paths(runner: DifferentialRunner) -> None:
    plan = runner.plan(
        ["pickup_date"],
        ["tip_to_fare_ratio_pct", "total_fare_amount"],
        SCENARIOS["zero_fare_group"],
    )
    for rows in (runner.raw(plan), runner.rollup(plan)):
        assert rows
        assert all(row["total_fare_amount"] in (0, None) for row in rows)
        assert all(row["tip_to_fare_ratio_pct"] is None for row in rows)


def test_zero_duration_group_yields_null_speed_in_both_paths(runner: DifferentialRunner) -> None:
    plan = runner.plan(
        ["vendor_id"],
        ["avg_trip_speed_mph", "total_duration_seconds"],
        SCENARIOS["zero_duration_group"],
    )
    for rows in (runner.raw(plan), runner.rollup(plan)):
        assert rows
        assert all(row["total_duration_seconds"] == 0 for row in rows)
        assert all(row["avg_trip_speed_mph"] is None for row in rows)


def test_empty_input_without_dimensions(runner: DifferentialRunner) -> None:
    """Bez ``GROUP BY`` pusty zbiór daje jeden wiersz: ``COUNT`` = 0, ``SUM`` = ``NULL``.

    Ten przypadek wykrył różnicę: re-agregacja ``COUNT -> SUM`` na pustym rollupie dawała
    ``NULL`` – stąd ``COALESCE(SUM(trips_count), 0)`` (``AggregationSpec.empty_input_value``).
    """
    plan = runner.plan(
        [], ["trips_count", "total_revenue", "avg_fare_per_trip"], SCENARIOS["gap_day_only"]
    )
    expected = [{"trips_count": 0, "total_revenue": None, "avg_fare_per_trip": None}]
    assert runner.raw(plan) == expected
    assert runner.rollup(plan) == expected


def test_null_dimension_values_form_their_own_group(runner: DifferentialRunner) -> None:
    plan = runner.plan(["rate_code"], ["trips_count"], FULL)
    raw = keyed(runner.raw(plan), ["rate_code"], "trips_count")
    rollup = keyed(runner.rollup(plan), ["rate_code"], "trips_count")
    assert (None,) in raw
    assert raw == rollup


def test_float_sums_actually_differ_by_summation_order(runner: DifferentialRunner) -> None:
    """Uzasadnienie tolerancji: przynajmniej jedna grupa różni się bitowo między ścieżkami."""
    dims = ["pickup_date", "vendor_id"]
    plan = runner.plan(dims, ["total_revenue"], FULL)
    raw = keyed(runner.raw(plan), dims, "total_revenue")
    rollup = keyed(runner.rollup(plan), dims, "total_revenue")
    assert any(raw[k] != rollup[k] for k in raw)
    assert all(values_equal(raw[k], rollup[k]) for k in raw)


# --------------------------------------------------------------------------- #
# Wariant NUMERIC (decimal128(38, 9) – typ kwot w BigQuery)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def decimal_runner(
    tmp_path_factory: pytest.TempPathFactory, synthetic_raw: pa.Table, nyc_model: SemanticModel
) -> Iterator[DifferentialRunner]:
    raw = to_decimal_money(synthetic_raw)
    base = tmp_path_factory.mktemp("rollups_decimal")
    build_local_rollups(raw, nyc_model.config, SYNTH_RANGE, base)
    instance = DifferentialRunner(raw, base, nyc_model)
    yield instance
    instance.close()


@pytest.mark.parametrize("dims", ["none", "date_vendor", "all_rollup"])
@pytest.mark.parametrize("scenario", ["full_range", "zero_fare_group", "exclusions"])
def test_raw_equals_rollup_numeric_money(
    decimal_runner: DifferentialRunner, dims: str, scenario: str
) -> None:
    dimensions = DIMENSION_SETS[dims]
    plan = decimal_runner.plan(dimensions, ADDITIVE_METRICS, SCENARIOS[scenario])
    raw_rows, rollup_rows = decimal_runner.raw(plan), decimal_runner.rollup(plan)
    assert raw_rows
    for metric in ADDITIVE_METRICS:
        assert_same_result(raw_rows, rollup_rows, dimensions, metric)


def test_numeric_money_sums_are_exact(decimal_runner: DifferentialRunner) -> None:
    """Dla ``NUMERIC`` sumy są dokładne – obie ścieżki dają identyczne wartości."""
    dims = ["pickup_date", "vendor_id"]
    plan = decimal_runner.plan(dims, ["total_revenue", "total_fare_amount"], FULL)
    for metric in ("total_revenue", "total_fare_amount"):
        raw = keyed(decimal_runner.raw(plan), dims, metric)
        assert all(isinstance(v, Decimal) for v in raw.values() if v is not None)
        assert raw == keyed(decimal_runner.rollup(plan), dims, metric)


# --------------------------------------------------------------------------- #
# Kontrprzykład: średnia z dziennych wskaźników ≠ wskaźnik dla zakresu
# --------------------------------------------------------------------------- #


def counterexample(runner: DifferentialRunner) -> dict[str, float]:
    """Liczby kontrprzykładu dla ``tip_to_fare_ratio_pct`` na pełnym zakresie danych."""
    metric = "tip_to_fare_ratio_pct"
    daily_plan = runner.plan(["pickup_date"], [metric, "total_fare_amount"], FULL)
    daily = runner.rollup(daily_plan)
    total_plan = runner.plan([], [metric], FULL)
    correct_rollup = runner.rollup(total_plan)[0][metric]
    correct_raw = runner.raw(total_plan)[0][metric]

    values = [row[metric] for row in daily if row[metric] is not None]
    weights = [row["total_fare_amount"] for row in daily if row[metric] is not None]
    return {
        "days": len(values),
        "correct_raw": correct_raw,
        "correct_rollup": correct_rollup,
        "naive_mean_of_daily": statistics.fmean(values),
        "fare_weighted_mean_of_daily": sum(v * w for v, w in zip(values, weights, strict=True))
        / sum(weights),
        "daily_min": min(values),
        "daily_max": max(values),
    }


def test_counterexample_mean_of_daily_ratios_is_wrong(runner: DifferentialRunner) -> None:
    numbers = counterexample(runner)
    assert numbers["days"] == (SYNTH_END - SYNTH_START).days  # 12 dni minus dziura
    # Wartość poprawna: raw == rollup (złota zasada: dzielenie po zsumowaniu zakresu).
    assert math.isclose(numbers["correct_raw"], numbers["correct_rollup"], rel_tol=REL_TOL)
    # Naiwna średnia z dziennych wskaźników jest wyraźnie inna (> 0,5 p.p.).
    assert abs(numbers["naive_mean_of_daily"] - numbers["correct_rollup"]) > 0.5
    # Średnia ważona mianownikiem (sumą opłat) odtwarza wartość poprawną.
    assert math.isclose(
        numbers["fare_weighted_mean_of_daily"], numbers["correct_rollup"], rel_tol=1e-9
    )
