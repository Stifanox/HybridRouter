"""Wspólne fixture'y: model semantyczny nyc_taxi.yaml i fabryka żądań (etap A3), dane
syntetyczne i zbudowane z nich rollupy (etap A4)."""

from __future__ import annotations

import copy
import datetime as dt
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml

from app.api.models import QueryRequest
from app.config.loader import load_model_dict, load_model_file
from app.semantic.resolver import SemanticModel, build_semantic_model
from app.sync.local_builder import build_local_rollups
from app.sync.materializer import MaterializationResult
from app.sync.partition_index import PartitionIndex, PartitionSnapshot
from tests.fixtures.synthetic import generate_trips

BQ_INTEGRATION_ENV = "HYBRID_ROUTER_BQ_INTEGRATION"
"""``1`` włącza testy z markerem ``bigquery`` (prawdziwe BigQuery: sieć i koszt)."""


def bigquery_tests_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(BQ_INTEGRATION_ENV) == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Domyślnie pomija testy ``@pytest.mark.bigquery`` (zasady wspólne, pkt 7)."""
    if bigquery_tests_enabled():
        return
    skip = pytest.mark.skip(reason=f"BigQuery integration test; set {BQ_INTEGRATION_ENV}=1")
    for item in items:
        if item.get_closest_marker("bigquery") is not None:
            item.add_marker(skip)


NYC_TAXI_YAML = Path(__file__).resolve().parents[1] / "configs" / "nyc_taxi.yaml"
MODEL_NAME = "nyc_yellow_taxi_analytics"

EXAMPLE_REQUEST: dict[str, Any] = {
    "model": MODEL_NAME,
    "dimensions": ["pickup_date", "vendor_id"],
    "metrics": ["trips_count", "total_revenue", "tip_to_fare_ratio_pct", "avg_trip_speed_mph"],
    "filters": [
        {"field": "pickup_date", "op": ">=", "value": "2022-12-01"},
        {"field": "pickup_date", "op": "<=", "value": "2022-12-23"},
        {"field": "payment_type", "op": "==", "value": "1"},
    ],
    "order_by": [{"field": "pickup_date", "direction": "ASC"}],
    "limit": 1000,
    "control": {"execution_mode": "auto", "include_telemetry": True, "confirm_high_cost": False},
}
"""Przykładowe żądanie z dok. 03, sekcja 4."""


@pytest.fixture(scope="session")
def nyc_model() -> SemanticModel:
    return build_semantic_model(load_model_file(NYC_TAXI_YAML))


@pytest.fixture(scope="session")
def models(nyc_model: SemanticModel) -> dict[str, SemanticModel]:
    return {nyc_model.name: nyc_model}


@pytest.fixture
def nyc_raw() -> dict[str, Any]:
    """Świeża, modyfikowalna kopia słownika z nyc_taxi.yaml."""
    with NYC_TAXI_YAML.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


ModelFactory = Callable[[dict[str, Any]], SemanticModel]


@pytest.fixture
def model_from() -> ModelFactory:
    """Model semantyczny z (zmodyfikowanego) słownika konfiguracji."""

    def build(data: dict[str, Any]) -> SemanticModel:
        return build_semantic_model(load_model_dict(data))

    return build


def make_request(**overrides: Any) -> QueryRequest:
    """Żądanie z dok. 03 z nadpisanymi polami (``filters=[]`` itd.)."""
    data = copy.deepcopy(EXAMPLE_REQUEST)
    data.update(overrides)
    return QueryRequest.model_validate(data)


# --------------------------------------------------------------------------- #
# Dane syntetyczne i rollupy (etap A4)
# --------------------------------------------------------------------------- #

SYNTH_START = dt.date(2022, 12, 1)
SYNTH_DAYS = 12
SYNTH_ROWS_PER_DAY = 250
SYNTH_GAP = dt.date(2022, 12, 4)
"""Dzień bez danych (``empty_day_offsets=(3,)``) – dziura w zakresie partycji."""
SYNTH_END = SYNTH_START + dt.timedelta(days=SYNTH_DAYS - 1)
SYNTH_RANGE = (SYNTH_START, SYNTH_END)
RAW_TABLE = "raw_trips"


@pytest.fixture(scope="session")
def synthetic_raw() -> pa.Table:
    return generate_trips(
        days=SYNTH_DAYS,
        rows_per_day=SYNTH_ROWS_PER_DAY,
        start=SYNTH_START,
        empty_day_offsets=((SYNTH_GAP - SYNTH_START).days,),
    )


@pytest.fixture(scope="session")
def synthetic_rollups(
    tmp_path_factory: pytest.TempPathFactory, synthetic_raw: pa.Table, nyc_model: SemanticModel
) -> MaterializationResult:
    """Dzienne rollupy zbudowane z ``synthetic_raw`` (katalog tymczasowy sesji)."""
    base_path = tmp_path_factory.mktemp("rollups")
    return build_local_rollups(synthetic_raw, nyc_model.config, SYNTH_RANGE, base_path)


@pytest.fixture(scope="session")
def rollup_base_path(synthetic_rollups: MaterializationResult) -> Path:
    return synthetic_rollups.publication.manifest_path.parent


@pytest.fixture(scope="session")
def rollup_snapshot(rollup_base_path: Path) -> PartitionSnapshot:
    """Migawka opublikowanych rollupów syntetycznych (wersja 1, 11 dni)."""
    return PartitionIndex(rollup_base_path, "pickup_date").snapshot()
