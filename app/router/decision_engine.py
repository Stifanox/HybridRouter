"""Silnik decyzyjny routera – kwalifikacja zapytania do lokalnego cache (dok. 03, Moduł 2).

* :func:`check_local_eligibility` – ocena Warunków 1–3 (etap A4),
* :func:`route_query` – wybór silnika wg ``execution_mode`` (etap A5). Decyzja
  ``bigquery`` jest w fazie 1 wyłącznie *wynikiem* routingu: API odpowiada na nią HTTP 501,
  bo wykonawca BigQuery (i Cost Guard) dochodzi w fazie 2.

* **Warunek 1 (ziarnistość):** każdy wymiar użyty w ``dimensions``, ``filters`` i
  ``order_by`` (``LogicalQuery.used_dimensions``) ma ``in_rollup: true``.
* **Warunek 2 (pokrycie czasowe):** zakres dat (``LogicalQuery.date_range``) jest ograniczony
  z obu stron i każdy jego dzień ma plik w migawce indeksu (:class:`PartitionSnapshot`).
* **Warunek 3 (addytywność, ADR-011):** każda miara bazowa użyta po rozwinięciu metryk ma
  funkcję re-agregacji w rejestrze ``AGGREGATIONS``.

Wszystkie trzy warunki są oceniane zawsze (bez skracania), więc wynik niesie pełną
diagnostykę – do telemetrii (``routing_reason``) i komunikatów błędów trybu ``force_duckdb``.

Zapytanie używa **jednej** migawki do oceny Warunku 2 i do listy plików::

    snapshot = partition_index.snapshot()
    eligibility = check_local_eligibility(plan, model, snapshot)
    if eligibility.eligible:
        sql = RollupRenderer(snapshot.files_for(plan.date_range)).render(plan, "duckdb")
"""

from __future__ import annotations

import datetime as dt
import typing
from dataclasses import dataclass

from app.api.models import Engine, ExecutionMode
from app.planner.logical_plan import DateRange, LogicalQuery
from app.router.exceptions import (
    CacheMissInForcedModeError,
    GranularityNotSupportedInLocalCacheError,
    NonAdditiveMeasureInLocalCacheError,
    RoutingError,
)
from app.semantic.registry import get_aggregation
from app.semantic.resolver import SemanticModel
from app.sync.partition_index import PartitionSnapshot

ConditionName = typing.Literal["granularity", "time_coverage", "additivity"]

MAX_REPORTED_MISSING_DAYS = 100
"""Górna granica liczby brakujących dni w wyniku (pełną liczbę niesie
``missing_day_count``) – zapytanie o zakres wieloletni nie generuje ogromnych list."""
_MAX_DAYS_IN_MESSAGE = 10


@dataclass(frozen=True, slots=True)
class ConditionCheck:
    """Ocena jednego warunku: spełniony / niespełniony wraz z powodami (po angielsku –
    trafiają do odpowiedzi API i telemetrii)."""

    condition: ConditionName
    satisfied: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str | None:
        return "; ".join(self.reasons) if self.reasons else None


@dataclass(frozen=True, slots=True)
class LocalEligibility:
    """Wynik kwalifikacji zapytania do lokalnego cache."""

    granularity: ConditionCheck
    time_coverage: ConditionCheck
    additivity: ConditionCheck
    unsupported_dimensions: tuple[str, ...]
    date_range: DateRange | None
    missing_days: tuple[dt.date, ...]
    """Brakujące dni rosnąco – co najwyżej :data:`MAX_REPORTED_MISSING_DAYS`."""
    missing_day_count: int
    non_additive_measures: tuple[str, ...]

    @property
    def conditions(self) -> tuple[ConditionCheck, ConditionCheck, ConditionCheck]:
        return (self.granularity, self.time_coverage, self.additivity)

    @property
    def eligible(self) -> bool:
        return all(check.satisfied for check in self.conditions)

    @property
    def reason(self) -> str | None:
        """Powód pierwszego niespełnionego warunku (kolejność 1, 2, 3) albo ``None``."""
        return next((c.reason for c in self.conditions if not c.satisfied), None)

    def to_details(self) -> dict[str, typing.Any]:
        """Pełna diagnostyka Warunków 1–3 jako słownik JSON (``details`` odpowiedzi API)."""
        return {
            "conditions": {
                c.condition: {"satisfied": c.satisfied, "reasons": list(c.reasons)}
                for c in self.conditions
            },
            "unsupported_dimensions": list(self.unsupported_dimensions),
            "date_range": None
            if self.date_range is None
            else [self.date_range[0].isoformat(), self.date_range[1].isoformat()],
            "missing_days": [day.isoformat() for day in self.missing_days],
            "missing_day_count": self.missing_day_count,
            "non_additive_measures": list(self.non_additive_measures),
        }

    def raise_if_ineligible(self) -> None:
        """Tryb ``force_duckdb``: wyjątek dla pierwszego niespełnionego warunku.

        :raises GranularityNotSupportedInLocalCacheError: Warunek 1.
        :raises CacheMissInForcedModeError: Warunek 2.
        :raises NonAdditiveMeasureInLocalCacheError: Warunek 3.
        """
        error = self.to_error()
        if error is not None:
            raise error

    def to_error(self) -> RoutingError | None:
        """Wyjątek odpowiadający pierwszemu niespełnionemu warunkowi (bez zgłaszania)."""
        if not self.granularity.satisfied:
            return GranularityNotSupportedInLocalCacheError(
                self.unsupported_dimensions, self.granularity.reason or ""
            )
        if not self.time_coverage.satisfied:
            return CacheMissInForcedModeError(
                self.date_range,
                self.missing_days,
                self.missing_day_count,
                self.time_coverage.reason or "",
            )
        if not self.additivity.satisfied:
            return NonAdditiveMeasureInLocalCacheError(
                self.non_additive_measures, self.additivity.reason or ""
            )
        return None


def check_local_eligibility(
    plan: LogicalQuery, model: SemanticModel, snapshot: PartitionSnapshot
) -> LocalEligibility:
    """Ocena Warunków 1–3 dla planu logicznego na migawce indeksu partycji.

    :raises ValueError: plan, model i migawka nie dotyczą tego samego modelu / klucza
        partycji (błąd programistyczny).
    """
    partition_key = model.config.storage.local_cache.partition_key
    if plan.model_name != model.name:
        raise ValueError(f"plan of model '{plan.model_name}' checked against '{model.name}'")
    if snapshot.partition_key != partition_key or plan.partition_key != partition_key:
        raise ValueError(
            f"partition key mismatch: model '{partition_key}', plan '{plan.partition_key}', "
            f"snapshot '{snapshot.partition_key}'"
        )

    unsupported = tuple(name for name, d in plan.used_dimensions.items() if not d.in_rollup)
    granularity = ConditionCheck(
        "granularity",
        not unsupported,
        tuple(f"Dimension '{name}' not present in local rollup" for name in unsupported),
    )

    time_coverage, missing, missing_count = _check_time_coverage(
        plan.date_range, partition_key, snapshot
    )

    non_additive = tuple(
        name
        for name, measure in plan.base_measures.items()
        if not _reaggregatable(measure.aggregation)
    )
    additivity = ConditionCheck(
        "additivity",
        not non_additive,
        tuple(_non_additive_reason(plan, name) for name in non_additive),
    )

    return LocalEligibility(
        granularity=granularity,
        time_coverage=time_coverage,
        additivity=additivity,
        unsupported_dimensions=unsupported,
        date_range=plan.date_range,
        missing_days=tuple(missing),
        missing_day_count=missing_count,
        non_additive_measures=non_additive,
    )


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Wybrany silnik wraz z powodem (``routing_reason`` w telemetrii).

    ``eligibility`` – ocena Warunków 1–3; ``None`` w trybie ``force_bigquery`` (cache nie
    jest sprawdzany).
    """

    engine: Engine
    mode: ExecutionMode
    reason: str
    eligibility: LocalEligibility | None


def route_query(
    plan: LogicalQuery, model: SemanticModel, mode: ExecutionMode, snapshot: PartitionSnapshot
) -> RoutingDecision:
    """Wybór silnika (dok. 03, Moduł 2 – algorytm wyboru).

    * ``force_bigquery`` -> ``bigquery`` bez sprawdzania cache,
    * ``force_duckdb`` -> ``duckdb`` albo wyjątek pierwszego niespełnionego warunku,
    * ``auto`` -> ``duckdb``, gdy Warunki 1–3 są spełnione; inaczej Full Fallback
      (ADR-004) do ``bigquery`` z powodem w ``reason``.

    :raises GranularityNotSupportedInLocalCacheError: ``force_duckdb``, Warunek 1.
    :raises CacheMissInForcedModeError: ``force_duckdb``, Warunek 2.
    :raises NonAdditiveMeasureInLocalCacheError: ``force_duckdb``, Warunek 3.
    """
    if mode == "force_bigquery":
        return RoutingDecision("bigquery", mode, "Forced by execution_mode 'force_bigquery'.", None)
    eligibility = check_local_eligibility(plan, model, snapshot)
    if mode == "force_duckdb":
        eligibility.raise_if_ineligible()
        reason = f"Forced by execution_mode 'force_duckdb'; {_local_reason(eligibility)}"
        return RoutingDecision("duckdb", mode, reason, eligibility)
    if eligibility.eligible:
        return RoutingDecision("duckdb", mode, _local_reason(eligibility), eligibility)
    reason = f"Full fallback to BigQuery: {eligibility.reason}."
    return RoutingDecision("bigquery", mode, reason, eligibility)


def _local_reason(eligibility: LocalEligibility) -> str:
    if eligibility.date_range is None:  # Warunek 2 spełniony => zakres ograniczony
        raise ValueError("local routing requires a bounded date range")
    start, end = eligibility.date_range
    return (
        f"All dimensions in rollup and date range [{start.isoformat()}..{end.isoformat()}] "
        "available in local Parquet cache."
    )


def _check_time_coverage(
    date_range: DateRange | None, partition_key: str, index: PartitionSnapshot
) -> tuple[ConditionCheck, list[dt.date], int]:
    if date_range is None:
        reason = f"Query date range is not bounded on both sides by filters on '{partition_key}'"
        return ConditionCheck("time_coverage", False, (reason,)), [], 0
    start, end = date_range
    count = index.count_missing(start, end)
    if count == 0:
        return ConditionCheck("time_coverage", True), [], 0
    missing = index.missing_days(start, end, limit=MAX_REPORTED_MISSING_DAYS)
    shown = ", ".join(day.isoformat() for day in missing[:_MAX_DAYS_IN_MESSAGE])
    more = f" and {count - _MAX_DAYS_IN_MESSAGE} more" if count > _MAX_DAYS_IN_MESSAGE else ""
    reason = (
        f"Local cache is missing {count} day(s) of {start.isoformat()}..{end.isoformat()}: "
        f"{shown}{more}"
    )
    return ConditionCheck("time_coverage", False, (reason,)), missing, count


def _reaggregatable(aggregation: str) -> bool:
    spec = get_aggregation(aggregation)
    return spec is not None and spec.additive


def _non_additive_reason(plan: LogicalQuery, measure: str) -> str:
    aggregation = plan.base_measures[measure].aggregation
    metrics = ", ".join(f"'{m.name}'" for m in plan.metrics if measure in m.base_measures)
    return (
        f"Measure '{measure}' ({aggregation}) cannot be re-aggregated from local rollup "
        f"(used by metric {metrics})"
    )
