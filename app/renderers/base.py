"""Wspólna logika rendererów: plan logiczny -> drzewo ``sqlglot`` -> SQL (ADR-008).

Zapytanie jest budowane wyłącznie z węzłów ``sqlglot.exp`` – nigdy przez sklejanie
napisów. Tekst SQL powstaje dopiero na końcu, w :meth:`SqlRenderer.render`, przez
``expression.sql(dialect=...)``.

Szkielet zapytania (wspólny dla obu typów źródła)::

    SELECT <wymiary>, <metryki> FROM <źródło>
    WHERE <filtry, AND>  GROUP BY <wymiary>  ORDER BY <aliasy>  LIMIT <n>

Podklasy decydują wyłącznie o trzech rzeczach (wzorzec *Template Method*):

* :meth:`SqlRenderer.source` – klauzula ``FROM``,
* :meth:`SqlRenderer.dimension_expr` – wyrażenie wymiaru (``WHERE`` i ``GROUP BY``),
* :meth:`SqlRenderer.measure_expr` – liść ``MeasureRef`` (agregat miary bazowej).

Metryki są tłumaczone visitorem AST domenowego (:class:`MetricExprBuilder`), który:

* opakowuje w nawiasy każdy argument operatora będący innym operatorem (jednoznaczny
  zapis niezależny od reguł priorytetów generatora),
* chroni dzielenie: ``A / B`` -> ``A / NULLIF(B, 0)``; wyjątek: ``B`` jest stałą niezerową
  (``Literal`` lub minus z niezerowego ``Literal``, np. ``/ 3600.0``),
* renderuje funkcje skalarne przez :data:`SCALAR_FUNCTION_BUILDERS` lub nadpisanie per
  dialekt z rejestru (``FunctionSpec.dialect_renderers``).
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from types import MappingProxyType

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.config.schemas import BaseMeasureConfig
from app.planner.logical_plan import (
    FilterOperator,
    FilterValue,
    LogicalQuery,
    PlannedDimension,
    PlannedFilter,
    PlannedOrder,
)
from app.renderers.errors import SqlFragmentError, UnsupportedFunctionError
from app.semantic.ast_nodes import (
    BinaryOp,
    Expr,
    FunctionCall,
    Literal,
    MeasureRef,
    UnaryOp,
    Visitor,
)
from app.semantic.registry import get_scalar_function

SOURCE_DIALECT = "bigquery"
"""Dialekt fragmentów ``sql`` w YAML – jedyne miejsce transpilacji (ADR-008)."""

AggregateBuilder = Callable[[exp.Expr], exp.Expr]
FunctionBuilder = Callable[[Sequence[exp.Expr]], exp.Expr]
"""Kontrakt pola ``FunctionSpec.dialect_renderers`` (rejestr A2): wyrenderowane argumenty
-> węzeł ``sqlglot``. Wpis dla dialektu nadpisuje domyślny builder."""


# --------------------------------------------------------------------------- #
# Agregacje i funkcje skalarne
# --------------------------------------------------------------------------- #


def _count_distinct(arg: exp.Expr) -> exp.Expr:
    return exp.Count(this=exp.Distinct(expressions=[arg]))


AGGREGATE_BUILDERS: Mapping[str, AggregateBuilder] = MappingProxyType(
    {
        "SUM": lambda arg: exp.Sum(this=arg),
        "COUNT": lambda arg: exp.Count(this=arg),
        "MIN": lambda arg: exp.Min(this=arg),
        "MAX": lambda arg: exp.Max(this=arg),
        "COUNT_DISTINCT": _count_distinct,
    }
)
"""Nazwa agregacji z rejestru ``AGGREGATIONS`` (także ``rollup_reaggregate``) -> węzeł."""


def _null_propagating(cls: type[exp.Greatest] | type[exp.Least]) -> FunctionBuilder:
    # BigQuery: GREATEST/LEAST zwracają NULL, gdy którykolwiek argument jest NULL;
    # DuckDB pomija NULL. ``ignore_nulls=False`` utrwala semantykę BigQuery w drzewie,
    # a generator sqlglot dla DuckDB dopisuje wtedy jawny CASE WHEN ... IS NULL.
    def build(args: Sequence[exp.Expr]) -> exp.Expr:
        return cls(this=args[0], expressions=list(args[1:]), ignore_nulls=False)

    return build


SCALAR_FUNCTION_BUILDERS: Mapping[str, FunctionBuilder] = MappingProxyType(
    {
        "GREATEST": _null_propagating(exp.Greatest),
        "LEAST": _null_propagating(exp.Least),
    }
)
"""Buildery funkcji, dla których ogólne ``exp.func(nazwa, ...)`` nie daje semantyki
niezależnej od silnika. Pozostałe funkcje rejestru (ROUND, COALESCE, ABS) mają w
``sqlglot`` typowane węzły o tej samej semantyce w obu dialektach."""


def aggregate(name: str, arg: exp.Expr) -> exp.Expr:
    """Węzeł agregacji o nazwie z rejestru (``SUM``, ``COUNT_DISTINCT``...)."""
    builder = AGGREGATE_BUILDERS.get(name.upper())
    if builder is None:
        raise UnsupportedFunctionError(f"no sqlglot builder for aggregation '{name}'")
    return builder(arg)


# --------------------------------------------------------------------------- #
# Fragmenty SQL z YAML
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=512)
def _parse_fragment_cached(fragment: str) -> exp.Expr:
    return sqlglot.parse_one(fragment, read=SOURCE_DIALECT)


def parse_fragment(fragment: str, owner: str) -> exp.Expr:
    """Parsuje fragment ``sql`` z YAML (dialekt BigQuery).

    Zwraca **kopię** – węzły ``sqlglot`` są mutowalne i znają rodzica, więc ten sam
    obiekt nie może trafić do drzewa w dwóch miejscach.
    """
    try:
        return _parse_fragment_cached(fragment).copy()
    except SqlglotError as exc:
        raise SqlFragmentError(owner, fragment, str(exc).splitlines()[0]) from exc


# --------------------------------------------------------------------------- #
# Literały i filtry
# --------------------------------------------------------------------------- #


def literal(value: FilterValue) -> exp.Expr:
    """Wartość filtra jako literał AST (nigdy konkatenacja napisów – ochrona przed SQL
    injection: generator dialektu sam escapuje apostrofy i ukośniki)."""
    if isinstance(value, dt.date):
        return exp.cast(exp.Literal.string(value.isoformat()), exp.DataType.build("DATE"))
    if isinstance(value, str):
        return exp.Literal.string(value)
    if isinstance(value, bool):  # pragma: no cover - wykluczone przez model żądania
        raise TypeError("boolean filter values are not supported")
    return exp.Literal.number(value)


_COMPARISONS: Mapping[FilterOperator, type[exp.Binary]] = MappingProxyType(
    {
        "==": exp.EQ,
        "!=": exp.NEQ,
        ">": exp.GT,
        ">=": exp.GTE,
        "<": exp.LT,
        "<=": exp.LTE,
    }
)


def condition(column: exp.Expr, planned: PlannedFilter) -> exp.Expr:
    """Warunek ``WHERE`` dla jednego filtra (operator API -> operator SQL)."""
    values = [literal(value) for value in planned.values]
    match planned.op:
        case "in":
            return exp.In(this=column, expressions=values)
        case "not_in":
            return exp.Not(this=exp.In(this=column, expressions=values))
        case _:
            return _COMPARISONS[planned.op](this=column, expression=values[0])


# --------------------------------------------------------------------------- #
# Visitor: AST domenowe -> sqlglot
# --------------------------------------------------------------------------- #

_ARITHMETIC: Mapping[str, type[exp.Binary]] = MappingProxyType(
    {"+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div}
)


def is_nonzero_constant(expr: Expr) -> bool:
    """Czy wyrażenie jest stałą różną od zera (wtedy dzielenie nie wymaga ``NULLIF``)."""
    match expr:
        case Literal(value=value):
            return value != 0
        case UnaryOp(operand=operand):
            return is_nonzero_constant(operand)
        case _:
            return False


class MetricExprBuilder(Visitor[exp.Expr]):
    """Tłumaczy rozwinięte AST metryki na wyrażenie ``sqlglot``.

    ``leaf`` zamienia nazwę miary bazowej na agregat właściwy dla typu źródła.
    """

    def __init__(self, leaf: Callable[[str], exp.Expr], dialect: str | None) -> None:
        self._leaf = leaf
        self._dialect = dialect

    def visit_measure_ref(self, node: MeasureRef) -> exp.Expr:
        return self._leaf(node.name)

    def visit_literal(self, node: Literal) -> exp.Expr:
        return exp.Literal.number(node.value)

    def visit_binary_op(self, node: BinaryOp) -> exp.Expr:
        left = self._operand(node.left)
        right = self._operand(node.right)
        if node.op == "/" and not is_nonzero_constant(node.right):
            right = exp.Nullif(this=right, expression=exp.Literal.number(0))
        return _ARITHMETIC[node.op](this=left, expression=right)

    def visit_unary_op(self, node: UnaryOp) -> exp.Expr:
        return exp.Neg(this=self._operand(node.operand))

    def visit_function_call(self, node: FunctionCall) -> exp.Expr:
        args = [self.visit(arg) for arg in node.args]
        spec = get_scalar_function(node.name)
        if spec is None:
            raise UnsupportedFunctionError(f"function '{node.name}' is not registered")
        override = spec.dialect_renderers.get(self._dialect) if self._dialect else None
        builder: FunctionBuilder | None = override or SCALAR_FUNCTION_BUILDERS.get(spec.name)
        if builder is not None:
            result = builder(args)
            if not isinstance(result, exp.Expr):
                raise UnsupportedFunctionError(
                    f"renderer of '{spec.name}' returned {type(result).__name__}, "
                    "expected sqlglot.exp.Expr"
                )
            return result
        return exp.func(spec.name, *args)

    def _operand(self, node: Expr) -> exp.Expr:
        rendered = self.visit(node)
        return exp.Paren(this=rendered) if isinstance(node, BinaryOp) else rendered


# --------------------------------------------------------------------------- #
# Renderer bazowy
# --------------------------------------------------------------------------- #


class SqlRenderer(ABC):
    """Plan logiczny -> ``exp.Select``; dialekt jest parametrem generowania tekstu."""

    @abstractmethod
    def source(self) -> exp.Expr:
        """Klauzula ``FROM`` (tabela lub funkcja tabelaryczna)."""

    @abstractmethod
    def dimension_expr(self, dimension: PlannedDimension) -> exp.Expr:
        """Wyrażenie wymiaru używane w ``WHERE`` i ``GROUP BY`` (zawsze nowy węzeł)."""

    @abstractmethod
    def measure_expr(self, name: str, measure: BaseMeasureConfig) -> exp.Expr:
        """Agregat miary bazowej – liść ``MeasureRef`` w metryce."""

    # -- API ------------------------------------------------------------------ #

    def render(self, plan: LogicalQuery, dialect: str, *, pretty: bool = False) -> str:
        """Tekst SQL planu w podanym dialekcie (``duckdb`` / ``bigquery``)."""
        return self.build(plan, dialect).sql(dialect=dialect, pretty=pretty)

    def build(self, plan: LogicalQuery, dialect: str | None = None) -> exp.Select:
        """Drzewo ``sqlglot`` planu.

        ``dialect`` jest potrzebny wyłącznie do wyboru nadpisań per dialekt z rejestru
        funkcji (``dialect_renderers``); bez nich drzewo jest niezależne od dialektu.
        """
        builder = MetricExprBuilder(lambda name: self._leaf(plan, name), dialect)
        projections: list[exp.Expr] = [self.select_dimension(d) for d in plan.dimensions]
        projections += [exp.alias_(builder.visit(m.expr), m.name) for m in plan.metrics]

        select = exp.Select(expressions=projections).from_(self.source(), copy=False)
        if plan.filters:
            conditions = [condition(self.dimension_expr(f.dimension), f) for f in plan.filters]
            select = select.where(exp.and_(*conditions), copy=False)
        if plan.dimensions:
            select = select.group_by(*(self.dimension_expr(d) for d in plan.dimensions), copy=False)
        if plan.order_by:
            select = select.order_by(*(self.order_expr(o) for o in plan.order_by), copy=False)
        if plan.limit is not None:
            select = select.limit(plan.limit, copy=False)
        return select

    # -- elementy wspólne ----------------------------------------------------- #

    def select_dimension(self, dimension: PlannedDimension) -> exp.Expr:
        """Wymiar w ``SELECT``: wyrażenie aliasowane nazwą wymiaru (bez aliasu, gdy jest
        to już kolumna o tej nazwie)."""
        expr = self.dimension_expr(dimension)
        if isinstance(expr, exp.Column) and not expr.table and expr.name == dimension.name:
            return expr
        return exp.alias_(expr, dimension.name)

    @staticmethod
    def order_expr(order: PlannedOrder) -> exp.Ordered:
        """Sortowanie po aliasie z ``SELECT``.

        ``NULLS LAST`` jest jawne: domyślne położenie NULL różni się między silnikami
        (BigQuery ``ASC`` -> NULLS FIRST, DuckDB -> NULLS LAST), a przy ``LIMIT`` od tego
        zależy, które wiersze trafią do wyniku. Generator pomija klauzulę tam, gdzie jest
        ona domyślna dla dialektu.
        """
        return exp.Ordered(
            this=exp.column(order.field), desc=order.direction == "DESC", nulls_first=False
        )

    def _leaf(self, plan: LogicalQuery, name: str) -> exp.Expr:
        measure = plan.base_measures.get(name)
        if measure is None:
            raise KeyError(f"base measure '{name}' is not part of the plan")
        return self.measure_expr(name, measure)
