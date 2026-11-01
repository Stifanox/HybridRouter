"""Modele Pydantic kontraktu API (dok. 03, sekcja 4).

Etap A3 definiuje wyłącznie modele **żądania** (``POST /api/v1/query``); modele odpowiedzi
(sukces, błąd, telemetria) dochodzą w etapie A5.

Modele weryfikują jedynie *kształt* żądania (typy JSON, nieznane klucze). Walidacja
względem modelu semantycznego – istnienie wymiarów i metryk, operatory filtrów, zgodność
wartości z typem wymiaru, ``limit > 0`` – należy do ``app.planner.builder`` i zgłaszana
jest jednym typem błędu (``QueryValidationError``), mapowanym w A5 na HTTP 400.
"""

from __future__ import annotations

from typing import Literal

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
