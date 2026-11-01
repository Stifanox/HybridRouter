"""Plan logiczny zapytania – niezależny od silnika i dialektu SQL (ADR-008).

:class:`LogicalQuery` powstaje w ``app.planner.builder`` z żądania API i modelu
semantycznego. Zawiera wszystko, czego potrzebują:

* **router** (Warunki 1–3, dok. 03 Moduł 2): ``used_dimensions`` z definicjami wymiarów
  (``in_rollup``), ``date_range`` wyliczony z filtrów na ``partition_key``,
  ``used_base_measures`` z addytywnością,
* **renderery** (``app.renderers``): wymiary, metryki z rozwiniętym AST domenowym,
  definicje użytych miar bazowych (wyrażenie ``sql`` i agregacja), filtry z wartościami już
  skonwertowanymi do typu wymiaru, sortowanie i limit.

Plan nie zawiera żadnego tekstu SQL ani węzłów ``sqlglot`` – o postaci liści (surowa tabela
vs rollup) i dialekcie decyduje renderer.

Wszystkie klasy są niemutowalne (``frozen``); kolekcje to krotki, ``frozenset`` lub
``MappingProxyType``.
"""

from __future__ import annotations

import datetime as dt
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

from app.config.schemas import BaseMeasureConfig, DimensionConfig
from app.semantic.ast_nodes import Expr

FilterOperator: TypeAlias = typing.Literal["==", "!=", ">", ">=", "<", "<=", "in", "not_in"]
"""Operatory filtrów przyjmowane przez API (dok. 03, Moduł 1 „Operatory filtrów”)."""

FILTER_OPERATORS: tuple[FilterOperator, ...] = typing.get_args(FilterOperator)
LIST_OPERATORS: frozenset[FilterOperator] = frozenset({"in", "not_in"})
"""Operatory, których wartością jest niepusta lista."""

FilterValue: TypeAlias = str | int | float | dt.date
"""Wartość filtra po konwersji wg typu wymiaru (``date`` -> ``datetime.date`` itd.)."""

SortDirection: TypeAlias = typing.Literal["ASC", "DESC"]
DateRange: TypeAlias = tuple[dt.date, dt.date]


@dataclass(frozen=True, slots=True)
class PlannedDimension:
    """Wymiar użyty w zapytaniu wraz z definicją z YAML."""

    name: str
    definition: DimensionConfig

    @property
    def in_rollup(self) -> bool:
        return self.definition.in_rollup


@dataclass(frozen=True, slots=True)
class PlannedMetric:
    """Metryka w ``SELECT``: nazwa (alias), AST rozwinięte do miar bazowych, addytywność."""

    name: str
    expr: Expr
    base_measures: frozenset[str]
    additive: bool


@dataclass(frozen=True, slots=True)
class PlannedFilter:
    """Warunek na wymiarze. Dla operatorów skalarnych ``values`` ma dokładnie jeden element,
    dla ``in`` / ``not_in`` – co najmniej jeden."""

    dimension: PlannedDimension
    op: FilterOperator
    values: tuple[FilterValue, ...]

    @property
    def value(self) -> FilterValue:
        """Wartość operatora skalarnego."""
        if self.op in LIST_OPERATORS:
            raise TypeError(f"operator '{self.op}' has a list of values")
        return self.values[0]


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    """Sortowanie po polu wynikowym (alias wymiaru lub metryki z ``SELECT``)."""

    field: str
    direction: SortDirection


@dataclass(frozen=True, slots=True)
class LogicalQuery:
    """Plan logiczny zapytania.

    * ``dimensions`` / ``metrics`` – w kolejności z żądania (kolejność kolumn wyniku),
    * ``base_measures`` – definicje miar bazowych, do których rozwijają się metryki
      (renderer zamienia na nie liście ``MeasureRef``),
    * ``date_range`` – domknięty przedział dni ``[start, end]`` wyznaczony z filtrów na
      ``partition_key``; ``None``, gdy zakres nie jest ograniczony z obu stron,
    * ``used_dimensions`` – wymiary z ``dimensions``, ``filters`` i ``order_by``
      (Warunek 1 routera).
    """

    model_name: str
    partition_key: str
    dimensions: tuple[PlannedDimension, ...]
    metrics: tuple[PlannedMetric, ...]
    filters: tuple[PlannedFilter, ...]
    order_by: tuple[PlannedOrder, ...]
    limit: int | None
    date_range: DateRange | None
    base_measures: Mapping[str, BaseMeasureConfig]
    used_dimensions: Mapping[str, PlannedDimension]

    @property
    def used_base_measures(self) -> frozenset[str]:
        """Miary bazowe użyte po rozwinięciu wszystkich metryk (Warunek 3 routera)."""
        return frozenset(self.base_measures)

    @property
    def additive(self) -> bool:
        """Czy wszystkie metryki da się policzyć z dziennego rollupu."""
        return all(metric.additive for metric in self.metrics)
