"""Rejestry agregacji i funkcji skalarnych (ADR-010, ADR-011)."""

from __future__ import annotations

import pytest

from app.semantic.registry import (
    AGGREGATE_FUNCTION_NAMES,
    AGGREGATIONS,
    FORBIDDEN_AGGREGATIONS,
    SCALAR_FUNCTIONS,
    AggregationSpec,
    FunctionSpec,
    _index,
    get_aggregation,
    get_scalar_function,
    is_additive,
)


def test_aggregations_content() -> None:
    assert {name: spec.rollup_reaggregate for name, spec in AGGREGATIONS.items()} == {
        "SUM": "SUM",
        "COUNT": "SUM",
        "MIN": "MIN",
        "MAX": "MAX",
        "COUNT_DISTINCT": None,
    }
    assert all(spec.description for spec in AGGREGATIONS.values())


def test_empty_input_value_only_for_count() -> None:
    """COUNT na pustym zbiorze = 0, a jego re-agregacja SUM dałaby NULL (A4)."""
    assert {name: spec.empty_input_value for name, spec in AGGREGATIONS.items()} == {
        "SUM": None,
        "COUNT": 0,
        "MIN": None,
        "MAX": None,
        "COUNT_DISTINCT": None,
    }


def test_scalar_functions_content() -> None:
    assert {n: (s.min_args, s.max_args) for n, s in SCALAR_FUNCTIONS.items()} == {
        "ROUND": (1, 2),
        "COALESCE": (2, None),
        "ABS": (1, 1),
        "GREATEST": (2, None),
        "LEAST": (2, None),
    }


def test_avg_is_forbidden_not_registered() -> None:
    assert "AVG" not in AGGREGATIONS
    assert "SUM/COUNT" in FORBIDDEN_AGGREGATIONS["AVG"]
    assert set(AGGREGATIONS) | set(FORBIDDEN_AGGREGATIONS) <= AGGREGATE_FUNCTION_NAMES
    assert not AGGREGATE_FUNCTION_NAMES & set(SCALAR_FUNCTIONS)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("SUM", True), ("count", True), ("Min", True), ("MAX", True), ("COUNT_DISTINCT", False)],
)
def test_is_additive(name: str, expected: bool) -> None:
    assert is_additive(name) is expected
    spec = get_aggregation(name)
    assert spec is not None and spec.additive is expected


def test_is_additive_unknown_raises() -> None:
    with pytest.raises(KeyError, match="AVG"):
        is_additive("AVG")


def test_lookup_is_case_insensitive() -> None:
    assert get_scalar_function("round") is SCALAR_FUNCTIONS["ROUND"]
    assert get_scalar_function("SQRT") is None
    assert get_aggregation("median") is None


def test_registries_are_read_only() -> None:
    with pytest.raises(TypeError):
        AGGREGATIONS["AVG"] = AggregationSpec("AVG", None, "")  # type: ignore[index]
    with pytest.raises(TypeError):
        SCALAR_FUNCTIONS["SQRT"] = FunctionSpec("SQRT", 1, 1, "")  # type: ignore[index]


@pytest.mark.parametrize(
    ("spec", "counts", "arity"),
    [
        (FunctionSpec("F", 1, 2, ""), {0: False, 1: True, 2: True, 3: False}, "1..2"),
        (FunctionSpec("F", 2, None, ""), {1: False, 2: True, 50: True}, "2 or more"),
        (FunctionSpec("F", 1, 1, ""), {0: False, 1: True, 2: False}, "1"),
    ],
)
def test_function_spec_arity(spec: FunctionSpec, counts: dict[int, bool], arity: str) -> None:
    assert {n: spec.accepts(n) for n in counts} == counts
    assert spec.arity == arity


def test_function_spec_rejects_invalid_arity() -> None:
    with pytest.raises(ValueError):
        FunctionSpec("F", 2, 1, "")
    with pytest.raises(ValueError):
        FunctionSpec("F", -1, None, "")


def test_dialect_renderers_extension_point() -> None:
    def render(*args: object) -> str:
        return "x"

    spec = FunctionSpec("F", 1, 1, "", dialect_renderers={"duckdb": render})
    assert spec.dialect_renderers["duckdb"] is render
    assert spec == FunctionSpec("F", 1, 1, "")  # renderery nie uczestniczą w porównaniu
    assert hash(spec) == hash(FunctionSpec("F", 1, 1, ""))
    assert SCALAR_FUNCTIONS["ROUND"].dialect_renderers == {}


def test_index_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _index([FunctionSpec("F", 1, 1, ""), FunctionSpec("f", 1, 1, "")])
