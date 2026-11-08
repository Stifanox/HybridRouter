"""End-to-end: wynik ``POST /api/v1/query`` (rollupy Parquet przez DuckDB) jest równy
wynikowi ``RawTableRenderer@duckdb`` na surowych danych syntetycznych (ADR-012).

W odróżnieniu od testów różnicowych A4 ścieżka rollupowa przechodzi tu przez całe API:
walidację HTTP, routing, migawkę indeksu, renderowanie, wykonawcę i serializację JSON.
"""

from __future__ import annotations

import math
from typing import Any

import duckdb
import pyarrow as pa
from fastapi.testclient import TestClient

from app.api.models import QueryRequest
from app.api.service import to_json_rows
from app.planner.builder import plan_query
from app.renderers.raw_table import RawTableRenderer
from app.semantic.resolver import SemanticModel
from tests.api.conftest import BodyFactory
from tests.conftest import RAW_TABLE

DIMENSIONS = ["pickup_date", "vendor_id", "payment_type"]


def _raw_rows(request: dict[str, Any], model: SemanticModel, raw: pa.Table) -> list[dict[str, Any]]:
    plan = plan_query(QueryRequest.model_validate(request), model)
    sql = RawTableRenderer(RAW_TABLE).render(plan, "duckdb")
    with duckdb.connect(":memory:") as connection:
        connection.register(RAW_TABLE, raw)
        rows = to_json_rows(connection.sql(sql).to_arrow_table())
    return [
        {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in r.items()} for r in rows
    ]


def _close(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return bool(a == b)
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def test_api_equals_raw_table(
    client: TestClient, body: BodyFactory, nyc_model: SemanticModel, synthetic_raw: pa.Table
) -> None:
    additive = [name for name, metric in nyc_model.metrics.items() if metric.additive]
    assert len(additive) == 13
    request = body(
        mode="force_duckdb",
        dimensions=DIMENSIONS,
        metrics=additive,
        filters=[
            {"field": "pickup_date", "op": ">=", "value": "2022-12-05"},
            {"field": "pickup_date", "op": "<=", "value": "2022-12-12"},
            {"field": "rate_code", "op": "not_in", "value": ["5"]},
        ],
        order_by=[{"field": d, "direction": "ASC"} for d in DIMENSIONS],
        limit=None,
    )
    response = client.post("/api/v1/query", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["telemetry"]["engine_used"] == "duckdb"
    api_rows = response.json()["data"]
    raw_rows = _raw_rows(request, nyc_model, synthetic_raw)

    assert len(api_rows) == len(raw_rows) > 50
    for api_row, raw_row in zip(api_rows, raw_rows, strict=True):
        assert list(api_row) == list(raw_row) == [*DIMENSIONS, *additive]
        mismatched = [k for k in api_row if not _close(api_row[k], raw_row[k])]
        assert not mismatched, (api_row, raw_row)
    # przypadki brzegowe rzeczywiście obecne w porównaniu
    assert any(row["tip_to_fare_ratio_pct"] is None for row in api_rows)
    assert any(row["avg_trip_speed_mph"] is None for row in api_rows)
