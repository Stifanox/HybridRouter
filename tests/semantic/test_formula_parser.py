"""Parser formuł: poprawne formuły z nyc_taxi.yaml i odrzucanie konstrukcji spoza białej listy."""

from __future__ import annotations

from typing import Any

import pytest

from app.semantic.ast_nodes import BinaryOp, FunctionCall, Literal, MeasureRef, UnaryOp
from app.semantic.errors import FormulaSyntaxError
from app.semantic.formula_parser import _char_position, _line_starts, parse_formula

fare, tip, trips = (
    MeasureRef("total_fare_amount"),
    MeasureRef("total_tip_amount"),
    MeasureRef("trips_count"),
)


def test_all_nyc_taxi_formulas_parse(raw: dict[str, Any]) -> None:
    for name, metric in raw["derived_metrics"].items():
        parse_formula(metric["formula"], name)


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        (
            "(total_tip_amount / total_fare_amount) * 100",
            BinaryOp("*", BinaryOp("/", tip, fare), Literal(100)),
        ),
        (
            "total_revenue - total_fare_amount - total_tip_amount",
            BinaryOp("-", BinaryOp("-", MeasureRef("total_revenue"), fare), tip),
        ),
        (
            "total_distance_miles / (total_duration_seconds / 3600.0)",
            BinaryOp(
                "/",
                MeasureRef("total_distance_miles"),
                BinaryOp("/", MeasureRef("total_duration_seconds"), Literal(3600.0)),
            ),
        ),
        (
            "ROUND(total_fare_amount / trips_count, 2)",
            FunctionCall("ROUND", (BinaryOp("/", fare, trips), Literal(2))),
        ),
        ("-trips_count", UnaryOp("-", trips)),
        ("  trips_count  ", trips),
        ("(trips_count\n  + 1)", BinaryOp("+", trips, Literal(1))),
    ],
)
def test_valid_formulas(formula: str, expected: object) -> None:
    assert parse_formula(formula) == expected


@pytest.mark.parametrize("spelling", ["round", "Round", "ROUND"])
def test_function_names_are_case_insensitive(spelling: str) -> None:
    assert parse_formula(f"{spelling}(a)") == FunctionCall("ROUND", (MeasureRef("a"),))


@pytest.mark.parametrize(
    ("formula", "args"),
    [
        ("ROUND(a)", 1),
        ("ROUND(a, 2)", 2),
        ("COALESCE(a, b, c, 0)", 4),
        ("ABS(a)", 1),
        ("GREATEST(a, b)", 2),
        ("LEAST(a, b, c)", 3),
    ],
)
def test_registered_functions_with_valid_arity(formula: str, args: int) -> None:
    expr = parse_formula(formula)
    assert isinstance(expr, FunctionCall)
    assert len(expr.args) == args


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        ("__import__('os')", "unknown function '__import__'"),
        ("a.b", "attribute access is not allowed"),
        ("a[0]", "indexing/slicing is not allowed"),
        ("a ** 2", "operator '**' is not allowed"),
        ("a % 2", "operator '%' is not allowed"),
        ("a // 2", "operator '//' is not allowed"),
        ("a > b", "comparison is not allowed"),
        ("a and b", "boolean operator is not allowed"),
        ("not a", "operator 'not' is not allowed"),
        ("+a", "operator 'unary +' is not allowed"),
        ("~a", "operator '~' is not allowed"),
        ("'x'", "constant 'x' is not allowed"),
        ("b'x'", "constant b'x' is not allowed"),
        ("True", "constant True is not allowed"),
        ("None", "constant None is not allowed"),
        ("1j", "constant 1j is not allowed"),
        ("1e999", "not finite"),
        ("lambda: 1", "lambda is not allowed"),
        ("a if b else c", "conditional expression is not allowed"),
        ("(a := 1)", "assignment expression is not allowed"),
        ("[a]", "list is not allowed"),
        ("(a, b)", "tuple is not allowed"),
        ("{a: b}", "dict is not allowed"),
        ("[x for x in a]", "comprehension is not allowed"),
        ("f'{a}'", "f-string is not allowed"),
        ("SUM(a)", "aggregate function 'SUM' is not allowed in formulas"),
        ("count(a)", "define aggregations in base_measures"),
        ("AVG(a) / 2", "aggregate function 'AVG'"),
        ("ROUND()", "ROUND() takes 1..2 argument(s), got 0"),
        ("ROUND(a, 1, 2)", "ROUND() takes 1..2 argument(s), got 3"),
        ("ABS(a, b)", "ABS() takes 1 argument(s), got 2"),
        ("COALESCE(a)", "COALESCE() takes 2 or more argument(s), got 1"),
        ("ROUND(a, ndigits=2)", "keyword arguments are not allowed"),
        ("GREATEST(*a)", "starred arguments are not allowed"),
        ("ROUNDD(a)", "did you mean 'ROUND'?"),
        ("a.ROUND(b)", "only calls of registered scalar functions"),
        ("ROUND(a)(b)", "only calls of registered scalar functions"),
        ("zażółć + 1", "must be an ASCII identifier"),
        ("a +", "invalid syntax"),
        ("a = 1", "invalid syntax"),
        ("import os", "invalid syntax"),
        ("a; b", "invalid syntax"),
        ("   ", "formula is empty"),
    ],
)
def test_rejected_formulas(formula: str, reason: str) -> None:
    with pytest.raises(FormulaSyntaxError) as exc_info:
        parse_formula(formula, "my_metric")
    assert reason in exc_info.value.reason
    assert exc_info.value.metric == "my_metric"


def test_error_reports_metric_fragment_and_position() -> None:
    formula = "total_revenue + a ** 2"
    with pytest.raises(FormulaSyntaxError) as exc_info:
        parse_formula(formula, "bad_metric")
    error = exc_info.value
    assert error.metric == "bad_metric"
    assert error.fragment == "a ** 2"
    assert error.position == formula.index("a ** 2")
    message = str(error)
    assert "in 'a ** 2' at column 17 of formula 'total_revenue + a ** 2'" in message
    assert message.endswith("\n      total_revenue + a ** 2\n" + " " * 6 + " " * 16 + "^")


def test_position_counts_characters_not_utf8_bytes() -> None:
    # col_offset z modułu ast jest w bajtach UTF-8 – pozycja w błędzie ma być w znakach.
    # Konwersja przebiega od lewej, więc poprawny fragment przed błędem nie może zawierać
    # znaków spoza ASCII – helper testujemy bezpośrednio.
    text = "(a +\n ąę + x[0])"
    col_in_bytes = len(" ąę + ".encode())
    assert _char_position(text, _line_starts(text), 2, col_in_bytes) == text.index("x[0]")


def test_position_in_multiline_formula() -> None:
    formula = "(a +\n b.c)"
    with pytest.raises(FormulaSyntaxError) as exc_info:
        parse_formula(formula)
    assert exc_info.value.position == formula.index("b.c")


def test_syntax_error_has_position() -> None:
    with pytest.raises(FormulaSyntaxError) as exc_info:
        parse_formula("a + (b", "m")
    assert exc_info.value.position is not None


def test_null_byte_is_rejected() -> None:
    with pytest.raises(FormulaSyntaxError, match="invalid"):
        parse_formula("a\x00 + b")


def test_pathologically_deep_formula_is_rejected_cleanly() -> None:
    with pytest.raises(FormulaSyntaxError):
        parse_formula("-" * 100_000 + "a")
    with pytest.raises(FormulaSyntaxError):
        parse_formula("(" * 1000 + "a" + ")" * 1000)


def test_long_flat_formula_is_accepted() -> None:
    expr = parse_formula(" + ".join(["a"] * 200))
    assert isinstance(expr, BinaryOp)


def test_formula_is_never_evaluated() -> None:
    # Gdyby formuła była wykonywana, wywołanie zakończyłoby proces / rzuciło NameError.
    with pytest.raises(FormulaSyntaxError):
        parse_formula("exit(1)")
    assert parse_formula("os") == MeasureRef("os")
