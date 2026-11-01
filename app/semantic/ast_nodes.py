"""AST domenowe formuł metryk (ADR-009).

Niewielkie, niemutowalne drzewo wyrażeń niezależne od SQL i od biblioteki ``sqlglot``:

* :class:`MeasureRef` – odwołanie do miary bazowej lub metryki pochodnej (po nazwie),
* :class:`Literal` – nieujemna stała liczbowa (negacja to zawsze :class:`UnaryOp`),
* :class:`BinaryOp` – operator ``+ - * /``,
* :class:`UnaryOp` – jednoargumentowy minus,
* :class:`FunctionCall` – wywołanie funkcji skalarnej z rejestru ``SCALAR_FUNCTIONS``.

Przetwarzanie drzewa odbywa się wzorcem *Visitor* (:class:`Visitor`). Nowa konstrukcja
języka to nowa klasa węzła, rozszerzenie unii :data:`Expr` i nowa metoda w visitorach –
``mypy`` wskaże wtedy wszystkie miejsca wymagające uzupełnienia (``assert_never``).

Moduł celowo **nie importuje** ``sqlglot`` – renderowanie do SQL (w tym ochrona dzielenia
``NULLIF``) jest zadaniem rendererów (etap A3).
"""

from __future__ import annotations

import math
import typing
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar, assert_never

BinaryOperator: TypeAlias = typing.Literal["+", "-", "*", "/"]
UnaryOperator: TypeAlias = typing.Literal["-"]

BINARY_OPERATORS: frozenset[str] = frozenset(typing.get_args(BinaryOperator))
UNARY_OPERATORS: frozenset[str] = frozenset(typing.get_args(UnaryOperator))


# --------------------------------------------------------------------------- #
# Węzły
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MeasureRef:
    """Odwołanie do miary po nazwie (przed rozwinięciem: miara bazowa lub metryka pochodna;
    po rozwinięciu przez resolver: wyłącznie miara bazowa)."""

    name: str


@dataclass(frozen=True, slots=True)
class Literal:
    """Skończona, nieujemna stała liczbowa.

    Niezmiennik „nieujemności” daje jednoznaczną postać kanoniczną: ``-2`` jest zawsze
    ``UnaryOp("-", Literal(2))`` – dokładnie tak, jak zwraca to parser formuł.
    """

    value: int | float

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            raise TypeError(f"Literal value must be int or float, got {type(self.value).__name__}")
        if not math.isfinite(self.value):
            raise ValueError(f"Literal value must be finite, got {self.value!r}")
        if self.value < 0:
            raise ValueError(f"Literal value must be non-negative (use UnaryOp), got {self.value}")


@dataclass(frozen=True, slots=True)
class BinaryOp:
    """Operator dwuargumentowy ``left <op> right``."""

    op: BinaryOperator
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.op not in BINARY_OPERATORS:
            raise ValueError(f"unsupported binary operator {self.op!r}")


@dataclass(frozen=True, slots=True)
class UnaryOp:
    """Operator jednoargumentowy (wyłącznie minus)."""

    op: UnaryOperator
    operand: Expr

    def __post_init__(self) -> None:
        if self.op not in UNARY_OPERATORS:
            raise ValueError(f"unsupported unary operator {self.op!r}")


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """Wywołanie funkcji skalarnej. ``name`` w postaci kanonicznej (wielkie litery),
    ``args`` jako krotka – węzeł pozostaje niemutowalny i haszowalny."""

    name: str
    args: tuple[Expr, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.args, tuple):
            raise TypeError("FunctionCall.args must be a tuple")


Expr: TypeAlias = MeasureRef | Literal | BinaryOp | UnaryOp | FunctionCall
"""Dowolny węzeł AST domenowego."""


# --------------------------------------------------------------------------- #
# Visitor
# --------------------------------------------------------------------------- #

T = TypeVar("T")


class Visitor(ABC, Generic[T]):
    """Bazowy visitor AST domenowego zwracający wartość typu ``T``.

    :meth:`visit` rozdziela węzły na metody ``visit_<węzeł>``; podklasa musi
    zaimplementować wszystkie z nich (``ABC``), więc pominięcie węzła jest wykrywane
    już przy tworzeniu instancji, a nie w trakcie przetwarzania.
    """

    def visit(self, expr: Expr) -> T:
        match expr:
            case MeasureRef():
                return self.visit_measure_ref(expr)
            case Literal():
                return self.visit_literal(expr)
            case BinaryOp():
                return self.visit_binary_op(expr)
            case UnaryOp():
                return self.visit_unary_op(expr)
            case FunctionCall():
                return self.visit_function_call(expr)
            case _:  # pragma: no cover - wyczerpanie unii sprawdza mypy
                assert_never(expr)

    @abstractmethod
    def visit_measure_ref(self, node: MeasureRef) -> T: ...

    @abstractmethod
    def visit_literal(self, node: Literal) -> T: ...

    @abstractmethod
    def visit_binary_op(self, node: BinaryOp) -> T: ...

    @abstractmethod
    def visit_unary_op(self, node: UnaryOp) -> T: ...

    @abstractmethod
    def visit_function_call(self, node: FunctionCall) -> T: ...


class MeasureRefCollector(Visitor[frozenset[str]]):
    """Zbiór nazw wszystkich ``MeasureRef`` w wyrażeniu."""

    def visit_measure_ref(self, node: MeasureRef) -> frozenset[str]:
        return frozenset({node.name})

    def visit_literal(self, node: Literal) -> frozenset[str]:
        return frozenset()

    def visit_binary_op(self, node: BinaryOp) -> frozenset[str]:
        return self.visit(node.left) | self.visit(node.right)

    def visit_unary_op(self, node: UnaryOp) -> frozenset[str]:
        return self.visit(node.operand)

    def visit_function_call(self, node: FunctionCall) -> frozenset[str]:
        return frozenset().union(*(self.visit(arg) for arg in node.args))


def measure_refs(expr: Expr) -> frozenset[str]:
    """Nazwy miar, do których wyrażenie odwołuje się bezpośrednio."""
    return MeasureRefCollector().visit(expr)


def walk(expr: Expr) -> Iterator[Expr]:
    """Węzły wyrażenia w porządku pre-order (od lewej do prawej, jak w zapisie formuły)."""
    yield expr
    match expr:
        case BinaryOp(left=left, right=right):
            yield from walk(left)
            yield from walk(right)
        case UnaryOp(operand=operand):
            yield from walk(operand)
        case FunctionCall(args=args):
            for arg in args:
                yield from walk(arg)
        case _:
            pass


# --------------------------------------------------------------------------- #
# Zapis tekstowy (minimalne nawiasy)
# --------------------------------------------------------------------------- #

_PREC_ADD = 1
_PREC_MUL = 2
_PREC_UNARY = 3
_PREC_ATOM = 4

_BINARY_PRECEDENCE: dict[str, int] = {
    "+": _PREC_ADD,
    "-": _PREC_ADD,
    "*": _PREC_MUL,
    "/": _PREC_MUL,
}


class _FormulaPrinter(Visitor[tuple[str, int]]):
    """Zwraca parę (tekst, priorytet wyrażenia) – priorytet decyduje o nawiasach u rodzica."""

    def visit_measure_ref(self, node: MeasureRef) -> tuple[str, int]:
        return node.name, _PREC_ATOM

    def visit_literal(self, node: Literal) -> tuple[str, int]:
        return repr(node.value), _PREC_ATOM  # repr(float) – najkrótszy zapis odwracalny

    def visit_binary_op(self, node: BinaryOp) -> tuple[str, int]:
        prec = _BINARY_PRECEDENCE[node.op]
        left, left_prec = self.visit(node.left)
        right, right_prec = self.visit(node.right)
        # Operatory są lewostronnie łączne: lewy argument o tym samym priorytecie nie
        # wymaga nawiasów, prawy – tak (a - (b - c) != a - b - c; dla + i * zachowujemy
        # nawiasy, by parse(to_formula(x)) odtwarzało identyczne drzewo).
        if left_prec < prec:
            left = f"({left})"
        if right_prec <= prec:
            right = f"({right})"
        return f"{left} {node.op} {right}", prec

    def visit_unary_op(self, node: UnaryOp) -> tuple[str, int]:
        operand, operand_prec = self.visit(node.operand)
        # ``-(-a)`` zamiast ``--a`` – czytelniej, a drzewo po ponownym parsowaniu identyczne.
        if operand_prec <= _PREC_UNARY:
            operand = f"({operand})"
        return f"{node.op}{operand}", _PREC_UNARY

    def visit_function_call(self, node: FunctionCall) -> tuple[str, int]:
        args = ", ".join(self.visit(arg)[0] for arg in node.args)
        return f"{node.name}({args})", _PREC_ATOM


def to_formula(expr: Expr) -> str:
    """Czytelny zapis wyrażenia z minimalną liczbą nawiasów.

    Dla każdego drzewa zwróconego przez parser formuł zachodzi
    ``parse_formula(to_formula(x)) == x``.
    """
    return _FormulaPrinter().visit(expr)[0]
