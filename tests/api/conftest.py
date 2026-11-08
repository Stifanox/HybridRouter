"""Fixture'y testów API: cache z syntetycznymi rollupami w ``{data_root}/nyc_taxi`` i klient
HTTP (``TestClient`` uruchamia ``lifespan`` aplikacji)."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import create_app
from app.semantic.resolver import SemanticModel
from app.sync.local_builder import build_local_rollups
from tests.conftest import EXAMPLE_REQUEST, NYC_TAXI_YAML, SYNTH_RANGE

CONFIG_DIR = NYC_TAXI_YAML.parent

COVERED_FILTERS: list[dict[str, Any]] = [
    {"field": "pickup_date", "op": ">=", "value": "2022-12-05"},
    {"field": "pickup_date", "op": "<=", "value": "2022-12-12"},
]
"""Zakres w pełni pokryty przez cache syntetyczny (2022-12-01…12 bez 2022-12-04)."""

GAP_FILTERS: list[dict[str, Any]] = [
    {"field": "pickup_date", "op": ">=", "value": "2022-12-03"},
    {"field": "pickup_date", "op": "<=", "value": "2022-12-05"},
]
"""Zakres z dziurą (2022-12-04)."""


@pytest.fixture(scope="session")
def data_root(
    tmp_path_factory: pytest.TempPathFactory, synthetic_raw: pa.Table, nyc_model: SemanticModel
) -> Path:
    root = tmp_path_factory.mktemp("data")
    build_local_rollups(synthetic_raw, nyc_model.config, SYNTH_RANGE, root / "nyc_taxi")
    return root


@pytest.fixture(scope="session")
def settings(data_root: Path) -> Settings:
    return Settings(config_dir=CONFIG_DIR, data_root=data_root)


@pytest.fixture(scope="session")
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


Body = dict[str, Any]
BodyFactory = Callable[..., Body]


@pytest.fixture
def body() -> BodyFactory:
    """Ciało żądania: przykład z dok. 03 na zakresie pokrytym przez cache, z nadpisaniami.

    ``mode`` / ``telemetry`` ustawiają pola ``control``.
    """

    def build(mode: str = "auto", telemetry: bool = True, **overrides: Any) -> Body:
        data = copy.deepcopy(EXAMPLE_REQUEST)
        data["filters"] = [*COVERED_FILTERS, {"field": "payment_type", "op": "==", "value": "1"}]
        data["control"] = {"execution_mode": mode, "include_telemetry": telemetry}
        data.update(overrides)
        return data

    return build
