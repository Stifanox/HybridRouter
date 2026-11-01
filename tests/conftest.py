"""Wspólne fixture'y: model semantyczny nyc_taxi.yaml i fabryka żądań (etap A3)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.api.models import QueryRequest
from app.config.loader import load_model_dict, load_model_file
from app.semantic.resolver import SemanticModel, build_semantic_model

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
