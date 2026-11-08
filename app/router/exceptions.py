"""Wyjątki routera (dok. 03, Moduł 2).

Zgłaszane w trybie ``force_duckdb``, gdy zapytanie nie kwalifikuje się do lokalnego cache
(Warunki 1–3). Są to błędy **zapytania** (A5: HTTP 400 z ``error_type`` = nazwa klasy i
``details``), w odróżnieniu od ``app.renderers.errors`` – błędów programistycznych.
W trybie ``auto`` te same warunki prowadzą do fallbacku do BigQuery (faza 2), a powód trafia
do telemetrii.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any


class RoutingError(Exception):
    """Bazowy błąd routingu; ``details`` – dane do odpowiedzi JSON."""

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    @property
    def error_type(self) -> str:
        return type(self).__name__


class GranularityNotSupportedInLocalCacheError(RoutingError):
    """Warunek 1: zapytanie używa wymiaru nieobecnego w rollupie (``in_rollup: false``)."""

    def __init__(self, dimensions: Sequence[str], message: str) -> None:
        self.dimensions = tuple(dimensions)
        super().__init__(message, {"dimensions": list(self.dimensions)})


class CacheMissInForcedModeError(RoutingError):
    """Warunek 2: zakres dat nieograniczony lub bez kompletu partycji w lokalnym cache."""

    def __init__(
        self,
        date_range: tuple[dt.date, dt.date] | None,
        missing_days: Sequence[dt.date],
        missing_day_count: int,
        message: str,
    ) -> None:
        self.date_range = date_range
        self.missing_days = tuple(missing_days)
        self.missing_day_count = missing_day_count
        super().__init__(
            message,
            {
                "date_range": None
                if date_range is None
                else [date_range[0].isoformat(), date_range[1].isoformat()],
                "missing_days": [day.isoformat() for day in self.missing_days],
                "missing_day_count": missing_day_count,
            },
        )


class NonAdditiveMeasureInLocalCacheError(RoutingError):
    """Warunek 3: miara bazowa bez funkcji re-agregacji (miara holistyczna, ADR-011)."""

    def __init__(self, measures: Sequence[str], message: str) -> None:
        self.measures = tuple(measures)
        super().__init__(message, {"measures": list(self.measures)})
