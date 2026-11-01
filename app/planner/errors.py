"""Błędy walidacji zapytania (plan logiczny).

Wszystkie dziedziczą po :class:`QueryValidationError` – w A5 mapowanym na HTTP 400 z
``error_type`` równym nazwie klasy. Builder zgłasza **pierwszy** wykryty błąd (zapytanie
jest krótkie, a precyzyjny ``error_type`` jest ważniejszy niż lista wszystkich problemów).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.semantic.errors import suggest


class QueryValidationError(Exception):
    """Bazowy błąd walidacji zapytania.

    ``field`` wskazuje element żądania (np. ``filters[2].op``), ``message`` – opis.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}" if field else message)
        self.field = field
        self.message = message

    @property
    def error_type(self) -> str:
        return type(self).__name__


def _hint(name: str, candidates: Iterable[str]) -> str:
    suggestion = suggest(name, candidates)
    return f"; did you mean '{suggestion}'?" if suggestion else ""


class UnknownModelError(QueryValidationError):
    def __init__(self, field: str, name: str, candidates: Iterable[str]) -> None:
        self.name = name
        super().__init__(field, f"unknown model '{name}'{_hint(name, candidates)}")


class UnknownDimensionError(QueryValidationError):
    def __init__(
        self, field: str, name: str, candidates: Iterable[str], note: str | None = None
    ) -> None:
        self.name = name
        detail = f"; {note}" if note else _hint(name, candidates)
        super().__init__(field, f"unknown dimension '{name}'{detail}")


class UnknownMetricError(QueryValidationError):
    def __init__(
        self, field: str, name: str, candidates: Iterable[str], note: str | None = None
    ) -> None:
        self.name = name
        detail = f"; {note}" if note else _hint(name, candidates)
        super().__init__(field, f"unknown metric '{name}'{detail}")


class FilterOnMetricError(QueryValidationError):
    def __init__(self, field: str, name: str) -> None:
        self.name = name
        super().__init__(
            field,
            f"'{name}' is a metric; filters may reference only dimensions "
            "(filtering on aggregated values / HAVING is not supported)",
        )


class UnsupportedOperatorError(QueryValidationError):
    def __init__(self, field: str, op: str, allowed: Iterable[str]) -> None:
        self.op = op
        allowed_list = ", ".join(f"'{a}'" for a in allowed)
        super().__init__(field, f"unsupported operator '{op}' (allowed: {allowed_list})")


class InvalidFilterValueError(QueryValidationError):
    """Wartość niezgodna z operatorem (skalar vs lista) lub z typem wymiaru."""


class EmptyDateRangeError(QueryValidationError):
    """Filtry na ``partition_key`` wykluczają się (np. ``>= 12-10`` i ``<= 12-01``)."""


class OrderByFieldNotSelectedError(QueryValidationError):
    def __init__(self, field: str, name: str, selected: Iterable[str]) -> None:
        self.name = name
        super().__init__(
            field,
            f"cannot order by '{name}': field is not in dimensions or metrics of the query"
            f"{_hint(name, selected)}",
        )


class DuplicateFieldError(QueryValidationError):
    def __init__(self, field: str, name: str) -> None:
        self.name = name
        super().__init__(field, f"'{name}' is listed more than once")


class EmptyMetricsError(QueryValidationError):
    def __init__(self) -> None:
        super().__init__("metrics", "at least one metric is required")


class InvalidLimitError(QueryValidationError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__("limit", f"limit must be a positive integer, got {limit}")
