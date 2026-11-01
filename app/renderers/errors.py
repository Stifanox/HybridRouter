"""Błędy rendererów SQL.

Są to błędy **programistyczne** lub konfiguracyjne, a nie błędy zapytania użytkownika:
plan, który do renderera dotarł, przeszedł walidację buildera, a wybór renderera należy
do routera (Warunki 1–3). Jeśli np. ``RollupRenderer`` dostaje miarę holistyczną, to
zawiódł router – stąd osobna hierarchia, niemapowana na HTTP 400.
"""

from __future__ import annotations


class RendererError(Exception):
    """Bazowy błąd renderowania planu do SQL."""


class NonAdditiveMeasureError(RendererError):
    """Miara bez funkcji re-agregacji (np. ``COUNT_DISTINCT``) w ``RollupRenderer``."""

    def __init__(self, measure: str, aggregation: str) -> None:
        self.measure = measure
        self.aggregation = aggregation
        super().__init__(
            f"base measure '{measure}' ({aggregation}) has no rollup re-aggregation and "
            "cannot be computed from the local rollup (router Condition 3 violated)"
        )


class DimensionNotInRollupError(RendererError):
    """Wymiar z ``in_rollup: false`` w ``RollupRenderer``."""

    def __init__(self, dimension: str) -> None:
        self.dimension = dimension
        super().__init__(
            f"dimension '{dimension}' is not present in the local rollup "
            "(router Condition 1 violated)"
        )


class SqlFragmentError(RendererError):
    """Fragment ``sql`` z YAML nie daje się sparsować w dialekcie BigQuery."""

    def __init__(self, owner: str, fragment: str, reason: str) -> None:
        self.owner = owner
        self.fragment = fragment
        super().__init__(f"cannot parse SQL fragment of '{owner}': {fragment!r} ({reason})")


class UnsupportedFunctionError(RendererError):
    """Agregacja lub funkcja z rejestru bez sposobu renderowania do ``sqlglot``."""
