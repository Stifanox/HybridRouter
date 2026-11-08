"""Modele Pydantic kontraktu API (dok. 03, sekcja 4).

* żądanie ``POST /api/v1/query`` (etap A3): :class:`QueryRequest`,
* odpowiedzi (etap A5): :class:`QueryResponse` (z opcjonalną :class:`Telemetry`),
  :class:`ErrorResponse`, :class:`SchemaResponse`, :class:`HealthResponse`.

Modele weryfikują jedynie *kształt* żądania (typy JSON, nieznane klucze). Walidacja
względem modelu semantycznego – istnienie wymiarów i metryk, operatory filtrów, zgodność
wartości z typem wymiaru, ``limit > 0`` – należy do ``app.planner.builder`` i zgłaszana
jest jednym typem błędu (``QueryValidationError``), mapowanym w A5 na HTTP 400.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr

ExecutionMode = Literal["auto", "force_duckdb", "force_bigquery"]
SortDirection = Literal["ASC", "DESC"]

FilterScalar = StrictStr | StrictInt | StrictFloat
"""Pojedyncza wartość filtra w JSON. Typy ścisłe: ``true`` nie staje się liczbą 1."""

FilterValue = FilterScalar | list[FilterScalar]
"""Wartość filtra: skalar (``==``, ``>``, ...) albo lista (``in``, ``not_in``)."""


class _RequestModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Filter(_RequestModel):
    """Warunek ``field op value`` na wymiarze (filtrowanie po metrykach nie jest wspierane).

    ``op`` jest napisem, a nie ``Literal`` – nieobsługiwany operator zgłasza builder
    czytelnym błędem z listą dozwolonych operatorów.
    """

    field: StrictStr
    op: StrictStr
    value: FilterValue


class OrderBy(_RequestModel):
    """Sortowanie po wymiarze lub metryce obecnej w zapytaniu."""

    field: StrictStr
    direction: SortDirection = "ASC"


class ExecutionControl(_RequestModel):
    """Flagi sterujące wykonaniem (dok. 02, sekcja 2)."""

    execution_mode: ExecutionMode = "auto"
    include_telemetry: bool = False
    confirm_high_cost: bool = False


class QueryRequest(_RequestModel):
    """Deklaratywne zapytanie analityczne (``POST /api/v1/query``)."""

    model: StrictStr
    dimensions: list[StrictStr] = Field(default_factory=list)
    metrics: list[StrictStr] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: StrictInt | None = None
    control: ExecutionControl = Field(default_factory=ExecutionControl)


# --------------------------------------------------------------------------- #
# Odpowiedzi (etap A5)
# --------------------------------------------------------------------------- #

Engine = Literal["duckdb", "bigquery"]


class _ResponseModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Telemetry(_ResponseModel):
    """Telemetria zapytania (dok. 02, sekcja 2; dok. 03, sekcja 4) – tylko przy
    ``control.include_telemetry = true``.

    * ``ast_compilation_time_ms`` – budowa planu logicznego (walidacja, rozwinięte AST
      metryk) + renderowanie SQL (AST ``sqlglot``); bez oceny routingu,
    * ``execution_time_ms`` – wykonanie w silniku z pobraniem pełnego wyniku,
    * ``total_time_ms`` – od przyjęcia zwalidowanego żądania do gotowych wierszy wyniku
      (bez serializacji JSON odpowiedzi),
    * ``bytes_scanned`` / ``estimated_cost_usd`` – koszt BigQuery; dla DuckDB ``0``.
    """

    engine_used: Engine
    routing_mode: ExecutionMode
    routing_reason: str
    ast_compilation_time_ms: float
    execution_time_ms: float
    total_time_ms: float
    bytes_scanned: int
    estimated_cost_usd: float
    compiled_sql: str


class QueryResponse(_ResponseModel):
    """Odpowiedź sukcesu; klucz ``telemetry`` występuje wyłącznie, gdy telemetrię zebrano."""

    status: Literal["success"] = "success"
    data: list[dict[str, Any]]
    telemetry: Telemetry | None = Field(default=None, exclude_if=lambda value: value is None)


class ErrorResponse(_ResponseModel):
    """Jednolita odpowiedź błędu (4xx/5xx).

    ``error_type`` – nazwa klasy błędu domenowego (np. ``UnknownMetricError``,
    ``CacheMissInForcedModeError``, ``BigQueryNotAvailableYet``), ``details`` – dane
    strukturalne (pole żądania, brakujące dni, powód routingu, ...).
    """

    status: Literal["error"] = "error"
    error_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DimensionInfo(_ResponseModel):
    name: str
    type: str
    in_rollup: bool


class BaseMeasureInfo(_ResponseModel):
    name: str
    aggregation: str
    additive: bool


class DerivedMetricInfo(_ResponseModel):
    name: str
    formula: str
    description: str | None
    additive: bool
    base_measures: list[str]
    """Miary bazowe po rozwinięciu formuły (posortowane)."""


class ModelInfo(_ResponseModel):
    name: str
    description: str | None
    partition_key: str
    dimensions: list[DimensionInfo]
    base_measures: list[BaseMeasureInfo]
    derived_metrics: list[DerivedMetricInfo]


class SchemaResponse(_ResponseModel):
    """``GET /api/v1/schema`` – katalog modeli semantycznych."""

    models: list[ModelInfo]


class CacheHealth(_ResponseModel):
    status: Literal["ok", "degraded"]
    partitions: int
    """Liczba dni z partycją w aktualnej migawce indeksu."""
    manifest_version: int
    first_day: str | None
    last_day: str | None
    error: str | None = Field(default=None, exclude_if=lambda value: value is None)


class HealthResponse(_ResponseModel):
    """``GET /health`` – stan serwisu i lokalnego cache każdego modelu."""

    status: Literal["ok", "degraded"]
    models: dict[str, CacheHealth]
