"""Wspólne fixture'y testów warstwy semantycznej (etap A2)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config.loader import load_model_dict
from app.config.schemas import ModelConfig

NYC_TAXI = Path(__file__).resolve().parents[2] / "configs" / "nyc_taxi.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    """Świeża, modyfikowalna kopia słownika wczytanego z nyc_taxi.yaml."""
    with NYC_TAXI.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


ConfigFactory = Callable[[dict[str, Any]], ModelConfig]


@pytest.fixture
def structural() -> ConfigFactory:
    """``ModelConfig`` po walidacji strukturalnej, ale BEZ semantycznej – pozwala testować
    resolver na konfiguracjach, które walidator semantyczny by odrzucił."""

    def build(data: dict[str, Any]) -> ModelConfig:
        return load_model_dict(data, semantic_validators=())

    return build
