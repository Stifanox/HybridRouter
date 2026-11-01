"""Resolver: rozwijanie metryk do miar bazowych, cykle, nieznane nazwy, wymiary."""

from __future__ import annotations

from typing import Any

import pytest

from app.config.loader import load_model_file
from app.semantic.ast_nodes import BinaryOp, Literal, MeasureRef, to_formula
from app.semantic.errors import (
    CircularDependencyError,
    ConstantFormulaError,
    DimensionInFormulaError,
    DivisionByZeroLiteralError,
    FormulaSyntaxError,
    UnknownAggregationError,
    UnknownMeasureError,
    suggest,
)
from app.semantic.formula_parser import parse_formula
from app.semantic.resolver import SemanticModel, analyze, build_semantic_model, substitute

from .conftest import NYC_TAXI, ConfigFactory


@pytest.fixture(scope="module")
def nyc() -> SemanticModel:
    return build_semantic_model(load_model_file(NYC_TAXI))


def _with_metrics(raw: dict[str, Any], **formulas: str) -> dict[str, Any]:
    raw["derived_metrics"] = {name: {"formula": f} for name, f in formulas.items()}
    return raw


def test_multilevel_metric_expands_to_base_measures(nyc: SemanticModel) -> None:
    metric = nyc.get_metric("surcharge_to_revenue_roi_pct")
    revenue, fare, tip = (
        MeasureRef(n) for n in ("total_revenue", "total_fare_amount", "total_tip_amount")
    )
    surcharges = BinaryOp("-", BinaryOp("-", revenue, fare), tip)

    assert metric.kind == "derived"
    assert metric.formula == parse_formula("(non_fare_surcharges / total_revenue) * 100")
    assert metric.expanded == BinaryOp("*", BinaryOp("/", surcharges, revenue), Literal(100))
    assert metric.base_measures == {"total_revenue", "total_fare_amount", "total_tip_amount"}
    assert metric.additive is True
    assert to_formula(metric.expanded) == (
        "(total_revenue - total_fare_amount - total_tip_amount) / total_revenue * 100"
    )


def test_every_metric_expands_to_base_measures_only(nyc: SemanticModel) -> None:
    base = set(nyc.config.base_measures)
    assert set(nyc.metrics) == base | set(nyc.config.derived_metrics)
    for metric in nyc.metrics.values():
        refs = {n.name for n in _refs(metric.expanded)}
        assert refs == metric.base_measures
        assert refs <= base


def _refs(expr: Any) -> list[MeasureRef]:
    from app.semantic.ast_nodes import walk

    return [n for n in walk(expr) if isinstance(n, MeasureRef)]


def test_base_measures_are_uniform_metrics(nyc: SemanticModel) -> None:
    metric = nyc.get_metric("trips_count")
    assert metric.kind == "base"
    assert metric.formula == metric.expanded == MeasureRef("trips_count")
    assert metric.base_measures == {"trips_count"}
    assert metric.additive is True
    assert nyc.get_metric("unique_pickup_zones").additive is False


def test_metric_order_is_base_then_derived(nyc: SemanticModel) -> None:
    names = list(nyc.metrics)
    assert names[: len(nyc.config.base_measures)] == list(nyc.config.base_measures)
    assert names[len(nyc.config.base_measures) :] == list(nyc.config.derived_metrics)
    assert nyc.name == "nyc_yellow_taxi_analytics"


def test_semantic_model_is_read_only(nyc: SemanticModel) -> None:
    with pytest.raises(TypeError):
        nyc.metrics["x"] = nyc.metrics["trips_count"]  # type: ignore[index]


def test_get_metric_unknown_suggests(nyc: SemanticModel) -> None:
    with pytest.raises(UnknownMeasureError, match="did you mean 'total_revenue'"):
        nyc.get_metric("total_revenu")


def test_shared_dependency_is_expanded_once_and_reused(
    raw: dict[str, Any], structural: ConfigFactory
) -> None:
    model = build_semantic_model(
        structural(_with_metrics(raw, x="trips_count + 1", y="x * x", z="y - x"))
    )
    assert to_formula(model.get_metric("z").expanded) == (
        "(trips_count + 1) * (trips_count + 1) - (trips_count + 1)"
    )


def test_two_element_cycle(raw: dict[str, Any], structural: ConfigFactory) -> None:
    config = structural(_with_metrics(raw, a="b + trips_count", b="a * 2"))
    with pytest.raises(CircularDependencyError) as exc_info:
        build_semantic_model(config)
    assert exc_info.value.cycle == ("a", "b", "a")
    assert "circular dependency: a -> b -> a" in str(exc_info.value)


def test_three_element_cycle(raw: dict[str, Any], structural: ConfigFactory) -> None:
    config = structural(
        _with_metrics(raw, ok="trips_count", a="ok + b", b="c / 2", c="ROUND(a, 1)")
    )
    resolution = analyze(config)
    assert resolution.model is None
    assert [str(e) for e in resolution.errors] == ["circular dependency: a -> b -> c -> a"]


def test_self_reference(raw: dict[str, Any], structural: ConfigFactory) -> None:
    with pytest.raises(CircularDependencyError, match="a -> a"):
        build_semantic_model(structural(_with_metrics(raw, a="a + 1")))


def test_metric_depending_on_cycle_reports_only_the_cycle(
    raw: dict[str, Any], structural: ConfigFactory
) -> None:
    config = structural(_with_metrics(raw, top="a + 1", a="b", b="a"))
    errors = analyze(config).errors
    assert [type(e) for e in errors] == [CircularDependencyError]
    assert errors[0].cycle == ("a", "b", "a")  # type: ignore[attr-defined]


def test_unknown_name_with_suggestion(raw: dict[str, Any], structural: ConfigFactory) -> None:
    config = structural(_with_metrics(raw, m="total_tip_amout / total_fare_amount"))
    with pytest.raises(UnknownMeasureError) as exc_info:
        build_semantic_model(config)
    error = exc_info.value
    assert (error.metric, error.name, error.suggestion) == (
        "m",
        "total_tip_amout",
        "total_tip_amount",
    )
    assert "did you mean 'total_tip_amount'?" in str(error)


def test_unknown_name_suggests_derived_metrics_too(
    raw: dict[str, Any], structural: ConfigFactory
) -> None:
    config = structural(_with_metrics(raw, ratio="trips_count", m="ratoi * 2"))
    with pytest.raises(UnknownMeasureError, match="did you mean 'ratio'"):
        build_semantic_model(config)


def test_unknown_name_without_close_match(raw: dict[str, Any], structural: ConfigFactory) -> None:
    config = structural(_with_metrics(raw, m="qwerty + 1"))
    with pytest.raises(UnknownMeasureError) as exc_info:
        build_semantic_model(config)
    assert exc_info.value.suggestion is None
    assert "did you mean" not in str(exc_info.value)


def test_case_mismatch_is_unknown_but_suggested(
    raw: dict[str, Any], structural: ConfigFactory
) -> None:
    config = structural(_with_metrics(raw, m="Trips_Count * 2"))
    with pytest.raises(UnknownMeasureError, match="did you mean 'trips_count'"):
        build_semantic_model(config)


def test_dimension_in_formula_is_rejected(raw: dict[str, Any], structural: ConfigFactory) -> None:
    config = structural(_with_metrics(raw, m="total_revenue / vendor_id"))
    with pytest.raises(DimensionInFormulaError, match="'vendor_id' is a dimension"):
        build_semantic_model(config)


def test_constant_formula_is_rejected(raw: dict[str, Any], structural: ConfigFactory) -> None:
    with pytest.raises(ConstantFormulaError, match="at least one measure"):
        build_semantic_model(structural(_with_metrics(raw, m="ROUND(100 / 3, 2)")))


def test_all_errors_are_collected(raw: dict[str, Any], structural: ConfigFactory) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "AVG"
    config = structural(
        _with_metrics(raw, s="a ** 2", u="nope", d="pickup_date", c1="c2", c2="c1", dep="s + 1")
    )
    kinds = [type(e) for e in analyze(config).errors]
    assert kinds == [
        UnknownAggregationError,
        FormulaSyntaxError,
        UnknownMeasureError,
        DimensionInFormulaError,
        CircularDependencyError,
    ]


def test_unknown_aggregation_is_reported(raw: dict[str, Any], structural: ConfigFactory) -> None:
    raw["base_measures"]["total_tip_amount"]["aggregation"] = "SUMM"
    with pytest.raises(UnknownAggregationError, match="did you mean 'SUM'"):
        build_semantic_model(structural(raw))


def test_model_without_derived_metrics(raw: dict[str, Any], structural: ConfigFactory) -> None:
    del raw["derived_metrics"]
    model = build_semantic_model(structural(raw))
    assert set(model.metrics) == set(raw["base_measures"])


def test_substitute_replaces_only_listed_refs() -> None:
    expr = parse_formula("ROUND(-a / b, 2)")
    out = substitute(expr, {"a": parse_formula("x + y")})
    assert to_formula(out) == "ROUND(-(x + y) / b, 2)"
    assert substitute(expr, {}) == expr


@pytest.mark.parametrize(
    ("name", "expected"),
    [("TOTAL_REVENUE", "total_revenue"), ("total_revnue", "total_revenue"), ("zzz", None)],
)
def test_suggest(name: str, expected: str | None) -> None:
    assert suggest(name, ["total_revenue", "trips_count"]) == expected


@pytest.mark.parametrize(
    "formula", ["total_tip_amount / 0", "total_tip_amount / -0.0", "(total_tip_amount / 0) * 100"]
)
def test_division_by_constant_zero_rejected(
    raw: dict[str, Any], structural: ConfigFactory, formula: str
) -> None:
    raw["derived_metrics"]["broken"] = {"formula": formula}
    with pytest.raises(DivisionByZeroLiteralError, match="division by constant zero"):
        build_semantic_model(structural(raw))


def test_division_by_nonzero_constant_accepted(
    raw: dict[str, Any], structural: ConfigFactory
) -> None:
    raw["derived_metrics"]["ok"] = {"formula": "total_tip_amount / -3600.0 + total_tip_amount * 0"}
    assert build_semantic_model(structural(raw)).get_metric("ok").additive
