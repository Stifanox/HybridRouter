# Hybrid OLAP Router

Hybrydowy router zapytań analitycznych z warstwą semantyczną (praca magisterska).
Deklaratywne zapytania JSON są kompilowane (formuła → AST domenowe → plan logiczny → AST
`sqlglot` → SQL) i wykonywane na lokalnym cache dziennych rollupów Parquet (DuckDB) albo –
od fazy 2 – w BigQuery.

**Stan: faza 1.** Działa lokalny cache DuckDB. Zapytania, które router kieruje do BigQuery,
zwracają `HTTP 501` (`BigQueryNotAvailableYet`) z powodem decyzji.

Specyfikacja: `docs/specyfikacja/`, notatki implementacyjne: `docs/gemini/notatki-implementacyjne/`.

## Instalacja

Wymagane: Python ≥ 3.11 i Poetry.

```bash
poetry config --local virtualenvs.in-project true
poetry install
```

## Zasianie cache danymi syntetycznymi

```bash
poetry run python scripts/seed_synthetic_cache.py
```

Skrypt generuje deterministyczne syntetyczne przejazdy z okresu 2022-12-01…2022-12-31, buduje
dzienne rollupy i publikuje je w `./data/nyc_taxi` (`manifest.json` +
`pickup_date=YYYY-MM-DD/rollup-<wersja>.parquet`). Opcje: `--start-date`, `--end-date`,
`--rows-per-day`, `--empty-day` (dzień bez danych), `--data-root`; listę wyświetla `--help`.

## Uruchomienie

```bash
poetry run uvicorn app.main:app
```

Zmienne środowiskowe (opcjonalne):

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `HYBRID_ROUTER_CONFIG_DIR` | `configs` | katalog z plikami `*.yaml` modeli |
| `HYBRID_ROUTER_DATA_ROOT` | – | katalog nadrzędny cache; cache modelu: `{DATA_ROOT}/{nazwa katalogu z base_path}` |
| `HYBRID_ROUTER_INDEX_CHECK_INTERVAL` | `0` | co ile sekund sprawdzać `manifest.json` (`0` – przy każdym zapytaniu) |
| `HYBRID_ROUTER_DUCKDB_THREADS` | – | limit wątków DuckDB |

Dokumentacja OpenAPI: <http://localhost:8000/docs>.

## Przykładowe zapytanie

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "nyc_yellow_taxi_analytics",
    "dimensions": ["pickup_date", "vendor_id"],
    "metrics": ["trips_count", "total_revenue", "tip_to_fare_ratio_pct", "avg_trip_speed_mph"],
    "filters": [
      {"field": "pickup_date", "op": ">=", "value": "2022-12-01"},
      {"field": "pickup_date", "op": "<=", "value": "2022-12-23"},
      {"field": "payment_type", "op": "==", "value": "1"}
    ],
    "order_by": [{"field": "pickup_date", "direction": "ASC"}],
    "limit": 1000,
    "control": {"execution_mode": "auto", "include_telemetry": true, "confirm_high_cost": false}
  }'
```

Pozostałe endpointy: `GET /api/v1/schema` (katalog modeli, wymiarów i metryk) oraz
`GET /health` (stan cache: liczba partycji, wersja manifestu).

## Testy i kontrola jakości

```bash
poetry run pytest                 # w tym testy różnicowe i testy API
poetry run pytest --cov           # z pomiarem pokrycia
poetry run ruff check . && poetry run ruff format --check .
poetry run mypy app tests scripts
```
