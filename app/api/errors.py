"""Centralne mapowanie wyjątków na jednolity JSON błędu (:class:`ErrorResponse`).

| Wyjątek | HTTP | ``error_type`` |
|---|---|---|
| ``RequestValidationError`` (kształt JSON żądania, Pydantic) | 400 | ``RequestValidationError`` |
| ``QueryValidationError`` i podklasy (builder planu) | 400 | nazwa klasy |
| ``RoutingError`` i podklasy (``force_duckdb``, Warunki 1–3) | 400 | nazwa klasy |
| :class:`BigQueryNotAvailableYet` (routing do ``bigquery``) | 501 | jw. (nazwa klasy) |
| ``HTTPException`` (np. nieznana ścieżka, zła metoda) | jej kod | np. ``NotFound`` |
| ``ConfigValidationError`` | 500 | ``ConfigValidationError`` |
| ``QueryExecutionError``, ``RendererError`` (błędy programistyczne / silnika) | 500 | nazwa klasy |
| każdy inny wyjątek | 500 | ``InternalServerError`` |

Odpowiedzi 500 nie zawierają śladu stosu ani tekstu SQL – pełna informacja trafia do logu.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.models import ErrorResponse
from app.config.errors import ConfigValidationError
from app.executors.base import QueryExecutionError
from app.planner.errors import QueryValidationError
from app.renderers.errors import RendererError
from app.router.decision_engine import RoutingDecision
from app.router.exceptions import RoutingError

logger = logging.getLogger(__name__)


class BigQueryNotAvailableYet(Exception):
    """Router wybrał BigQuery, a wykonawca BigQuery dochodzi w fazie 2 (HTTP 501).

    Punkt rozszerzenia fazy 2: w miejscu zgłoszenia tego wyjątku (``QueryService``)
    zapytanie trafi do ``BigQueryExecutor`` (renderer ``RawTableRenderer@bigquery``,
    dry-run i Cost Guard).
    """

    def __init__(self, decision: RoutingDecision) -> None:
        self.decision = decision
        self.message = (
            "Query was routed to BigQuery, which is not available in phase 1 "
            "(local DuckDB cache only)."
        )
        super().__init__(self.message)

    @property
    def details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "engine": self.decision.engine,
            "routing_mode": self.decision.mode,
            "routing_reason": self.decision.reason,
        }
        if self.decision.eligibility is not None:
            details.update(self.decision.eligibility.to_details())
        return details


def error_response(
    status_code: int, error_type: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = ErrorResponse(error_type=error_type, message=message, details=details or {})
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def _request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    first = errors[0] if errors else None
    message = (
        f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}" if first else "invalid request"
    )
    return error_response(400, "RequestValidationError", message, {"errors": errors})


async def _query_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, QueryValidationError)
    return error_response(400, exc.error_type, exc.message, {"field": exc.field})


async def _routing(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RoutingError)
    return error_response(400, exc.error_type, exc.message, exc.details)


async def _bigquery_not_available(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BigQueryNotAvailableYet)
    return error_response(501, "BigQueryNotAvailableYet", exc.message, exc.details)


async def _http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "HTTP Error"
    error_type = "".join(word.capitalize() for word in phrase.replace("-", " ").split())
    response = error_response(exc.status_code, error_type, str(exc.detail))
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _config(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ConfigValidationError)
    logger.error("configuration error: %s", exc)
    return error_response(
        500,
        "ConfigValidationError",
        "Semantic model configuration is invalid",
        {"source": exc.source, "issues": [str(issue) for issue in exc.issues]},
    )


async def _internal(request: Request, exc: Exception) -> JSONResponse:
    """Błędy silnika i programistyczne – bez SQL i śladu stosu w odpowiedzi."""
    logger.exception("unhandled error in %s %s", request.method, request.url.path)
    if isinstance(exc, QueryExecutionError):
        return error_response(
            500, "QueryExecutionError", f"Query execution failed in engine '{exc.engine}'"
        )
    if isinstance(exc, RendererError):
        return error_response(500, type(exc).__name__, "SQL compilation failed")
    return error_response(500, "InternalServerError", "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(QueryValidationError, _query_validation)
    app.add_exception_handler(RoutingError, _routing)
    app.add_exception_handler(BigQueryNotAvailableYet, _bigquery_not_available)
    app.add_exception_handler(StarletteHTTPException, _http)
    app.add_exception_handler(ConfigValidationError, _config)
    app.add_exception_handler(QueryExecutionError, _internal)
    app.add_exception_handler(RendererError, _internal)
    app.add_exception_handler(Exception, _internal)
