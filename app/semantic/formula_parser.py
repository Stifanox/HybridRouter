"""Parser formuł metryk pochodnych: tekst -> AST domenowe (ADR-009).

Formuła jest parsowana wbudowanym modułem :mod:`ast` (``mode="eval"`` – pojedyncze
wyrażenie, bez instrukcji), a następnie **konwertowana** na AST domenowe według białej
listy węzłów. Kod formuły nigdy nie jest kompilowany do kodu bajtowego ani wykonywany.

Gramatyka dopuszczalnych formuł (EBNF)::

    expr     ::= term   (("+" | "-") term)*
    term     ::= factor (("*" | "/") factor)*
    factor   ::= "-" factor | atom
    atom     ::= NUMBER | NAME | call | "(" expr ")"
    call     ::= FUNC "(" [expr ("," expr)*] ")"
    NUMBER   ::= literał int/float Pythona, skończony (bez bool, complex, napisów)
    NAME     ::= identyfikator ASCII – miara bazowa lub metryka pochodna
    FUNC     ::= nazwa z SCALAR_FUNCTIONS (bez rozróżniania wielkości liter),
                 liczba argumentów zgodna z FunctionSpec, bez argumentów nazwanych i *args

Wszystko inne (atrybuty, indeksowanie, ``**``, porównania, operatory logiczne i bitowe,
lambdy, napisy, ``True``/``None``, wyrażenia warunkowe, comprehensions, ``:=`` …) kończy
się :class:`~app.semantic.errors.FormulaSyntaxError`. Weryfikacja, czy ``NAME`` wskazuje
istniejącą miarę, należy do resolvera (wymaga znajomości całego modelu).
"""

from __future__ import annotations

import ast
import math
from typing import NoReturn

from app.semantic.ast_nodes import (
    BinaryOp,
    BinaryOperator,
    Expr,
    FunctionCall,
    Literal,
    MeasureRef,
    UnaryOp,
)
from app.semantic.errors import FormulaSyntaxError, suggest
from app.semantic.registry import (
    AGGREGATE_FUNCTION_NAMES,
    SCALAR_FUNCTIONS,
    get_scalar_function,
)

_BINARY_OPS: dict[type[ast.operator], BinaryOperator] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}

_OPERATOR_SYMBOLS: dict[type[ast.AST], str] = {
    ast.Pow: "**",
    ast.Mod: "%",
    ast.FloorDiv: "//",
    ast.MatMult: "@",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitOr: "|",
    ast.BitXor: "^",
    ast.BitAnd: "&",
    ast.UAdd: "unary +",
    ast.Not: "not",
    ast.Invert: "~",
}

_CONSTRUCT_NAMES: dict[type[ast.AST], str] = {
    ast.Attribute: "attribute access",
    ast.Subscript: "indexing/slicing",
    ast.Compare: "comparison",
    ast.BoolOp: "boolean operator",
    ast.Lambda: "lambda",
    ast.IfExp: "conditional expression",
    ast.NamedExpr: "assignment expression",
    ast.Starred: "starred expression",
    ast.JoinedStr: "f-string",
    ast.List: "list",
    ast.Tuple: "tuple",
    ast.Set: "set",
    ast.Dict: "dict",
    ast.ListComp: "comprehension",
    ast.SetComp: "comprehension",
    ast.DictComp: "comprehension",
    ast.GeneratorExp: "generator expression",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
}


class _Converter:
    """Konwersja ``ast.expr`` -> :data:`Expr` z białą listą węzłów."""

    def __init__(self, formula: str, metric: str) -> None:
        self._formula = formula
        self._metric = metric
        self._line_starts = _line_starts(formula)

    # -- błędy --------------------------------------------------------------- #

    def _fail(self, node: ast.expr, reason: str) -> NoReturn:
        position = _char_position(self._formula, self._line_starts, node.lineno, node.col_offset)
        fragment = ast.get_source_segment(self._formula, node)
        raise FormulaSyntaxError(self._metric, self._formula, reason, position, fragment)

    # -- konwersja ----------------------------------------------------------- #

    def convert(self, node: ast.expr) -> Expr:
        match node:
            case ast.BinOp(left=left, op=op, right=right):
                symbol = _BINARY_OPS.get(type(op))
                if symbol is None:
                    self._fail(node, f"operator '{_operator_name(op)}' is not allowed")
                return BinaryOp(symbol, self.convert(left), self.convert(right))
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                return UnaryOp("-", self.convert(operand))
            case ast.UnaryOp(op=op):
                self._fail(node, f"operator '{_operator_name(op)}' is not allowed")
            case ast.Constant(value=value):
                return self._constant(node, value)
            case ast.Name(id=name):
                if not name.isascii():
                    self._fail(node, f"name '{name}' must be an ASCII identifier")
                return MeasureRef(name)
            case ast.Call():
                return self._call(node)
            case _:
                construct = _CONSTRUCT_NAMES.get(type(node), type(node).__name__)
                self._fail(node, f"{construct} is not allowed")

    def _constant(self, node: ast.Constant, value: object) -> Literal:
        # bool jest podklasą int – musi zostać odrzucony jawnie, przed sprawdzeniem int.
        if isinstance(value, bool) or not isinstance(value, int | float):
            self._fail(node, f"constant {value!r} is not allowed (only int/float numbers)")
        if not math.isfinite(value):
            self._fail(node, "number is out of range (not finite)")
        return Literal(value)

    def _call(self, node: ast.Call) -> FunctionCall:
        if not isinstance(node.func, ast.Name):
            self._fail(node, "only calls of registered scalar functions are allowed")
        raw_name = node.func.id
        name = raw_name.upper()
        spec = get_scalar_function(name)
        if spec is None:
            if name in AGGREGATE_FUNCTION_NAMES:
                self._fail(
                    node,
                    f"aggregate function '{raw_name}' is not allowed in formulas; "
                    "define aggregations in base_measures and reference the measure by name",
                )
            hint = suggest(name, SCALAR_FUNCTIONS)
            reason = f"unknown function '{raw_name}'"
            reason += f"; did you mean '{hint}'?" if hint else ""
            reason += f" (allowed: {', '.join(sorted(SCALAR_FUNCTIONS))})"
            self._fail(node, reason)
        if node.keywords:
            self._fail(node, f"keyword arguments are not allowed in {name}()")
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                self._fail(arg, f"starred arguments are not allowed in {name}()")
        if not spec.accepts(len(node.args)):
            self._fail(node, f"{name}() takes {spec.arity} argument(s), got {len(node.args)}")
        return FunctionCall(name, tuple(self.convert(arg) for arg in node.args))


def parse_formula(formula: str, metric: str = "<formula>") -> Expr:
    """Parsuje formułę metryki ``metric`` do AST domenowego.

    Białe znaki z brzegów formuły są pomijane (pozycje w błędach odnoszą się do formuły
    po ich usunięciu).

    :raises FormulaSyntaxError: składnia niepoprawna lub konstrukcja spoza białej listy.
    """
    text = formula.strip()
    if not text:
        raise FormulaSyntaxError(metric, formula, "formula is empty")
    try:
        tree = ast.parse(text, mode="eval")
        return _Converter(text, metric).convert(tree.body)
    except SyntaxError as exc:
        position = None
        if exc.lineno is not None and exc.offset is not None:
            starts = _line_starts(text)
            if 1 <= exc.lineno <= len(starts):
                position = min(starts[exc.lineno - 1] + max(exc.offset - 1, 0), len(text))
        raise FormulaSyntaxError(metric, text, f"invalid syntax ({exc.msg})", position) from exc
    except ValueError as exc:  # pragma: no cover - bajt NUL w Pythonie 3.11 (3.12+: SyntaxError)
        raise FormulaSyntaxError(metric, text, f"invalid formula ({exc})") from exc
    except (RecursionError, MemoryError) as exc:
        # Patologicznie głębokie wyrażenia (np. tysiące zagnieżdżonych operatorów).
        raise FormulaSyntaxError(metric, text, "formula is too deeply nested") from exc


# --------------------------------------------------------------------------- #
# Pomocnicze
# --------------------------------------------------------------------------- #


def _operator_name(op: ast.AST) -> str:
    return _OPERATOR_SYMBOLS.get(type(op), type(op).__name__)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _char_position(text: str, line_starts: list[int], lineno: int, col_offset: int) -> int:
    """``col_offset`` z modułu ``ast`` liczony jest w bajtach UTF-8 – przelicz na znaki."""
    start = line_starts[lineno - 1]
    line = text[start : line_starts[lineno]] if lineno < len(line_starts) else text[start:]
    prefix = line.encode("utf-8")[:col_offset].decode("utf-8", errors="ignore")
    return start + len(prefix)
