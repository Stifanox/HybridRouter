"""Walidator semantyczny i integracja z loaderem (punkt rozszerzenia z A1)."""

from __future__ import annotations

from typing import Any

import pytest

from app.config.loader import (
    SEMANTIC_VALIDATORS,
    ConfigValidationError,
    load_model_dict,
    load_model_file,
)
from app.semantic.resolver import build_semantic_model
from app.semantic.validator import validate_semantics

from .conftest import NYC_TAXI, ConfigFactory


def _issues(raw: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    return [str(issue) for issue in exc_info.value.issues]


def test_validator_is_registered_in_loader() -> None:
    assert validate_semantics in SEMANTIC_VALIDATORS


def test_nyc_taxi_loads_with_semantic_validation() -> None:
    model = load_model_file(NYC_TAXI)  # domyślnie z SEMANTIC_VALIDATORS
    assert list(validate_semantics(model)) == []
    semantic = build_semantic_model(model)
    assert len(semantic.metrics) == len(model.base_measures) + len(model.derived_metrics)


def test_avg_is_rejected_with_hint(raw: dict[str, Any]) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "avg"
    assert _issues(raw) == [
        "base_measures.total_tip_amount.aggregation: aggregation 'AVG' is not supported; "
        "define it as a derived metric SUM/COUNT (e.g. total_x / count_x)"
    ]


def test_unknown_aggregation_lists_allowed(raw: dict[str, Any]) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "MEDIAN"
    (issue,) = _issues(raw)
    assert issue.startswith("base_measures.total_tip_amount.aggregation: unknown aggregation")
    assert "allowed: COUNT, COUNT_DISTINCT, MAX, MIN, SUM" in issue


def test_unknown_aggregation_suggestion(raw: dict[str, Any]) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "SUMM"
    raw["base_measures"]["total_fare_amount"]["aggregation"] = "XYZ"
    fare, tip = _issues(raw)  # kolejność jak w YAML
    assert tip.endswith("did you mean 'SUM'?")
    assert "did you mean" not in fare


def test_count_distinct_is_accepted_and_non_additive(raw: dict[str, Any]) -> None:
    raw["derived_metrics"]["trips_per_zone"] = {"formula": "trips_count / unique_pickup_zones"}
    raw["derived_metrics"]["trips_per_zone_rounded"] = {"formula": "ROUND(trips_per_zone, 1)"}
    model = build_semantic_model(load_model_dict(raw))

    assert model.get_metric("unique_pickup_zones").additive is False
    assert model.get_metric("trips_per_zone").additive is False
    assert model.get_metric("trips_per_zone_rounded").additive is False  # dziedziczone
    assert model.get_metric("trips_per_zone_rounded").base_measures == {
        "trips_count",
        "unique_pickup_zones",
    }
    assert model.get_metric("avg_fare_per_trip").additive is True


@pytest.mark.parametrize("aggregation", ["SUM", "COUNT", "MIN", "MAX", "COUNT_DISTINCT"])
def test_all_registered_aggregations_are_accepted(raw: dict[str, Any], aggregation: str) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = aggregation
    load_model_dict(raw)


def test_formula_errors_have_field_paths(raw: dict[str, Any]) -> None:
    raw["derived_metrics"] = {
        "a": {"formula": "SUM(total_revenue)"},
        "b": {"formula": "c + 1"},
        "c": {"formula": "b * 2"},
        "d": {"formula": "total_revnue / trips_count"},
    }
    issues = _issues(raw)
    assert [issue.split(":")[0] for issue in issues] == [
        "derived_metrics.a.formula",
        "derived_metrics.d.formula",
        "derived_metrics.b.formula",
    ]
    assert "define aggregations in base_measures" in issues[0]
    assert "did you mean 'total_revenue'?" in issues[1]
    assert issues[2].endswith("circular dependency: b -> c -> b")


def test_error_message_shows_formula_pointer(raw: dict[str, Any]) -> None:
    raw["derived_metrics"]["bad"] = {"formula": "total_revenue.real"}
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw, source="configs/nyc_taxi.yaml")
    message = str(exc_info.value)
    assert message.startswith("Invalid configuration in 'configs/nyc_taxi.yaml':")
    assert "derived_metrics.bad.formula: attribute access is not allowed" in message
    assert "      total_revenue.real\n      ^" in message


def test_semantic_validation_can_be_disabled(
    raw: dict[str, Any], structural: ConfigFactory
) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "AVG"
    model = structural(raw)
    assert [i.path for i in validate_semantics(model)] == [
        "base_measures.total_tip_amount.aggregation"
    ]
