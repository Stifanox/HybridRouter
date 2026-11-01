"""Błędy warstwy semantycznej.

Wszystkie dziedziczą po :class:`SemanticError` i wskazują element konfiguracji, którego
dotyczą (``metric`` – nazwa metryki pochodnej lub miary bazowej). Walidator
(``app.semantic.validator``) tłumaczy je na ``ConfigIssue`` ze ścieżką pola.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence


class SemanticError(Exception):
    """Bazowy błąd modelu semantycznego."""

    def __init__(self, metric: str, message: str) -> None:
        super().__init__(message)
        self.metric = metric
        self.message = message


class FormulaSyntaxError(SemanticError):
    """Formuła spoza dopuszczalnej gramatyki (biała lista ADR-009).

    ``position`` to 0-bazowy indeks znaku w formule (``None`` – cała formuła),
    ``fragment`` – fragment źródła odpowiadający odrzuconemu węzłowi.
    """

    def __init__(
        self,
        metric: str,
        formula: str,
        reason: str,
        position: int | None = None,
        fragment: str | None = None,
    ) -> None:
        self.formula = formula
        self.reason = reason
        self.position = position
        self.fragment = fragment
        where = f" at column {position + 1}" if position is not None else ""
        what = f" in '{fragment}'" if fragment and fragment != formula else ""
        message = f"{reason}{what}{where} of formula '{formula}'"
        if position is not None:
            # Wskaźnik pod formułą; znaki nowej linii zastąpione spacją, by kolumny się zgadzały.
            pointer = " " * position + "^"
            flat = formula.replace("\n", " ").replace("\r", " ")
            message += f"\n      {flat}\n      {pointer}"
        super().__init__(metric, message)


class UnknownMeasureError(SemanticError):
    """Odwołanie do nazwy, która nie jest miarą bazową ani metryką pochodną."""

    def __init__(self, metric: str, name: str, candidates: Iterable[str]) -> None:
        self.name = name
        self.suggestion = suggest(name, candidates)
        hint = f"; did you mean '{self.suggestion}'?" if self.suggestion else ""
        super().__init__(metric, f"unknown measure or metric '{name}'{hint}")


class DimensionInFormulaError(SemanticError):
    """Formuła odwołuje się do wymiaru – formuły operują wyłącznie na wartościach miar."""

    def __init__(self, metric: str, name: str) -> None:
        self.name = name
        super().__init__(
            metric,
            f"'{name}' is a dimension; formulas may reference only base measures "
            "and derived metrics",
        )


class CircularDependencyError(SemanticError):
    """Cykl w zależnościach metryk pochodnych; ``cycle`` – ścieżka, np. ``(a, b, a)``."""

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = tuple(cycle)
        super().__init__(self.cycle[0], f"circular dependency: {' -> '.join(self.cycle)}")


class ConstantFormulaError(SemanticError):
    """Formuła nie odwołuje się do żadnej miary (np. ``"100"``)."""

    def __init__(self, metric: str, formula: str) -> None:
        super().__init__(metric, f"formula '{formula}' must reference at least one measure")


class UnknownAggregationError(SemanticError):
    """``aggregation`` miary bazowej spoza rejestru ``AGGREGATIONS`` (lub zabroniona, jak AVG)."""

    def __init__(
        self, metric: str, aggregation: str, candidates: Iterable[str], hint: str | None = None
    ) -> None:
        self.aggregation = aggregation
        if hint is not None:
            message = f"aggregation '{aggregation}' is not supported; {hint}"
        else:
            allowed = sorted(candidates)
            suggestion = suggest(aggregation, allowed)
            message = f"unknown aggregation '{aggregation}' (allowed: {', '.join(allowed)})"
            if suggestion:
                message += f"; did you mean '{suggestion}'?"
        super().__init__(metric, message)


def suggest(name: str, candidates: Iterable[str]) -> str | None:
    """Najbliższa nazwa z ``candidates``: najpierw dopasowanie bez wielkości liter, potem
    ``difflib`` (próg podobieństwa 0.6)."""
    pool = list(candidates)
    folded = name.casefold()
    for candidate in pool:
        if candidate.casefold() == folded:
            return candidate
    by_folded = {candidate.casefold(): candidate for candidate in pool}
    matches = difflib.get_close_matches(folded, list(by_folded), n=1)
    return by_folded[matches[0]] if matches else None
