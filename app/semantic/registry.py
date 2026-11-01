"""Dwupoziomowy model funkcji (ADR-010, ADR-011).

* :data:`AGGREGATIONS` – funkcje agregujące, dopuszczalne **wyłącznie** w
  ``base_measures.aggregation``. ``rollup_reaggregate`` to funkcja, którą wartość
  częściową z dziennego rollupu łączy się w wynik dla dłuższego okresu
  (``COUNT -> SUM``); ``None`` oznacza miarę holistyczną, której nie da się
  odtworzyć z rollupu (``COUNT_DISTINCT``).
* :data:`SCALAR_FUNCTIONS` – funkcje skalarne dopuszczalne w formułach metryk
  pochodnych (operują na wartościach już zagregowanych).

Rozszerzanie: nowa funkcja to nowy wpis w krotce ``_AGGREGATION_SPECS`` lub
``_SCALAR_FUNCTION_SPECS`` – parser formuł i walidator korzystają z rejestrów i nie
wymagają zmian. Renderowanie zależne od dialektu (etap A3) podłącza się przez pole
``dialect_renderers``; moduł nie zależy od ``sqlglot``.

Klucze rejestrów są kanoniczne (wielkie litery); wyszukiwanie przez
:func:`get_aggregation` / :func:`get_scalar_function` nie rozróżnia wielkości liter.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

DialectRenderer = Callable[..., Any]
"""PUNKT ROZSZERZENIA (A3): funkcja budująca wyrażenie SQL danego dialektu z już
wyrenderowanych argumentów. Typ celowo ogólny – warstwa semantyczna nie zna ``sqlglot``."""


@dataclass(frozen=True, slots=True)
class AggregationSpec:
    """Opis funkcji agregującej miary bazowej."""

    name: str
    rollup_reaggregate: str | None
    description: str
    dialect_renderers: Mapping[str, DialectRenderer] = field(
        default_factory=lambda: MappingProxyType({}), compare=False, hash=False
    )

    @property
    def additive(self) -> bool:
        """Czy wynik da się odtworzyć z dziennego rollupu (Warunek 3 routera)."""
        return self.rollup_reaggregate is not None


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    """Opis funkcji skalarnej dopuszczalnej w formułach; ``max_args=None`` – bez limitu."""

    name: str
    min_args: int
    max_args: int | None
    description: str
    dialect_renderers: Mapping[str, DialectRenderer] = field(
        default_factory=lambda: MappingProxyType({}), compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if self.min_args < 0 or (self.max_args is not None and self.max_args < self.min_args):
            raise ValueError(f"invalid arity for {self.name}: {self.min_args}..{self.max_args}")

    def accepts(self, arg_count: int) -> bool:
        """Czy funkcja przyjmuje ``arg_count`` argumentów."""
        return arg_count >= self.min_args and (self.max_args is None or arg_count <= self.max_args)

    @property
    def arity(self) -> str:
        """Opis liczby argumentów do komunikatów błędów, np. ``1..2``, ``2 or more``."""
        if self.max_args is None:
            return f"{self.min_args} or more"
        if self.max_args == self.min_args:
            return str(self.min_args)
        return f"{self.min_args}..{self.max_args}"


# --------------------------------------------------------------------------- #
# Zawartość rejestrów
# --------------------------------------------------------------------------- #

_AGGREGATION_SPECS: tuple[AggregationSpec, ...] = (
    AggregationSpec("SUM", "SUM", "Suma; rozdzielna – sumy dzienne sumują się."),
    AggregationSpec("COUNT", "SUM", "Liczność; rozdzielna – liczności dzienne się sumują."),
    AggregationSpec("MIN", "MIN", "Minimum; rozdzielna – minimum z minimów dziennych."),
    AggregationSpec("MAX", "MAX", "Maksimum; rozdzielna – maksimum z maksimów dziennych."),
    AggregationSpec(
        "COUNT_DISTINCT",
        None,
        "Liczba unikalnych wartości; holistyczna – nie materializowana w rollupie (ADR-011).",
    ),
)

_SCALAR_FUNCTION_SPECS: tuple[FunctionSpec, ...] = (
    FunctionSpec("ROUND", 1, 2, "Zaokrąglenie: ROUND(x) lub ROUND(x, miejsca_po_przecinku)."),
    FunctionSpec("COALESCE", 2, None, "Pierwszy argument różny od NULL."),
    FunctionSpec("ABS", 1, 1, "Wartość bezwzględna."),
    FunctionSpec("GREATEST", 2, None, "Największy z argumentów."),
    FunctionSpec("LEAST", 2, None, "Najmniejszy z argumentów."),
)

FORBIDDEN_AGGREGATIONS: Mapping[str, str] = MappingProxyType(
    {"AVG": "define it as a derived metric SUM/COUNT (e.g. total_x / count_x)"}
)
"""Agregacje świadomie odrzucane, z podpowiedzią dla autora konfiguracji."""

AGGREGATE_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "SUM",
        "COUNT",
        "MIN",
        "MAX",
        "COUNT_DISTINCT",
        "AVG",
        "MEDIAN",
        "STDDEV",
        "VARIANCE",
        "COUNTIF",
        "ANY_VALUE",
        "APPROX_COUNT_DISTINCT",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
    }
)
"""Nazwy funkcji agregujących rozpoznawane w formułach wyłącznie po to, by zgłosić
czytelny błąd „agregacje definiuj w base_measures” zamiast „nieznana funkcja”."""


class _Named(Protocol):
    @property
    def name(self) -> str: ...


_SpecT = TypeVar("_SpecT", bound=_Named)


def _index(specs: Iterable[_SpecT]) -> Mapping[str, _SpecT]:
    index: dict[str, _SpecT] = {}
    for spec in specs:
        key = spec.name.upper()
        if key in index:
            raise ValueError(f"duplicate registry entry {key!r}")
        index[key] = spec
    return MappingProxyType(index)


AGGREGATIONS: Mapping[str, AggregationSpec] = _index(_AGGREGATION_SPECS)
SCALAR_FUNCTIONS: Mapping[str, FunctionSpec] = _index(_SCALAR_FUNCTION_SPECS)


# --------------------------------------------------------------------------- #
# Wyszukiwanie
# --------------------------------------------------------------------------- #


def get_aggregation(name: str) -> AggregationSpec | None:
    """Specyfikacja agregacji (bez rozróżniania wielkości liter) lub ``None``."""
    return AGGREGATIONS.get(name.upper())


def get_scalar_function(name: str) -> FunctionSpec | None:
    """Specyfikacja funkcji skalarnej (bez rozróżniania wielkości liter) lub ``None``."""
    return SCALAR_FUNCTIONS.get(name.upper())


def is_additive(aggregation: str) -> bool:
    """Czy agregacja ma funkcję re-agregacji na rollupie.

    :raises KeyError: agregacja spoza rejestru.
    """
    spec = get_aggregation(aggregation)
    if spec is None:
        raise KeyError(f"unknown aggregation {aggregation!r}")
    return spec.additive
