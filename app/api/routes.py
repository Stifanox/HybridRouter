"""Endpointy HTTP (dok. 03, sekcja 4): ``/api/v1/query``, ``/api/v1/schema``, ``/health``.

Endpointy są zwykłymi funkcjami ``def`` (nie ``async def``): FastAPI wykonuje je w puli
wątków (anyio), więc synchroniczne wywołania DuckDB, odczyt ``manifest.json`` i
renderowanie SQL nie blokują pętli zdarzeń. ``DuckDBExecutor`` tworzy osobny kursor na
każde wywołanie, a ``QueryService`` jest niemutowalny – równoległe żądania są bezpieczne.
Błędy są mapowane centralnie w ``app.api.errors``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.models import (
    ErrorResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    SchemaResponse,
)
from app.api.service import QueryService

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid query or forced routing not possible"},
    500: {"model": ErrorResponse, "description": "Internal error"},
    501: {"model": ErrorResponse, "description": "Routed to BigQuery (available in phase 2)"},
}


def get_service(request: Request) -> QueryService:
    service: QueryService = request.app.state.service
    return service


Service = Annotated[QueryService, Depends(get_service)]

router = APIRouter()


@router.post("/api/v1/query", response_model=QueryResponse, responses=_ERRORS)
def query(body: QueryRequest, service: Service) -> QueryResponse:
    """Wykonuje deklaratywne zapytanie analityczne."""
    return service.execute(body)


@router.get("/api/v1/schema", response_model=SchemaResponse)
def schema(service: Service) -> SchemaResponse:
    """Katalog modeli: wymiary, miary bazowe i metryki pochodne."""
    return service.schema()


@router.get("/health", response_model=HealthResponse)
def health(service: Service) -> HealthResponse:
    """Stan serwisu i liczba dostępnych partycji lokalnego cache każdego modelu."""
    return service.health()
