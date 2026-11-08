"""Kwalifikacja do lokalnego cache: Warunki 1–3 osobno i łącznie."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from app.planner.builder import plan_query
from app.planner.logical_plan import LogicalQuery
from app.router.decision_engine import (
    MAX_REPORTED_MISSING_DAYS,
    ConditionCheck,
    check_local_eligibility,
)
from app.router.exceptions import (
    CacheMissInForcedModeError,
    GranularityNotSupportedInLocalCacheError,
    NonAdditiveMeasureInLocalCacheError,
    RoutingError,
)
from app.semantic.resolver import SemanticModel
from app.sync.partition_index import PartitionIndex, PartitionSnapshot
from tests.conftest import SYNTH_GAP, ModelFactory, make_request
from tests.sync.helpers import publish_days

COVERED = [
    {"field": "pickup_date", "op": ">=", "value": "2022-12-05"},
    {"field": "pickup_date", "op": "<=", "value": "2022-12-12"},
]
WITH_GAP = [
    {"field": "pickup_date", "op": ">=", "value": "2022-12-01"},
    {"field": "pickup_date", "op": "<=", "value": "2022-12-12"},
]


@pytest.fixture(scope="module")
def index(rollup_snapshot: PartitionSnapshot) -> PartitionSnapshot:
    return rollup_snapshot


def _plan(model: SemanticModel, **overrides: Any) -> LogicalQuery:
    base: dict[str, Any] = {
        "dimensions": ["pickup_date", "vendor_id"],
        "metrics": ["trips_count", "tip_to_fare_ratio_pct"],
        "filters": COVERED,
        "order_by": [],
        "limit": None,
    }
    base.update(overrides)
    return plan_query(make_request(**base), model)


def test_all_conditions_satisfied(nyc_model: SemanticModel, index: PartitionSnapshot) -> None:
    result = check_local_eligibility(_plan(nyc_model), nyc_model, index)
    assert result.eligible
    assert result.reason is None
    assert [c.condition for c in result.conditions] == [
        "granularity",
        "time_coverage",
        "additivity",
    ]
    assert all(c.satisfied and c.reason is None for c in result.conditions)
    assert result.missing_days == () and result.missing_day_count == 0
    assert result.to_error() is None
    result.raise_if_ineligible()


# -- Warunek 1 ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        {"dimensions": ["dropoff_location_id"]},
        {"filters": [*COVERED, {"field": "dropoff_location_id", "op": "==", "value": "7"}]},
        {
            "dimensions": ["vendor_id", "dropoff_location_id"],
            "order_by": [{"field": "dropoff_location_id", "direction": "ASC"}],
        },
    ],
    ids=["select", "filter", "order_by"],
)
def test_condition_1_granularity(
    nyc_model: SemanticModel, index: PartitionSnapshot, overrides: dict[str, Any]
) -> None:
    result = check_local_eligibility(_plan(nyc_model, **overrides), nyc_model, index)
    assert not result.granularity.satisfied
    assert result.time_coverage.satisfied and result.additivity.satisfied
    assert result.unsupported_dimensions == ("dropoff_location_id",)
    assert result.reason == "Dimension 'dropoff_location_id' not present in local rollup"
    with pytest.raises(GranularityNotSupportedInLocalCacheError) as info:
        result.raise_if_ineligible()
    assert info.value.dimensions == ("dropoff_location_id",)
    assert info.value.error_type == "GranularityNotSupportedInLocalCacheError"
    assert info.value.details == {"dimensions": ["dropoff_location_id"]}


def test_condition_1_lists_every_dimension(
    nyc_raw: dict[str, Any], model_from: ModelFactory, index: PartitionSnapshot
) -> None:
    nyc_raw["dimensions"]["vendor_id"]["in_rollup"] = False
    model = model_from(nyc_raw)
    plan = _plan(model, dimensions=["vendor_id", "dropoff_location_id"])
    result = check_local_eligibility(plan, model, index)
    assert result.unsupported_dimensions == ("vendor_id", "dropoff_location_id")
    assert len(result.granularity.reasons) == 2
    assert result.granularity.reason == (
        "Dimension 'vendor_id' not present in local rollup; "
        "Dimension 'dropoff_location_id' not present in local rollup"
    )


# -- Warunek 2 ---------------------------------------------------------------- #


def test_condition_2_gap_in_range(nyc_model: SemanticModel, index: PartitionSnapshot) -> None:
    result = check_local_eligibility(_plan(nyc_model, filters=WITH_GAP), nyc_model, index)
    assert not result.time_coverage.satisfied
    assert result.granularity.satisfied and result.additivity.satisfied
    assert result.missing_days == (SYNTH_GAP,)
    assert result.missing_day_count == 1
    assert result.reason == (
        "Local cache is missing 1 day(s) of 2022-12-01..2022-12-12: 2022-12-04"
    )
    with pytest.raises(CacheMissInForcedModeError) as info:
        result.raise_if_ineligible()
    assert info.value.missing_days == (SYNTH_GAP,)
    assert info.value.details == {
        "date_range": ["2022-12-01", "2022-12-12"],
        "missing_days": ["2022-12-04"],
        "missing_day_count": 1,
    }


def test_condition_2_range_beyond_cache(nyc_model: SemanticModel, index: PartitionSnapshot) -> None:
    filters = [
        {"field": "pickup_date", "op": ">=", "value": "2022-12-10"},
        {"field": "pickup_date", "op": "<", "value": "2023-01-01"},
    ]
    result = check_local_eligibility(_plan(nyc_model, filters=filters), nyc_model, index)
    assert result.missing_day_count == 19  # 13..31 grudnia
    assert result.missing_days[0] == dt.date(2022, 12, 13)
    assert result.reason is not None and result.reason.endswith("and 9 more")


@pytest.mark.parametrize(
    "filters",
    [
        [],
        [{"field": "pickup_date", "op": ">=", "value": "2022-12-05"}],
        [{"field": "pickup_date", "op": "<=", "value": "2022-12-05"}],
        [{"field": "pickup_date", "op": "!=", "value": "2022-12-05"}],
    ],
    ids=["none", "lower_only", "upper_only", "not_equal"],
)
def test_condition_2_unbounded_range(
    nyc_model: SemanticModel, index: PartitionSnapshot, filters: list[dict[str, Any]]
) -> None:
    result = check_local_eligibility(_plan(nyc_model, filters=filters), nyc_model, index)
    assert not result.time_coverage.satisfied
    assert result.date_range is None
    assert result.reason == (
        "Query date range is not bounded on both sides by filters on 'pickup_date'"
    )
    with pytest.raises(CacheMissInForcedModeError) as info:
        result.raise_if_ineligible()
    assert info.value.details["date_range"] is None


def test_condition_2_huge_range_is_cheap(nyc_model: SemanticModel, tmp_path: Path) -> None:
    filters = [
        {"field": "pickup_date", "op": ">=", "value": "0001-01-01"},
        {"field": "pickup_date", "op": "<=", "value": "9999-12-31"},
    ]
    result = check_local_eligibility(
        _plan(nyc_model, filters=filters),
        nyc_model,
        PartitionSnapshot(tmp_path, "pickup_date"),
    )
    assert result.missing_day_count == (dt.date.max - dt.date.min).days + 1
    assert len(result.missing_days) == MAX_REPORTED_MISSING_DAYS


# -- Warunek 3 ---------------------------------------------------------------- #


def test_condition_3_holistic_measure(nyc_model: SemanticModel, index: PartitionSnapshot) -> None:
    plan = _plan(nyc_model, metrics=["trips_count", "unique_pickup_zones"])
    result = check_local_eligibility(plan, nyc_model, index)
    assert not result.additivity.satisfied
    assert result.granularity.satisfied and result.time_coverage.satisfied
    assert result.non_additive_measures == ("unique_pickup_zones",)
    assert result.reason == (
        "Measure 'unique_pickup_zones' (COUNT_DISTINCT) cannot be re-aggregated from local "
        "rollup (used by metric 'unique_pickup_zones')"
    )
    with pytest.raises(NonAdditiveMeasureInLocalCacheError) as info:
        result.raise_if_ineligible()
    assert info.value.measures == ("unique_pickup_zones",)


def test_condition_3_inherited_by_derived_metric(
    nyc_raw: dict[str, Any], model_from: ModelFactory, index: PartitionSnapshot
) -> None:
    nyc_raw["derived_metrics"]["trips_per_zone"] = {"formula": "trips_count / unique_pickup_zones"}
    model = model_from(nyc_raw)
    result = check_local_eligibility(_plan(model, metrics=["trips_per_zone"]), model, index)
    assert result.non_additive_measures == ("unique_pickup_zones",)
    assert result.additivity.reason is not None
    assert "used by metric 'trips_per_zone'" in result.additivity.reason


# -- Łącznie ------------------------------------------------------------------ #


def test_all_conditions_evaluated_and_first_failure_raised(
    nyc_model: SemanticModel, index: PartitionSnapshot
) -> None:
    plan = _plan(
        nyc_model,
        dimensions=["dropoff_location_id"],
        metrics=["unique_pickup_zones"],
        filters=WITH_GAP,
    )
    result = check_local_eligibility(plan, nyc_model, index)
    assert [c.satisfied for c in result.conditions] == [False, False, False]
    assert result.reason == result.granularity.reason
    assert isinstance(result.to_error(), GranularityNotSupportedInLocalCacheError)

    without_granularity = check_local_eligibility(
        _plan(nyc_model, metrics=["unique_pickup_zones"], filters=WITH_GAP), nyc_model, index
    )
    assert isinstance(without_granularity.to_error(), CacheMissInForcedModeError)


def test_new_publication_changes_outcome_only_for_new_snapshot(
    nyc_model: SemanticModel, tmp_path: Path
) -> None:
    index = PartitionIndex(tmp_path, "pickup_date")
    plan = _plan(nyc_model, filters=[{"field": "pickup_date", "op": "==", "value": "2022-12-05"}])
    before = index.snapshot()
    assert not check_local_eligibility(plan, nyc_model, before).eligible
    publish_days(tmp_path, [dt.date(2022, 12, 5)])
    after = index.snapshot()
    assert check_local_eligibility(plan, nyc_model, after).eligible
    # Migawka pobrana wcześniej (trwające zapytanie) nie zmienia się po publikacji.
    assert not check_local_eligibility(plan, nyc_model, before).eligible


def test_routing_errors_share_base(nyc_model: SemanticModel, index: PartitionSnapshot) -> None:
    error = check_local_eligibility(_plan(nyc_model, filters=WITH_GAP), nyc_model, index).to_error()
    assert isinstance(error, RoutingError)
    assert str(error) == error.message


def test_condition_check_reason() -> None:
    assert ConditionCheck("granularity", True).reason is None
    assert ConditionCheck("additivity", False, ("a", "b")).reason == "a; b"


# -- Błędy programistyczne ------------------------------------------------------ #


def test_model_mismatch(
    nyc_model: SemanticModel,
    nyc_raw: dict[str, Any],
    model_from: ModelFactory,
    index: PartitionSnapshot,
) -> None:
    nyc_raw["model_name"] = "other"
    with pytest.raises(ValueError, match="checked against 'other'"):
        check_local_eligibility(_plan(nyc_model), model_from(nyc_raw), index)


def test_partition_key_mismatch(nyc_model: SemanticModel, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="partition key mismatch"):
        check_local_eligibility(_plan(nyc_model), nyc_model, PartitionSnapshot(tmp_path, "day"))
