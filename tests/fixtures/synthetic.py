"""Deterministyczny generator surowych przejazdów NYC Yellow Taxi (ADR-012).

Dane mają nazwy kolumn tabeli BigQuery ``tlc_yellow_trips_2022`` i służą testom
różnicowym: ścieżka „surowa” (``RawTableRenderer@duckdb`` na tej tabeli) vs ścieżka
„rollupowa” (``RollupRenderer@duckdb`` na dziennych rollupach Parquet zbudowanych z tych
samych danych).

Własności (każda sprawdzana w ``tests/test_synthetic.py``):

* **determinizm** – ten sam ``seed`` i parametry dają identyczną tabelę
  (``random.Random(seed)``, bez zależności od czasu i globalnego stanu),
* **znaczniki czasu z dokładnością do pełnych sekund**, typ ``TIMESTAMP`` bez strefy
  (``timestamp[us]``) – ``TIMESTAMP_DIFF`` w BigQuery obcina różnicę, a ``DATE_DIFF`` w
  DuckDB liczy przekroczone granice sekund; dla pełnych sekund obie funkcje są zgodne
  (notatka A3, „Otwarte kwestie”),
* **przypadki brzegowe** (deterministycznie obecne każdego dnia z danymi):

  - ``payment_type = 3`` („no charge”) ma ``fare_amount = 0`` i ``tip_amount = 0`` – suma
    ``fare_amount`` w tej grupie jest zerem (``NULLIF`` w ``tip_to_fare_ratio_pct``),
  - ``payment_type = 4`` („dispute”) ma zerowy czas i dystans przejazdu (``NULLIF`` w
    ``avg_trip_speed_mph``); pojedyncze przejazdy o zerowym czasie występują też w innych
    grupach,
  - pierwszy przejazd dnia rozpoczyna się po 23:55 i kończy następnego dnia (przejazd
    przez północ – należy do dnia odbioru),
  - wartości ``NULL`` w miarach (``fare_amount``, ``tip_amount``, ``total_amount``,
    ``trip_distance``, ``passenger_count``, ``dropoff_datetime``) i w wymiarze
    ``rate_code``,
  - dni z ``empty_day_offsets`` nie mają żadnych przejazdów (dziura w zakresie dat),

* **zmienność między dniami** – mnożnik opłat i stopa napiwków losowane per dzień, ujemnie
  skorelowane; dzięki temu średnia z dziennych wskaźników wyraźnie różni się od wskaźnika
  policzonego dla całego zakresu (kontrprzykład „średniej ze średnich”, dok. 02).

Typy kolumn: identyfikatory kategorii jako ``INT64`` (rzutowane w YAML przez
``CAST(... AS STRING)``, co sprawdza transpilację fragmentów ``sql``), kwoty i dystans jako
``FLOAT64`` (domyślnie – wariant trudniejszy dla porównań: suma zmiennoprzecinkowa zależy od
kolejności dodawania) albo ``NUMERIC`` = ``decimal128(38, 9)`` (``money_as_decimal=True``).
Faktyczne typy tabeli publicznej należy potwierdzić w fazie 2 (schemat BigQuery).
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Collection
from decimal import Decimal
from typing import Any

import pyarrow as pa

DEFAULT_SEED = 20221201
DEFAULT_START = dt.date(2022, 12, 1)
DEFAULT_EMPTY_DAY_OFFSETS: tuple[int, ...] = (3,)

TIMESTAMP_TYPE = pa.timestamp("us")
"""``TIMESTAMP`` bez strefy czasowej (BigQuery ``TIMESTAMP`` ma dokładność mikrosekund)."""

NUMERIC_TYPE = pa.decimal128(38, 9)
"""Odpowiednik ``NUMERIC`` z BigQuery."""

MONEY_COLUMNS: tuple[str, ...] = ("trip_distance", "fare_amount", "tip_amount", "total_amount")

RAW_SCHEMA = pa.schema(
    [
        pa.field("vendor_id", pa.int64()),
        pa.field("pickup_datetime", TIMESTAMP_TYPE),
        pa.field("dropoff_datetime", TIMESTAMP_TYPE),
        pa.field("passenger_count", pa.int64()),
        pa.field("trip_distance", pa.float64()),
        pa.field("rate_code", pa.int64()),
        pa.field("payment_type", pa.int64()),
        pa.field("fare_amount", pa.float64()),
        pa.field("tip_amount", pa.float64()),
        pa.field("total_amount", pa.float64()),
        pa.field("pickup_location_id", pa.int64()),
        pa.field("dropoff_location_id", pa.int64()),
    ]
)
"""Schemat surowej tabeli (nazwy kolumn jak w BigQuery)."""

PAYMENT_NO_CHARGE = 3
"""Grupa z zerową sumą ``fare_amount``."""
PAYMENT_DISPUTE = 4
"""Grupa z zerowym czasem przejazdu."""

_NULL_PROBABILITY: dict[str, float] = {
    "fare_amount": 0.02,
    "tip_amount": 0.02,
    "total_amount": 0.01,
    "trip_distance": 0.01,
    "passenger_count": 0.02,
    "dropoff_datetime": 0.005,
    "rate_code": 0.03,
}
_SECONDS_PER_DAY = 86_400


def generate_trips(
    *,
    days: int = 10,
    rows_per_day: int = 200,
    start: dt.date = DEFAULT_START,
    seed: int = DEFAULT_SEED,
    empty_day_offsets: Collection[int] = DEFAULT_EMPTY_DAY_OFFSETS,
    money_as_decimal: bool = False,
) -> pa.Table:
    """Surowe przejazdy dla dni ``start .. start + days - 1``.

    :param days: liczba dni zakresu (łącznie z dniami pustymi).
    :param rows_per_day: liczba przejazdów w każdym dniu z danymi (co najmniej 3 – tyle
        wierszy zajmują wymuszone przypadki brzegowe).
    :param empty_day_offsets: przesunięcia (od ``start``) dni bez żadnych przejazdów.
    :param money_as_decimal: kwoty i dystans jako ``decimal128(38, 9)`` zamiast ``float64``.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    if rows_per_day < 3:
        raise ValueError("rows_per_day must be >= 3 (forced edge cases)")
    rng = random.Random(seed)
    columns: dict[str, list[Any]] = {field.name: [] for field in RAW_SCHEMA}
    empty = frozenset(empty_day_offsets)

    for offset in range(days):
        # Parametry dnia losowane zawsze (także dla dni pustych), więc dodanie dziury nie
        # zmienia danych pozostałych dni.
        fare_scale = rng.uniform(0.6, 1.6)
        tip_rate = 0.2 / fare_scale * rng.uniform(0.8, 1.2)
        day_rng = random.Random(f"{seed}:{offset}")
        if offset in empty:
            continue
        midnight = dt.datetime.combine(start + dt.timedelta(days=offset), dt.time())
        for index in range(rows_per_day):
            _append_trip(columns, day_rng, midnight, index, fare_scale, tip_rate)

    table = pa.table(columns, schema=RAW_SCHEMA)
    return to_decimal_money(table) if money_as_decimal else table


def to_decimal_money(table: pa.Table) -> pa.Table:
    """Kopia tabeli z kwotami i dystansem jako ``NUMERIC`` (dokładna konwersja przez
    zapis dziesiętny – wartości mają co najwyżej 2 miejsca po przecinku)."""
    for name in MONEY_COLUMNS:
        values = [
            None if value is None else Decimal(repr(value)) for value in table[name].to_pylist()
        ]
        position = table.schema.get_field_index(name)
        table = table.set_column(
            position, pa.field(name, NUMERIC_TYPE), pa.array(values, NUMERIC_TYPE)
        )
    return table


def _maybe_null(rng: random.Random, column: str, value: Any) -> Any:
    return None if rng.random() < _NULL_PROBABILITY[column] else value


def _append_trip(
    columns: dict[str, list[Any]],
    rng: random.Random,
    midnight: dt.datetime,
    index: int,
    fare_scale: float,
    tip_rate: float,
) -> None:
    payment_type = rng.choices([1, 2, PAYMENT_NO_CHARGE, PAYMENT_DISPUTE], [60, 28, 6, 6])[0]
    if index == 1:
        payment_type = PAYMENT_NO_CHARGE  # wymuszona grupa z zerową sumą opłat
    elif index == 2:
        payment_type = PAYMENT_DISPUTE  # wymuszona grupa z zerowym czasem

    if index == 0:  # przejazd przez północ
        pickup_second = _SECONDS_PER_DAY - 300 + rng.randrange(0, 240)
        duration = 600 + rng.randrange(0, 600)
    else:
        pickup_second = rng.randrange(0, _SECONDS_PER_DAY)
        duration = rng.randint(60, 3600)
    if payment_type == PAYMENT_DISPUTE or rng.random() < 0.01:
        duration = 0
    pickup = midnight + dt.timedelta(seconds=pickup_second)
    dropoff = pickup + dt.timedelta(seconds=duration)

    speed_mph = rng.uniform(5.0, 30.0)
    distance = 0.0 if payment_type == PAYMENT_DISPUTE else round(duration / 3600 * speed_mph, 2)
    fare = round((3.0 + 2.5 * distance + 0.5 * duration / 60) * fare_scale, 2)
    tip = round(fare * tip_rate * rng.uniform(0.5, 1.5), 2) if payment_type == 1 else 0.0
    if payment_type == PAYMENT_NO_CHARGE:
        fare = tip = 0.0
    extras = rng.choice([0.5, 1.0, 2.5, 3.0]) + 0.3
    total = round(fare + tip + extras, 2)

    columns["vendor_id"].append(rng.choices([1, 2], [45, 55])[0])
    columns["pickup_datetime"].append(pickup)
    columns["dropoff_datetime"].append(_maybe_null(rng, "dropoff_datetime", dropoff))
    columns["passenger_count"].append(_maybe_null(rng, "passenger_count", rng.randint(1, 4)))
    columns["trip_distance"].append(_maybe_null(rng, "trip_distance", distance))
    columns["rate_code"].append(
        _maybe_null(rng, "rate_code", rng.choices([1, 2, 5], [85, 10, 5])[0])
    )
    columns["payment_type"].append(payment_type)
    columns["fare_amount"].append(_maybe_null(rng, "fare_amount", fare))
    columns["tip_amount"].append(_maybe_null(rng, "tip_amount", tip))
    columns["total_amount"].append(_maybe_null(rng, "total_amount", total))
    columns["pickup_location_id"].append(rng.randint(1, 265))
    columns["dropoff_location_id"].append(rng.randint(1, 265))
