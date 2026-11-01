"""Testy warstwy konfiguracji (etap A1): schematy Pydantic i loader z walidacją strukturalną."""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.config.loader import (
    SEMANTIC_VALIDATORS,
    ConfigIssue,
    ConfigValidationError,
    load_model_dict,
    load_model_file,
    load_models_dir,
)
from app.config.schemas import ModelConfig

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
NYC_TAXI = CONFIGS_DIR / "nyc_taxi.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    """Świeża, modyfikowalna kopia słownika wczytanego z nyc_taxi.yaml."""
    with NYC_TAXI.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def _issues(exc_info: pytest.ExceptionInfo[ConfigValidationError]) -> list[str]:
    return [str(issue) for issue in exc_info.value.issues]


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Poprawne ładowanie
# --------------------------------------------------------------------------- #


def test_load_nyc_taxi_file() -> None:
    model = load_model_file(NYC_TAXI)

    assert model.model_name == "nyc_yellow_taxi_analytics"
    assert model.storage.bigquery.partition_granularity == "DAY"
    assert model.storage.bigquery.use_query_cache is False
    cache = model.storage.local_cache
    assert cache.partition_key == "pickup_date"
    assert cache.partition_scheme == "hive"
    assert cache.date_window_start == dt.date(2022, 10, 1)
    assert cache.date_window_end == dt.date(2022, 12, 31)
    assert model.cost_guard.max_bytes_per_query == 10 * 1024**3
    assert model.cost_guard.action_on_exceed == "require_confirmation"

    assert list(model.dimensions) == [
        "pickup_date",
        "vendor_id",
        "payment_type",
        "rate_code",
        "dropoff_location_id",
    ]
    assert model.dimensions["dropoff_location_id"].in_rollup is False
    assert model.base_measures["trips_count"].aggregation == "COUNT"
    assert model.base_measures["unique_pickup_zones"].aggregation == "COUNT_DISTINCT"
    assert model.derived_metrics["avg_fare_per_trip"].formula == (
        "ROUND(total_fare_amount / trips_count, 2)"
    )
    assert len(model.base_measures) == 8
    assert len(model.derived_metrics) == 6


def test_load_models_dir_builds_registry() -> None:
    registry = load_models_dir(CONFIGS_DIR)

    assert set(registry) == {"nyc_yellow_taxi_analytics"}
    assert isinstance(registry["nyc_yellow_taxi_analytics"], ModelConfig)
    with pytest.raises(TypeError):
        registry["other"] = registry["nyc_yellow_taxi_analytics"]  # type: ignore[index]


def test_model_is_frozen() -> None:
    model = load_model_file(NYC_TAXI)
    with pytest.raises(ValidationError):
        model.model_name = "changed"  # type: ignore[misc]


def test_aggregation_is_normalized_to_upper_case(raw: dict[str, Any]) -> None:
    raw["base_measures"]["total_fare_amount"]["aggregation"] = "  sum "
    model = load_model_dict(raw)
    assert model.base_measures["total_fare_amount"].aggregation == "SUM"


def test_derived_metrics_section_is_optional(raw: dict[str, Any]) -> None:
    del raw["derived_metrics"]
    assert load_model_dict(raw).derived_metrics == {}


# --------------------------------------------------------------------------- #
# Walidacja pól (Pydantic): extra="forbid", Literal, dodatniość, niepuste napisy
# --------------------------------------------------------------------------- #


def test_unknown_key_is_rejected(raw: dict[str, Any]) -> None:
    raw["dimensions"]["vendor_id"]["in_rolup"] = True  # literówka
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw, source="test.yaml")
    assert any(i.startswith("dimensions.vendor_id.in_rolup:") for i in _issues(exc_info))


@pytest.mark.parametrize(
    ("section_path", "value"),
    [
        (("cost_guard", "action_on_exceed"), "ignore"),
        (("dimensions", "vendor_id", "type"), "timestamp"),
        (("storage", "local_cache", "partition_scheme"), "flat"),
        (("storage", "local_cache", "file_format"), "csv"),
        (("storage", "bigquery", "partition_granularity"), "WEEK"),
    ],
)
def test_literal_fields_reject_unknown_values(
    raw: dict[str, Any], section_path: tuple[str, ...], value: str
) -> None:
    node = raw
    for key in section_path[:-1]:
        node = node[key]
    node[section_path[-1]] = value

    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert any(i.startswith(".".join(section_path) + ":") for i in _issues(exc_info))


@pytest.mark.parametrize(
    "field", ["price_per_tb_usd", "max_bytes_per_query", "max_cost_usd_per_query"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_cost_guard_values_must_be_positive(raw: dict[str, Any], field: str, value: int) -> None:
    raw["cost_guard"][field] = value
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert any(i.startswith(f"cost_guard.{field}:") for i in _issues(exc_info))


@pytest.mark.parametrize("value", ["", "   ", None, 5])
def test_aggregation_must_be_non_empty_string(raw: dict[str, Any], value: object) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = value
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert any(
        i.startswith("base_measures.total_tip_amount.aggregation:") for i in _issues(exc_info)
    )


def test_in_rollup_must_be_a_real_boolean(raw: dict[str, Any]) -> None:
    raw["dimensions"]["vendor_id"]["in_rollup"] = "yes"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert any(i.startswith("dimensions.vendor_id.in_rollup:") for i in _issues(exc_info))


def test_invalid_date_is_rejected(raw: dict[str, Any]) -> None:
    raw["storage"]["local_cache"]["date_window_end"] = "2022-13-45"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert any(i.startswith("storage.local_cache.date_window_end:") for i in _issues(exc_info))


def test_missing_required_section_is_rejected(raw: dict[str, Any]) -> None:
    del raw["cost_guard"]
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert any(i.startswith("cost_guard:") for i in _issues(exc_info))


@pytest.mark.parametrize("action", ["raise_error", "require_confirmation", "silent_allow"])
def test_action_on_exceed_accepts_all_modes(raw: dict[str, Any], action: str) -> None:
    raw["cost_guard"]["action_on_exceed"] = action
    assert load_model_dict(raw).cost_guard.action_on_exceed == action


# --------------------------------------------------------------------------- #
# Walidacja strukturalna (reguły międzypolowe)
# --------------------------------------------------------------------------- #


def test_duplicate_key_within_section_is_rejected(tmp_path: Path) -> None:
    """W obrębie sekcji duplikat nazwy to zduplikowany klucz YAML – PyYAML domyślnie go gubi."""
    text = NYC_TAXI.read_text(encoding="utf-8")
    duplicated = text.replace(
        "  total_tip_amount:\n",
        '  total_tip_amount:\n    aggregation: "SUM"\n    sql: "tip_amount"\n  total_tip_amount:\n',
        1,
    )
    assert duplicated != text
    path = tmp_path / "dup.yaml"
    path.write_text(duplicated, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_file(path)
    assert exc_info.value.source == str(path)
    assert "duplicate key 'total_tip_amount'" in str(exc_info.value)


def test_case_insensitive_duplicate_within_section_is_rejected(raw: dict[str, Any]) -> None:
    raw["base_measures"]["Trips_Count"] = {"aggregation": "COUNT", "sql": "1"}
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert _issues(exc_info) == [
        "base_measures.Trips_Count: name conflicts with 'base_measures.trips_count'"
    ]


@pytest.mark.parametrize(
    ("section", "name", "definition", "conflicts_with"),
    [
        ("base_measures", "vendor_id", {"aggregation": "COUNT", "sql": "1"}, "dimensions"),
        ("derived_metrics", "total_revenue", {"formula": "trips_count"}, "base_measures"),
        ("derived_metrics", "pickup_date", {"formula": "trips_count"}, "dimensions"),
    ],
)
def test_names_must_be_unique_across_sections(
    raw: dict[str, Any],
    section: str,
    name: str,
    definition: dict[str, Any],
    conflicts_with: str,
) -> None:
    raw[section][name] = definition
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert _issues(exc_info) == [f"{section}.{name}: name conflicts with '{conflicts_with}.{name}'"]


def test_names_must_be_identifiers(raw: dict[str, Any]) -> None:
    raw["derived_metrics"]["tip ratio"] = {"formula": "total_tip_amount"}
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert len(exc_info.value.issues) == 1
    assert exc_info.value.issues[0].path == "derived_metrics.tip ratio"


def test_partition_key_must_be_defined_dimension(raw: dict[str, Any]) -> None:
    raw["storage"]["local_cache"]["partition_key"] = "pickup_day"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert _issues(exc_info) == [
        "storage.local_cache.partition_key: 'pickup_day' is not a defined dimension"
    ]


def test_partition_key_must_be_date_dimension(raw: dict[str, Any]) -> None:
    raw["storage"]["local_cache"]["partition_key"] = "vendor_id"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert _issues(exc_info) == [
        "storage.local_cache.partition_key: dimension 'vendor_id' must have type 'date', "
        "got 'string'"
    ]


def test_partition_key_must_be_in_rollup(raw: dict[str, Any]) -> None:
    raw["dimensions"]["pickup_date"]["in_rollup"] = False
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert _issues(exc_info) == [
        "storage.local_cache.partition_key: dimension 'pickup_date' must have in_rollup: true"
    ]


def test_date_window_start_after_end_is_rejected(raw: dict[str, Any]) -> None:
    raw["storage"]["local_cache"]["date_window_start"] = "2023-01-01"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert _issues(exc_info) == [
        "storage.local_cache.date_window_start: date_window_start (2023-01-01) must not be "
        "later than date_window_end (2022-12-31)"
    ]


def test_single_day_window_is_accepted(raw: dict[str, Any]) -> None:
    raw["storage"]["local_cache"]["date_window_start"] = "2022-12-31"
    assert load_model_dict(raw).storage.local_cache.date_window_start == dt.date(2022, 12, 31)


def test_all_structural_issues_are_reported_together(raw: dict[str, Any]) -> None:
    raw["storage"]["local_cache"]["date_window_start"] = "2023-01-01"
    raw["storage"]["local_cache"]["partition_key"] = "vendor_id"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw)
    assert {i.path for i in exc_info.value.issues} == {
        "storage.local_cache.partition_key",
        "storage.local_cache.date_window_start",
    }


# --------------------------------------------------------------------------- #
# Błędy na poziomie pliku i katalogu
# --------------------------------------------------------------------------- #


def test_error_message_names_file_and_field_path(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["cost_guard"]["price_per_tb_usd"] = -6.25
    path = _write_yaml(tmp_path / "broken.yaml", raw)

    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_file(path)
    message = str(exc_info.value)
    assert str(path) in message
    assert "cost_guard.price_per_tb_usd" in message


def test_duplicate_model_name_in_directory(tmp_path: Path, raw: dict[str, Any]) -> None:
    first = _write_yaml(tmp_path / "a.yaml", raw)
    second = _write_yaml(tmp_path / "b.yaml", raw)

    with pytest.raises(ConfigValidationError) as exc_info:
        load_models_dir(tmp_path)
    assert exc_info.value.source == str(second)
    assert _issues(exc_info) == [
        f"model_name: duplicate model_name 'nyc_yellow_taxi_analytics' "
        f"(already defined in '{first}')"
    ]


def test_directory_with_two_models(tmp_path: Path, raw: dict[str, Any]) -> None:
    _write_yaml(tmp_path / "a.yaml", raw)
    raw["model_name"] = "second_model"
    _write_yaml(tmp_path / "b.yaml", raw)
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    assert set(load_models_dir(tmp_path)) == {"nyc_yellow_taxi_analytics", "second_model"}


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="not a directory"):
        load_models_dir(tmp_path / "missing")


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="cannot read file"):
        load_model_file(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("model_name: [unclosed\n", "invalid YAML"),
        ("- just\n- a list\n", "top-level YAML element must be a mapping"),
        ("", "top-level YAML element must be a mapping"),
    ],
)
def test_malformed_yaml_is_rejected(tmp_path: Path, content: str, expected: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigValidationError, match=expected):
        load_model_file(path)


# --------------------------------------------------------------------------- #
# Punkt rozszerzenia: walidatory semantyczne (A2)
# --------------------------------------------------------------------------- #


def test_no_semantic_validators_registered_in_a1() -> None:
    assert SEMANTIC_VALIDATORS == ()


def _forbid_avg(model: ModelConfig) -> Iterator[ConfigIssue]:
    for name, measure in model.base_measures.items():
        if measure.aggregation == "AVG":
            yield ConfigIssue(f"base_measures.{name}.aggregation", "AVG is not allowed")


def test_semantic_validators_run_after_structural(raw: dict[str, Any]) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "avg"
    with pytest.raises(ConfigValidationError) as exc_info:
        load_model_dict(raw, semantic_validators=[_forbid_avg])
    assert _issues(exc_info) == ["base_measures.total_tip_amount.aggregation: AVG is not allowed"]


def test_semantic_validators_skipped_when_structure_invalid(raw: dict[str, Any]) -> None:
    calls: list[str] = []

    def spy(model: ModelConfig) -> list[ConfigIssue]:
        calls.append(model.model_name)
        return []

    raw["storage"]["local_cache"]["partition_key"] = "vendor_id"
    with pytest.raises(ConfigValidationError):
        load_model_dict(raw, semantic_validators=[spy])
    assert calls == []

    raw["storage"]["local_cache"]["partition_key"] = "pickup_date"
    load_model_dict(raw, semantic_validators=[spy])
    assert calls == ["nyc_yellow_taxi_analytics"]
