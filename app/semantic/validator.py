"""Walidacja semantyczna modelu – adapter resolvera na interfejs walidatorów loadera.

Funkcja :func:`validate_semantics` ma sygnaturę ``ModelValidator`` z ``app.config.loader``
(``ModelConfig -> Iterable[ConfigIssue]``) i jest zarejestrowana w
``SEMANTIC_VALIDATORS``. Sprawdza:

* ``aggregation`` każdej miary bazowej istnieje w ``AGGREGATIONS``; ``AVG`` jest
  odrzucane z podpowiedzią (metryka pochodna SUM/COUNT),
* każda formuła parsuje się według białej listy i rozwija do miar bazowych
  (brak cykli, nieznanych nazw i odwołań do wymiarów).

Addytywność metryk (``ResolvedMetric.additive``) wyznacza resolver; walidator jedynie
raportuje błędy. Moduł nie importuje loadera (unikanie cyklu importów).
"""

from __future__ import annotations

from collections.abc import Iterator

from app.config.errors import ConfigIssue
from app.config.schemas import ModelConfig
from app.semantic.errors import SemanticError, UnknownAggregationError
from app.semantic.resolver import analyze


def issue_path(error: SemanticError) -> str:
    """Ścieżka pola konfiguracji, której dotyczy błąd."""
    if isinstance(error, UnknownAggregationError):
        return f"base_measures.{error.metric}.aggregation"
    return f"derived_metrics.{error.metric}.formula"


def validate_semantics(model: ModelConfig) -> Iterator[ConfigIssue]:
    """Wszystkie błędy semantyczne modelu jako ``ConfigIssue``."""
    for error in analyze(model).errors:
        yield ConfigIssue(issue_path(error), error.message)
