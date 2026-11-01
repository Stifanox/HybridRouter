"""Budowa planu logicznego: ``QueryRequest`` + model semantyczny -> ``LogicalQuery``.

Builder jest jedynym miejscem walidacji zapytania względem modelu semantycznego. Kolejno
sprawdza: model, limit, listę metryk, wymiary, metryki, filtry (pole, operator, wartości
z konwersją wg typu wymiaru), sortowanie; na końcu wyznacza zakres dat z filtrów na
``partition_key``. Pierwszy wykryty problem jest zgłaszany jako podklasa
:class:`~app.planner.errors.QueryValidationError`.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from app.api.models import Filter, QueryRequest
from app.api.models import FilterValue as RequestFilterValue
from app.config.schemas import BaseMeasureConfig, DimensionConfig
from app.planner.errors import (
    DuplicateFieldError,
    EmptyDateRangeError,
    EmptyMetricsError,
    FilterOnMetricError,
    InvalidFilterValueError,
    InvalidLimitError,
    OrderByFieldNotSelectedError,
    UnknownDimensionError,
    UnknownMetricError,
    UnknownModelError,
    UnsupportedOperatorError,
)
from app.planner.logical_plan import (
    FILTER_OPERATORS,
    LIST_OPERATORS,
    DateRange,
    FilterOperator,
    FilterValue,
    LogicalQuery,
    PlannedDimension,
    PlannedFilter,
    PlannedMetric,
    PlannedOrder,
)
from app.semantic.resolver import SemanticModel

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ONE_DAY = dt.timedelta(days=1)


def build_logical_query(request: QueryRequest, models: Mapping[str, SemanticModel]) -> LogicalQuery:
    """Wybiera model z rejestru po ``request.model`` i buduje plan.

    :raises UnknownModelError: brak modelu o tej nazwie (z podpowiedzią).
    :raises QueryValidationError: dowolny inny błąd zapytania (zob. :func:`plan_query`).
    """
    model = models.get(request.model)
    if model is None:
        raise UnknownModelError("model", request.model, models)
    return plan_query(request, model)


def plan_query(request: QueryRequest, model: SemanticModel) -> LogicalQuery:
    """Buduje plan dla wskazanego modelu (pole ``request.model`` nie jest sprawdzane).

    :raises QueryValidationError: zapytanie niezgodne z modelem semantycznym.
    """
    return _Planner(model).plan(request)


class _Planner:
    def __init__(self, model: SemanticModel) -> None:
        self._model = model
        self._config = model.config
        self._dimensions = model.config.dimensions

    def plan(self, request: QueryRequest) -> LogicalQuery:
        if request.limit is not None and request.limit <= 0:
            raise InvalidLimitError(request.limit)
        if not request.metrics:
            raise EmptyMetricsError()

        dimensions = tuple(
            self._dimension(f"dimensions[{i}]", name)
            for i, name in enumerate(_unique("dimensions", request.dimensions))
        )
        metrics = tuple(
            self._metric(f"metrics[{i}]", name)
            for i, name in enumerate(_unique("metrics", request.metrics))
        )
        filters = tuple(self._filter(f"filters[{i}]", f) for i, f in enumerate(request.filters))
        order_by = self._order_by(request, dimensions, metrics)

        used: dict[str, PlannedDimension] = {d.name: d for d in dimensions}
        for planned_filter in filters:
            used.setdefault(planned_filter.dimension.name, planned_filter.dimension)
        # order_by może wskazywać tylko pola z SELECT, więc nie dodaje nowych wymiarów.

        base_measures: dict[str, BaseMeasureConfig] = {}
        for metric in metrics:
            for base in sorted(metric.base_measures):
                base_measures[base] = self._config.base_measures[base]

        partition_key = self._config.storage.local_cache.partition_key
        return LogicalQuery(
            model_name=self._model.name,
            partition_key=partition_key,
            dimensions=dimensions,
            metrics=metrics,
            filters=filters,
            order_by=order_by,
            limit=request.limit,
            date_range=compute_date_range(filters, partition_key),
            base_measures=MappingProxyType(base_measures),
            used_dimensions=MappingProxyType(used),
        )

    # -- wymiary i metryki --------------------------------------------------- #

    def _dimension(self, field: str, name: str) -> PlannedDimension:
        definition = self._dimensions.get(name)
        if definition is None:
            note = f"'{name}' is a metric; use it in 'metrics'" if self._is_metric(name) else None
            raise UnknownDimensionError(field, name, self._dimensions, note)
        return PlannedDimension(name, definition)

    def _metric(self, field: str, name: str) -> PlannedMetric:
        resolved = self._model.metrics.get(name)
        if resolved is None:
            note = (
                f"'{name}' is a dimension; use it in 'dimensions'"
                if name in self._dimensions
                else None
            )
            raise UnknownMetricError(field, name, self._model.metrics, note)
        return PlannedMetric(
            name=resolved.name,
            expr=resolved.expanded,
            base_measures=resolved.base_measures,
            additive=resolved.additive,
        )

    def _is_metric(self, name: str) -> bool:
        return name in self._model.metrics

    # -- filtry -------------------------------------------------------------- #

    def _filter(self, field: str, request_filter: Filter) -> PlannedFilter:
        name = request_filter.field
        definition = self._dimensions.get(name)
        if definition is None:
            if self._is_metric(name):
                raise FilterOnMetricError(f"{field}.field", name)
            raise UnknownDimensionError(f"{field}.field", name, self._dimensions)

        op = _operator(f"{field}.op", request_filter.op)
        values = _filter_values(f"{field}.value", op, request_filter.value, definition)
        return PlannedFilter(PlannedDimension(name, definition), op, values)

    # -- sortowanie ---------------------------------------------------------- #

    def _order_by(
        self,
        request: QueryRequest,
        dimensions: Sequence[PlannedDimension],
        metrics: Sequence[PlannedMetric],
    ) -> tuple[PlannedOrder, ...]:
        selected = [d.name for d in dimensions] + [m.name for m in metrics]
        seen: set[str] = set()
        order: list[PlannedOrder] = []
        for i, item in enumerate(request.order_by):
            field = f"order_by[{i}].field"
            if item.field not in selected:
                raise OrderByFieldNotSelectedError(field, item.field, selected)
            if item.field in seen:
                raise DuplicateFieldError(field, item.field)
            seen.add(item.field)
            order.append(PlannedOrder(item.field, item.direction))
        return tuple(order)


def _unique(section: str, names: Iterable[str]) -> list[str]:
    result: list[str] = []
    for i, name in enumerate(names):
        if name in result:
            raise DuplicateFieldError(f"{section}[{i}]", name)
        result.append(name)
    return result


def _operator(field: str, op: str) -> FilterOperator:
    for candidate in FILTER_OPERATORS:
        if op == candidate:
            return candidate
    raise UnsupportedOperatorError(field, op, FILTER_OPERATORS)


# --------------------------------------------------------------------------- #
# Konwersja wartości filtrów
# --------------------------------------------------------------------------- #


def _filter_values(
    field: str, op: FilterOperator, raw: RequestFilterValue, dimension: DimensionConfig
) -> tuple[FilterValue, ...]:
    if op in LIST_OPERATORS:
        if not isinstance(raw, list):
            raise InvalidFilterValueError(field, f"operator '{op}' requires a list of values")
        if not raw:
            raise InvalidFilterValueError(field, f"operator '{op}' requires a non-empty list")
        return tuple(_convert(f"{field}[{i}]", item, dimension) for i, item in enumerate(raw))
    if isinstance(raw, list):
        raise InvalidFilterValueError(field, f"operator '{op}' requires a single value")
    return (_convert(field, raw, dimension),)


def _convert(field: str, raw: str | int | float, dimension: DimensionConfig) -> FilterValue:
    """Konwersja wartości JSON do typu wymiaru (``date``/``number``/``string``)."""
    match dimension.type:
        case "date":
            return _to_date(field, raw)
        case "number":
            return _to_number(field, raw)
        case "string":
            return _to_string(field, raw)


def _to_date(field: str, raw: str | int | float) -> dt.date:
    if isinstance(raw, str) and _ISO_DATE_RE.fullmatch(raw):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise InvalidFilterValueError(field, f"invalid date '{raw}': {exc}") from exc
    raise InvalidFilterValueError(field, f"expected a date 'YYYY-MM-DD', got {raw!r}")


def _to_number(field: str, raw: str | int | float) -> int | float:
    value: int | float
    if isinstance(raw, str):
        text = raw.strip()
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                raise InvalidFilterValueError(field, f"expected a number, got {raw!r}") from None
    else:
        value = raw
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidFilterValueError(field, f"expected a finite number, got {raw!r}")
    return value


def _to_string(field: str, raw: str | int | float) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int):
        return str(raw)
    # Dla float zapis tekstowy jest niejednoznaczny ("1" vs "1.0") – wymagamy napisu.
    raise InvalidFilterValueError(field, f"expected a string, got number {raw!r}")


# --------------------------------------------------------------------------- #
# Zakres dat (dok. 03, Moduł 2)
# --------------------------------------------------------------------------- #


def compute_date_range(filters: Iterable[PlannedFilter], partition_key: str) -> DateRange | None:
    """Domknięty przedział dni wyznaczony z filtrów na ``partition_key``.

    Granice z kilku filtrów są przecinane (``AND``). ``>`` / ``<`` przesuwają granicę o
    jeden dzień, ``==`` ogranicza z obu stron, ``in`` – do przedziału ``[min, max]`` listy
    (nadzbiór dni, bezpieczny dla sprawdzania pokrycia cache). ``!=`` i ``not_in`` nie
    zawężają zakresu.

    :returns: ``(start, end)`` albo ``None``, gdy brakuje dolnej lub górnej granicy.
    :raises EmptyDateRangeError: filtry wykluczają się wzajemnie.
    """
    lower: dt.date | None = None
    upper: dt.date | None = None

    def raise_lower(value: dt.date) -> None:
        nonlocal lower
        lower = value if lower is None else max(lower, value)

    def lower_upper(value: dt.date) -> None:
        nonlocal upper
        upper = value if upper is None else min(upper, value)

    for planned in filters:
        if planned.dimension.name != partition_key:
            continue
        dates = [v for v in planned.values if isinstance(v, dt.date)]
        if len(dates) != len(planned.values):  # pragma: no cover - gwarantuje konwersja
            raise TypeError(f"non-date value in filter on '{partition_key}'")
        try:
            match planned.op:
                case ">=":
                    raise_lower(dates[0])
                case ">":
                    raise_lower(dates[0] + _ONE_DAY)
                case "<=":
                    lower_upper(dates[0])
                case "<":
                    lower_upper(dates[0] - _ONE_DAY)
                case "==":
                    raise_lower(dates[0])
                    lower_upper(dates[0])
                case "in":
                    raise_lower(min(dates))
                    lower_upper(max(dates))
                case "!=" | "not_in":
                    pass
        except OverflowError:
            raise EmptyDateRangeError(
                "filters", f"filter '{partition_key} {planned.op} {dates[0]}' matches no day"
            ) from None

    if lower is not None and upper is not None and lower > upper:
        raise EmptyDateRangeError(
            "filters",
            f"filters on '{partition_key}' are contradictory: empty date range "
            f"[{lower.isoformat()}..{upper.isoformat()}]",
        )
    if lower is None or upper is None:
        return None
    return (lower, upper)
