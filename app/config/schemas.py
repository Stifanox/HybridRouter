"""Modele Pydantic v2 opisujące plik konfiguracyjny warstwy semantycznej (dok. 03, sekcja 2).

Modele realizują walidację *pojedynczych pól* (typy, zbiory dopuszczalnych wartości,
dodatniość limitów, niepuste napisy). Reguły obejmujące kilka pól lub sekcji
(unikalność nazw, ``partition_key``, okno dat) sprawdza ``app.config.loader``.
Semantyka formuł i rejestr agregacji należą do warstwy ``app.semantic`` (etap A2).

Wszystkie modele są niemutowalne (``frozen``) i odrzucają nieznane klucze
(``extra="forbid"``), dzięki czemu literówka w YAML jest błędem, a nie cichym pominięciem.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""Napis niepusty po usunięciu białych znaków z brzegów."""

AggregationName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, to_upper=True)
]
"""Nazwa funkcji agregującej, normalizowana do wielkich liter (``sum`` -> ``SUM``).

Zgodność z rejestrem ``AGGREGATIONS`` sprawdza walidacja semantyczna (A2).
"""

DimensionType = Literal["date", "string", "number"]
PartitionGranularity = Literal["HOUR", "DAY", "MONTH", "YEAR"]
ActionOnExceed = Literal["raise_error", "require_confirmation", "silent_allow"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
class BigQueryStorageConfig(_FrozenModel):
    """Źródło surowych danych w BigQuery (``storage.bigquery``)."""

    project_id: NonEmptyStr
    dataset_table: NonEmptyStr
    timestamp_column: NonEmptyStr
    partition_granularity: PartitionGranularity
    use_query_cache: StrictBool = False


class LocalCacheConfig(_FrozenModel):
    """Lokalny cache rollupów Parquet (``storage.local_cache``)."""

    base_path: NonEmptyStr
    file_format: Literal["parquet"]
    partition_scheme: Literal["hive"]
    partition_key: NonEmptyStr
    date_window_start: dt.date
    date_window_end: dt.date


class StorageConfig(_FrozenModel):
    bigquery: BigQueryStorageConfig
    local_cache: LocalCacheConfig


# --------------------------------------------------------------------------- #
# cost_guard
# --------------------------------------------------------------------------- #
class CostGuardConfig(_FrozenModel):
    """Limity strażnika kosztów BigQuery (ADR-006, dok. 03 Moduł 3)."""

    enabled: StrictBool
    price_per_tb_usd: float = Field(gt=0)
    max_bytes_per_query: int = Field(gt=0)
    max_cost_usd_per_query: float = Field(gt=0)
    action_on_exceed: ActionOnExceed


# --------------------------------------------------------------------------- #
# dimensions / base_measures / derived_metrics
# --------------------------------------------------------------------------- #
class DimensionConfig(_FrozenModel):
    """Wymiar: wyrażenie SQL (dialekt BigQuery), typ i obecność w rollupie."""

    sql: NonEmptyStr
    type: DimensionType
    in_rollup: StrictBool


class BaseMeasureConfig(_FrozenModel):
    """Miara bazowa: funkcja agregująca zastosowana do wyrażenia SQL (dialekt BigQuery)."""

    aggregation: AggregationName
    sql: NonEmptyStr


class DerivedMetricConfig(_FrozenModel):
    """Metryka pochodna: formuła nad miarami bazowymi i innymi metrykami pochodnymi."""

    formula: NonEmptyStr
    description: str | None = None


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
class ModelConfig(_FrozenModel):
    """Kompletna konfiguracja jednego modelu semantycznego (jeden plik YAML)."""

    model_name: NonEmptyStr
    description: str | None = None
    storage: StorageConfig
    cost_guard: CostGuardConfig
    dimensions: dict[str, DimensionConfig] = Field(min_length=1)
    base_measures: dict[str, BaseMeasureConfig] = Field(min_length=1)
    derived_metrics: dict[str, DerivedMetricConfig] = Field(default_factory=dict)
