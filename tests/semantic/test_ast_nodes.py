"""AST domenowe: niemutowalność, niezmienniki węzłów, Visitor, to_formula (round-trip)."""

from __future__ import annotations

import dataclasses
import random
import subprocess
import sys

import pytest

from app.semantic.ast_nodes import (
    BinaryOp,
    Expr,
    FunctionCall,
    Literal,
    MeasureRef,
    UnaryOp,
    Visitor,
    measure_refs,
    to_formula,
    walk,
)
from app.semantic.formula_parser import parse_formula
from app.semantic.registry import SCALAR_FUNCTIONS

a, b, c = MeasureRef("a"), MeasureRef("b"), MeasureRef("c")


def test_semantic_layer_does_not_import_sqlglot() -> None:
    """ADR-009: AST domenowe (i cała warstwa semantyczna) nie zależy od sqlglot."""
    code = (
        "import sys, app.semantic.ast_nodes, app.semantic.validator; "
        "sys.exit('sqlglot' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_nodes_are_frozen_hashable_and_slotted() -> None:
    expr = BinaryOp("+", a, FunctionCall("ROUND", (b, Literal(2))))
    with pytest.raises(dataclasses.FrozenInstanceError):
        expr.op = "-"  # type: ignore[misc]
    assert not hasattr(a, "__dict__")
    assert hash(expr) == hash(BinaryOp("+", a, FunctionCall("ROUND", (b, Literal(2)))))
    assert expr == BinaryOp("+", MeasureRef("a"), FunctionCall("ROUND", (b, Literal(2))))


@pytest.mark.parametrize("value", [True, "1", None])
def test_literal_rejects_non_numbers(value: object) -> None:
    with pytest.raises(TypeError):
        Literal(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, -0.5, float("inf"), float("nan")])
def test_literal_rejects_negative_and_non_finite(value: float) -> None:
    with pytest.raises(ValueError):
        Literal(value)


def test_operator_validation() -> None:
    with pytest.raises(ValueError, match="binary operator"):
        BinaryOp("**", a, b)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unary operator"):
        UnaryOp("+", a)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tuple"):
        FunctionCall("ABS", [a])  # type: ignore[arg-type]


class _NodeCounter(Visitor[int]):
    def visit_measure_ref(self, node: MeasureRef) -> int:
        return 1

    def visit_literal(self, node: Literal) -> int:
        return 1

    def visit_binary_op(self, node: BinaryOp) -> int:
        return 1 + self.visit(node.left) + self.visit(node.right)

    def visit_unary_op(self, node: UnaryOp) -> int:
        return 1 + self.visit(node.operand)

    def visit_function_call(self, node: FunctionCall) -> int:
        return 1 + sum(self.visit(arg) for arg in node.args)


def test_visitor_dispatches_every_node_type() -> None:
    expr = UnaryOp("-", BinaryOp("*", a, FunctionCall("GREATEST", (b, Literal(1)))))
    assert _NodeCounter().visit(expr) == 6
    assert len(list(walk(expr))) == 6


def test_visitor_subclass_must_implement_all_methods() -> None:
    class Incomplete(Visitor[int]):
        def visit_measure_ref(self, node: MeasureRef) -> int:
            return 0

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()  # type: ignore[abstract]


def test_measure_refs_and_walk_order() -> None:
    expr = BinaryOp("+", c, FunctionCall("COALESCE", (UnaryOp("-", a), c, Literal(0))))
    assert measure_refs(expr) == {"a", "c"}
    names = [n.name for n in walk(expr) if isinstance(n, MeasureRef)]
    assert names == ["c", "a", "c"]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (BinaryOp("+", BinaryOp("+", a, b), c), "a + b + c"),
        (BinaryOp("+", a, BinaryOp("+", b, c)), "a + (b + c)"),
        (BinaryOp("-", a, BinaryOp("-", b, c)), "a - (b - c)"),
        (BinaryOp("*", BinaryOp("+", a, b), c), "(a + b) * c"),
        (BinaryOp("+", a, BinaryOp("*", b, c)), "a + b * c"),
        (BinaryOp("/", a, BinaryOp("/", b, Literal(3600.0))), "a / (b / 3600.0)"),
        (UnaryOp("-", a), "-a"),
        (UnaryOp("-", UnaryOp("-", a)), "-(-a)"),
        (UnaryOp("-", BinaryOp("*", a, b)), "-(a * b)"),
        (BinaryOp("*", UnaryOp("-", a), b), "-a * b"),
        (BinaryOp("-", a, UnaryOp("-", b)), "a - -b"),
        (FunctionCall("ROUND", (BinaryOp("/", a, b), Literal(2))), "ROUND(a / b, 2)"),
        (Literal(1e-05), "1e-05"),
    ],
)
def test_to_formula_uses_minimal_parentheses(expr: Expr, expected: str) -> None:
    assert to_formula(expr) == expected
    assert parse_formula(to_formula(expr)) == expr


@pytest.mark.parametrize(
    "formula",
    [
        "(total_tip_amount / total_fare_amount) * 100",
        "total_revenue - total_fare_amount - total_tip_amount",
        "total_distance_miles / (total_duration_seconds / 3600.0)",
        "ROUND(total_fare_amount / trips_count, 2)",
        "-(a - (b + c)) * -d / round(e)",
        "coalesce(a, 0) + GREATEST(a, b, c, 1.5) - least(abs(-a), 2)",
    ],
)
def test_round_trip_of_formulas(formula: str) -> None:
    expr = parse_formula(formula)
    assert parse_formula(to_formula(expr)) == expr


def _random_expr(rng: random.Random, depth: int) -> Expr:
    if depth == 0 or rng.random() < 0.2:
        if rng.random() < 0.5:
            return MeasureRef(rng.choice("abcde"))
        return Literal(rng.choice([0, 1, 7, 2.5, 3600.0, 1e-07, 10**20]))
    kind = rng.randrange(3)
    if kind == 0:
        op = rng.choice(["+", "-", "*", "/"])
        return BinaryOp(op, _random_expr(rng, depth - 1), _random_expr(rng, depth - 1))  # type: ignore[arg-type]
    if kind == 1:
        return UnaryOp("-", _random_expr(rng, depth - 1))
    spec = rng.choice(list(SCALAR_FUNCTIONS.values()))
    count = rng.randint(spec.min_args, spec.max_args or spec.min_args + 2)
    return FunctionCall(spec.name, tuple(_random_expr(rng, depth - 1) for _ in range(count)))


def test_round_trip_property_on_random_trees() -> None:
    rng = random.Random(2026)
    for _ in range(500):
        expr = _random_expr(rng, depth=5)
        assert parse_formula(to_formula(expr)) == expr, to_formula(expr)
