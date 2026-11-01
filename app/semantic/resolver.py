"""Rozwijanie zależności metryk do miar bazowych (dok. 03, Moduł 1 pkt 4).

Metryki pochodne tworzą graf skierowany (metryka -> metryki/miary, do których się
odwołuje). Resolver:

1. sprawdza ``aggregation`` miar bazowych w rejestrze ``AGGREGATIONS`` (ADR-010),
2. parsuje formuły (biała lista, ``formula_parser``),
3. sprawdza bezpośrednie odwołania: nieznane nazwy (z podpowiedzią), wymiary, formuły
   bez żadnej miary,
4. przechodzi graf w głąb (DFS) z wykrywaniem cykli i podstawia AST metryk pochodnych,
   aż wszystkie liście ``MeasureRef`` wskazują miary bazowe,
5. wyznacza zbiór miar bazowych i addytywność każdej metryki (ADR-011: metryka jest
   addytywna, gdy wszystkie jej miary bazowe mają funkcję re-agregacji).

Wynikiem jest niemutowalny :class:`SemanticModel`, budowany raz per model. Miary bazowe
są w nim również „metrykami” (``expanded == MeasureRef(nazwa)``), dzięki czemu planer i
API pytają o nie jednolicie.
"""

from __future__ import annotations

import typing
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.config.schemas import ModelConfig
from app.semantic.ast_nodes import (
    BinaryOp,
    Expr,
    FunctionCall,
    Literal,
    MeasureRef,
    UnaryOp,
    Visitor,
    measure_refs,
    walk,
)
from app.semantic.errors import (
    CircularDependencyError,
    ConstantFormulaError,
    DimensionInFormulaError,
    SemanticError,
    UnknownAggregationError,
    UnknownMeasureError,
)
from app.semantic.formula_parser import parse_formula
from app.semantic.registry import AGGREGATIONS, FORBIDDEN_AGGREGATIONS, get_aggregation

MetricKind = typing.Literal["base", "derived"]


@dataclass(frozen=True, slots=True)
class ResolvedMetric:
    """Metryka po rozwinięciu.

    * ``formula`` – AST formuły przed rozwinięciem (dla miary bazowej ``MeasureRef`` na
      samą siebie),
    * ``expanded`` – AST, w którym wszystkie liście ``MeasureRef`` są miarami bazowymi,
    * ``base_measures`` – miary bazowe, od których metryka zależy,
    * ``additive`` – ``False``, gdy któraś z nich nie ma funkcji re-agregacji (Warunek 3).
    """

    name: str
    kind: MetricKind
    formula: Expr
    expanded: Expr
    base_measures: frozenset[str]
    additive: bool


@dataclass(frozen=True, slots=True)
class SemanticModel:
    """Model semantyczny: konfiguracja + rozwinięte metryki (najpierw miary bazowe,
    potem metryki pochodne – w kolejności z pliku YAML)."""

    config: ModelConfig
    metrics: Mapping[str, ResolvedMetric]

    @property
    def name(self) -> str:
        return self.config.model_name

    def get_metric(self, name: str) -> ResolvedMetric:
        """Metryka po nazwie.

        :raises UnknownMeasureError: nieznana nazwa (z podpowiedzią najbliższej).
        """
        metric = self.metrics.get(name)
        if metric is None:
            raise UnknownMeasureError(name, name, self.metrics)
        return metric


@dataclass(frozen=True, slots=True)
class Resolution:
    """Wynik analizy: model (``None`` przy błędach) i wszystkie wykryte błędy."""

    model: SemanticModel | None
    errors: tuple[SemanticError, ...]


# --------------------------------------------------------------------------- #
# Podstawianie AST
# --------------------------------------------------------------------------- #


class _Substituter(Visitor[Expr]):
    """Zastępuje ``MeasureRef`` o nazwach z ``replacements`` podanymi drzewami."""

    def __init__(self, replacements: Mapping[str, Expr]) -> None:
        self._replacements = replacements

    def visit_measure_ref(self, node: MeasureRef) -> Expr:
        return self._replacements.get(node.name, node)

    def visit_literal(self, node: Literal) -> Expr:
        return node

    def visit_binary_op(self, node: BinaryOp) -> Expr:
        return BinaryOp(node.op, self.visit(node.left), self.visit(node.right))

    def visit_unary_op(self, node: UnaryOp) -> Expr:
        return UnaryOp(node.op, self.visit(node.operand))

    def visit_function_call(self, node: FunctionCall) -> Expr:
        return FunctionCall(node.name, tuple(self.visit(arg) for arg in node.args))


def substitute(expr: Expr, replacements: Mapping[str, Expr]) -> Expr:
    """Kopia ``expr`` z podstawionymi drzewami w miejsce wskazanych ``MeasureRef``."""
    return _Substituter(replacements).visit(expr)


def _ordered_refs(expr: Expr) -> list[str]:
    """Nazwy ``MeasureRef`` w kolejności wystąpienia, bez powtórzeń (deterministyczne
    komunikaty i ścieżki cykli)."""
    return list(dict.fromkeys(node.name for node in walk(expr) if isinstance(node, MeasureRef)))


# --------------------------------------------------------------------------- #
# Analiza
# --------------------------------------------------------------------------- #


class _Resolver:
    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._base = config.base_measures
        self._derived = config.derived_metrics
        self._errors: list[SemanticError] = []
        self._parsed: dict[str, Expr] = {}
        self._failed: set[str] = set()
        self._expanded: dict[str, Expr] = {}
        self._visiting: set[str] = set()
        self._path: list[str] = []

    def run(self) -> Resolution:
        additive = self._check_aggregations()
        self._parse_formulas()
        self._check_references()
        for name in self._derived:
            self._expand(name)
        if self._errors:
            return Resolution(None, tuple(self._errors))
        return Resolution(self._build(additive), ())

    # -- krok 1: agregacje miar bazowych ------------------------------------ #

    def _check_aggregations(self) -> dict[str, bool]:
        additive: dict[str, bool] = {}
        for name, measure in self._base.items():
            spec = get_aggregation(measure.aggregation)
            if spec is None:
                hint = FORBIDDEN_AGGREGATIONS.get(measure.aggregation.upper())
                self._errors.append(
                    UnknownAggregationError(name, measure.aggregation, AGGREGATIONS, hint)
                )
            else:
                additive[name] = spec.additive
        return additive

    # -- krok 2: parsowanie formuł ------------------------------------------- #

    def _parse_formulas(self) -> None:
        for name, metric in self._derived.items():
            try:
                self._parsed[name] = parse_formula(metric.formula, name)
            except SemanticError as exc:
                self._errors.append(exc)
                self._failed.add(name)

    # -- krok 3: bezpośrednie odwołania -------------------------------------- #

    def _check_references(self) -> None:
        candidates = [*self._base, *self._derived]
        for name, expr in self._parsed.items():
            refs = _ordered_refs(expr)
            if not refs:
                self._fail(name, ConstantFormulaError(name, self._derived[name].formula))
            for ref in refs:
                if ref in self._base or ref in self._derived:
                    continue
                if ref in self._config.dimensions:
                    self._fail(name, DimensionInFormulaError(name, ref))
                else:
                    self._fail(name, UnknownMeasureError(name, ref, candidates))

    def _fail(self, name: str, error: SemanticError) -> None:
        self._errors.append(error)
        self._failed.add(name)

    # -- krok 4: rozwijanie (DFS z wykrywaniem cykli) ------------------------ #

    def _expand(self, name: str) -> Expr | None:
        """Rozwinięte AST metryki lub ``None``, gdy metryka (lub jej zależność) jest błędna."""
        if name in self._base:
            return MeasureRef(name)
        if name in self._failed:
            return None
        if name in self._expanded:
            return self._expanded[name]
        if name in self._visiting:
            # Każdy węzeł na ścieżce cyklu zostanie oznaczony jako błędny przy powrocie
            # z rekurencji, więc ten sam cykl nie może zostać wykryty ponownie.
            cycle = [*self._path[self._path.index(name) :], name]
            self._errors.append(CircularDependencyError(cycle))
            return None

        self._visiting.add(name)
        self._path.append(name)
        expr = self._parsed[name]
        children = {ref: self._expand(ref) for ref in _ordered_refs(expr)}
        self._path.pop()
        self._visiting.discard(name)

        resolved = {ref: child for ref, child in children.items() if child is not None}
        if len(resolved) != len(children):
            self._failed.add(name)  # błąd zgłoszony już przy zależności – nie powtarzamy
            return None
        expanded = substitute(expr, resolved)
        self._expanded[name] = expanded
        return expanded

    # -- krok 5: model wynikowy ---------------------------------------------- #

    def _build(self, additive: Mapping[str, bool]) -> SemanticModel:
        metrics: dict[str, ResolvedMetric] = {}
        for name in self._base:
            ref = MeasureRef(name)
            metrics[name] = ResolvedMetric(
                name, "base", ref, ref, frozenset({name}), additive[name]
            )
        for name in self._derived:
            expanded = self._expanded[name]
            bases = measure_refs(expanded)
            metrics[name] = ResolvedMetric(
                name,
                "derived",
                self._parsed[name],
                expanded,
                bases,
                all(additive[base] for base in bases),
            )
        return SemanticModel(self._config, MappingProxyType(metrics))


def analyze(config: ModelConfig) -> Resolution:
    """Pełna analiza modelu – zbiera **wszystkie** błędy (używane przez walidator)."""
    return _Resolver(config).run()


def build_semantic_model(config: ModelConfig) -> SemanticModel:
    """Buduje model semantyczny.

    :raises SemanticError: pierwszy wykryty błąd (pełną listę zwraca :func:`analyze`).
    """
    resolution = analyze(config)
    if resolution.model is None:
        raise resolution.errors[0]
    return resolution.model
