"""``GET /api/v1/schema`` i ``GET /health``; konwersja wyniku do typów JSON."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
from fastapi.testclient import TestClient

from app.api.service import to_json_rows
from app.config.settings import Settings
from app.main import create_app
from tests.api.conftest import CONFIG_DIR
from tests.conftest import MODEL_NAME


def test_schema(client: TestClient) -> None:
    response = client.get("/api/v1/schema")
    assert response.status_code == 200
    (model,) = response.json()["models"]
    assert model["name"] == MODEL_NAME
    assert model["partition_key"] == "pickup_date"
    assert model["description"].startswith("Model analityczny")

    dimensions = {d["name"]: d for d in model["dimensions"]}
    assert list(dimensions) == [
        "pickup_date",
        "vendor_id",
        "payment_type",
        "rate_code",
        "dropoff_location_id",
    ]
    assert dimensions["pickup_date"] == {"name": "pickup_date", "type": "date", "in_rollup": True}
    assert dimensions["dropoff_location_id"]["in_rollup"] is False

    measures = {m["name"]: m for m in model["base_measures"]}
    assert len(measures) == 8
    assert measures["trips_count"] == {
        "name": "trips_count",
        "aggregation": "COUNT",
        "additive": True,
    }
    assert measures["unique_pickup_zones"] == {
        "name": "unique_pickup_zones",
        "aggregation": "COUNT_DISTINCT",
        "additive": False,
    }

    metrics = {m["name"]: m for m in model["derived_metrics"]}
    assert list(metrics) == [
        "tip_to_fare_ratio_pct",
        "non_fare_surcharges",
        "surcharge_to_revenue_roi_pct",
        "avg_revenue_per_mile",
        "avg_trip_speed_mph",
        "avg_fare_per_trip",
    ]
    assert metrics["surcharge_to_revenue_roi_pct"] == {
        "name": "surcharge_to_revenue_roi_pct",
        "formula": "(non_fare_surcharges / total_revenue) * 100",
        "description": "Udział opłat dodatkowych w całkowitym przychodzie (%)",
        "additive": True,
        "base_measures": ["total_fare_amount", "total_revenue", "total_tip_amount"],
    }


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "models": {
            MODEL_NAME: {
                "status": "ok",
                "partitions": 11,  # 2022-12-01…12 bez dnia-dziury 2022-12-04
                "manifest_version": 1,
                "first_day": "2022-12-01",
                "last_day": "2022-12-12",
            }
        },
    }


def test_health_empty_cache(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(config_dir=CONFIG_DIR, data_root=tmp_path))) as client:
        cache = client.get("/health").json()["models"][MODEL_NAME]
    assert cache == {
        "status": "ok",
        "partitions": 0,
        "manifest_version": 0,
        "first_day": None,
        "last_day": None,
    }


def test_openapi_documents_endpoints(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert {"/api/v1/query", "/api/v1/schema", "/health"} <= set(spec["paths"])
    responses = spec["paths"]["/api/v1/query"]["post"]["responses"]
    assert {"200", "400", "500", "501"} <= set(responses)


def test_to_json_rows_normalizes_decimals() -> None:
    table = pa.table(
        {
            "day": pa.array([dt.date(2022, 12, 1)]),
            "count": pa.array([Decimal(42)], pa.decimal128(38, 0)),
            "money": pa.array([Decimal("12.345000000")], pa.decimal128(38, 9)),
            "missing": pa.array([None], pa.decimal128(38, 0)),
        }
    )
    (row,) = to_json_rows(table)
    assert row == {"day": dt.date(2022, 12, 1), "count": 42, "money": 12.345, "missing": None}
    assert type(row["count"]) is int
    assert type(row["money"]) is float
