"""Testy „golden SQL”: plan -> SQL dla trzech wariantów (typ źródła × dialekt).

Oczekiwany SQL leży w ``tests/renderers/golden/<przypadek>.<wariant>.sql``. Porównywany jest
SQL **znormalizowany** – ``sqlglot.parse_one(sql, read=d).sql(dialect=d, pretty=True)`` po
obu stronach – więc różnice w białych znakach i wielkości liter słów kluczowych nie psują
testu, a każda różnica w strukturze (nawiasy, NULLIF, kolejność kolumn) – tak.

Regeneracja po świadomej zmianie rendererów: ``UPDATE_GOLDEN=1 poetry run pytest
tests/renderers/test_golden.py`` – a potem przegląd ``git diff`` plików ``.sql``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import sqlglot

from app.planner.builder import build_logical_query
from app.renderers.base import SqlRenderer
from app.renderers.errors import NonAdditiveMeasureError
from app.renderers.raw_table import RawTableRenderer
from app.renderers.rollup import RollupRenderer
from app.semantic.resolver import SemanticModel
from tests.conftest import make_request

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

BQ_TABLE = "bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022"
DUCKDB_RAW_TABLE = "raw_trips"
GOLDEN_ROLLUP_FILES = (
    "./data/nyc_taxi/pickup_date=2022-12-01/rollup-00000001.parquet",
    "./data/nyc_taxi/pickup_date=2022-12-02/rollup-00000002.parquet",
)
"""Stała lista plików dla przypadków golden – testowana jest struktura zapytania; listę
plików dla zakresu dat wyznacza migawka indeksu (``PartitionSnapshot.files_for``)."""

VARIANTS: dict[str, str] = {
    "rollup_duckdb": "duckdb",
    "raw_bigquery": "bigquery",
    "raw_duckdb": "duckdb",
}

CASES: dict[str, dict[str, Any]] = {
    # Przykład z dok. 03, sekcja 4.
    "spec_example": {},
    # Metryka wielopoziomowa (surcharge -> non_fare_surcharges -> miary bazowe), sortowanie
    # po metryce malejąco, filtr równościowy na dacie.
    "multilevel": {
        "dimensions": ["payment_type"],
        "metrics": ["non_fare_surcharges", "surcharge_to_revenue_roi_pct"],
        "filters": [{"field": "pickup_date", "op": "==", "value": "2022-12-05"}],
        "order_by": [{"field": "surcharge_to_revenue_roi_pct", "direction": "DESC"}],
        "limit": 10,
    },
    # Funkcja skalarna ROUND z NULLIF wewnątrz argumentu.
    "round": {
        "dimensions": ["vendor_id"],
        "metrics": ["avg_fare_per_trip"],
        "filters": [
            {"field": "pickup_date", "op": ">", "value": "2022-11-30"},
            {"field": "pickup_date", "op": "<", "value": "2022-12-24"},
        ],
        "order_by": [{"field": "vendor_id", "direction": "ASC"}],
        "limit": None,
    },
    # Filtry in / not_in / != (także in na dacie).
    "in_not_in": {
        "dimensions": ["pickup_date", "rate_code"],
        "metrics": ["total_fare_amount", "avg_revenue_per_mile"],
        "filters": [
            {"field": "pickup_date", "op": "in", "value": ["2022-12-01", "2022-12-02"]},
            {"field": "vendor_id", "op": "in", "value": ["1", "2"]},
            {"field": "payment_type", "op": "not_in", "value": ["3", "4"]},
            {"field": "rate_code", "op": "!=", "value": "99"},
        ],
        "order_by": [],
        "limit": None,
    },
    # Brak filtrów, brak wymiarów: agregat globalny (bez WHERE i GROUP BY).
    "no_filters": {
        "dimensions": [],
        "metrics": ["trips_count", "avg_revenue_per_mile"],
        "filters": [],
        "order_by": [],
        "limit": None,
    },
    # Miara holistyczna – tylko RawTableRenderer (RollupRenderer -> błąd programistyczny).
    "holistic": {
        "dimensions": ["pickup_date"],
        "metrics": ["trips_count", "unique_pickup_zones"],
        "filters": [
            {"field": "pickup_date", "op": ">=", "value": "2022-12-01"},
            {"field": "pickup_date", "op": "<=", "value": "2022-12-07"},
        ],
        "order_by": [{"field": "unique_pickup_zones", "direction": "DESC"}],
        "limit": 5,
    },
}

ROLLUP_UNSUPPORTED = {"holistic"}


def renderer_for(variant: str, model: SemanticModel) -> SqlRenderer:
    match variant:
        case "rollup_duckdb":
            return RollupRenderer(GOLDEN_ROLLUP_FILES)
        case "raw_bigquery":
            return RawTableRenderer(BQ_TABLE)
        case _:
            return RawTableRenderer(DUCKDB_RAW_TABLE)


def normalize(sql: str, dialect: str) -> str:
    return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect, pretty=True)


PARAMS = [
    pytest.param(case, variant, id=f"{case}-{variant}")
    for case in CASES
    for variant in VARIANTS
    if not (case in ROLLUP_UNSUPPORTED and variant == "rollup_duckdb")
]


@pytest.mark.parametrize(("case", "variant"), PARAMS)
def test_golden_sql(models: dict[str, SemanticModel], case: str, variant: str) -> None:
    dialect = VARIANTS[variant]
    plan = build_logical_query(make_request(**CASES[case]), models)
    actual = renderer_for(variant, models["nyc_yellow_taxi_analytics"]).render(
        plan, dialect, pretty=True
    )
    path = GOLDEN_DIR / f"{case}.{variant}.sql"
    if UPDATE:
        path.write_text(actual + "\n", encoding="utf-8")
    expected = path.read_text(encoding="utf-8")
    assert normalize(actual, dialect) == normalize(expected, dialect)


def test_holistic_rejected_by_rollup_renderer(models: dict[str, SemanticModel]) -> None:
    plan = build_logical_query(make_request(**CASES["holistic"]), models)
    renderer = renderer_for("rollup_duckdb", models["nyc_yellow_taxi_analytics"])
    with pytest.raises(NonAdditiveMeasureError, match="unique_pickup_zones"):
        renderer.render(plan, "duckdb")


SPEC_FILES = [
    f"./data/nyc_taxi/pickup_date=2022-12-{day:02d}/rollup-00000001.parquet" for day in range(1, 24)
]
"""Pliki migawki dla zakresu 2022-12-01..23 z przykładu w dok. 03 (pierwsza publikacja)."""
SPEC_FILES_SQL = ", ".join(f"'{path}'" for path in SPEC_FILES)


def test_spec_example_matches_documented_compiled_sql(models: dict[str, SemanticModel]) -> None:
    """``compiled_sql`` z przykładowej odpowiedzi w dok. 03, sekcja 4 – dosłownie."""
    documented = (
        "SELECT pickup_date, vendor_id, COALESCE(SUM(trips_count), 0) AS trips_count, "
        "SUM(total_revenue) AS total_revenue, "
        "(SUM(total_tip_amount) / NULLIF(SUM(total_fare_amount), 0)) * 100 "
        "AS tip_to_fare_ratio_pct, SUM(total_distance_miles) / "
        "NULLIF((SUM(total_duration_seconds) / 3600.0), 0) AS avg_trip_speed_mph "
        f"FROM read_parquet([{SPEC_FILES_SQL}], "
        "hive_partitioning = true) WHERE pickup_date >= CAST('2022-12-01' AS DATE) "
        "AND pickup_date <= CAST('2022-12-23' AS DATE) AND payment_type = '1' "
        "GROUP BY pickup_date, vendor_id ORDER BY pickup_date ASC LIMIT 1000"
    )
    plan = build_logical_query(make_request(), models)
    actual = RollupRenderer(SPEC_FILES).render(plan, "duckdb")
    assert normalize(actual, "duckdb") == normalize(documented, "duckdb")


def test_every_golden_file_is_used() -> None:
    used = {f"{case}.{variant}.sql" for case, variant in (p.values for p in PARAMS)}
    assert {p.name for p in GOLDEN_DIR.glob("*.sql")} == used
