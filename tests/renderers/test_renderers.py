"""Testy jednostkowe rendererów: NULLIF guard, literały filtrów (SQL injection), funkcje
skalarne, błędy programistyczne, transpilacja fragmentów YAML BigQuery -> DuckDB."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from app.config.schemas import BaseMeasureConfig, DimensionConfig
from app.planner.builder import build_logical_query
from app.planner.logical_plan import LogicalQuery, PlannedDimension, PlannedFilter, PlannedMetric
from app.renderers.base import (
    MetricExprBuilder,
    aggregate,
    is_nonzero_constant,
    literal,
    parse_fragment,
)
from app.renderers.errors import (
    DimensionNotInRollupError,
    NonAdditiveMeasureError,
    SqlFragmentError,
    UnsupportedFunctionError,
)
from app.renderers.raw_table import RawTableRenderer
from app.renderers.rollup import RollupRenderer, rollup_glob, rollup_glob_for
from app.semantic import registry
from app.semantic.ast_nodes import (
    BinaryOp,
    Expr,
    FunctionCall,
    Literal,
    MeasureRef,
    UnaryOp,
    measure_refs,
)
from app.semantic.registry import FunctionSpec
from app.semantic.resolver import SemanticModel
from tests.conftest import make_request

A = MeasureRef("total_tip_amount")
B = MeasureRef("total_fare_amount")
ROLLUP = RollupRenderer("./data/nyc_taxi/pickup_date=*/rollup.parquet")
RAW = RawTableRenderer("raw_trips")


@pytest.fixture
def base_plan(models: dict[str, SemanticModel]) -> LogicalQuery:
    return build_logical_query(
        make_request(dimensions=[], filters=[], order_by=[], limit=None), models
    )


def with_metric(plan: LogicalQuery, expr: Expr, model: SemanticModel) -> LogicalQuery:
    measures = {name: model.config.base_measures[name] for name in sorted(measure_refs(expr))}
    metric = PlannedMetric("m", expr, frozenset(measures), True)
    return dataclasses.replace(plan, metrics=(metric,), base_measures=MappingProxyType(measures))


def metric_sql(
    expr: Expr, model: SemanticModel, plan: LogicalQuery, dialect: str = "duckdb"
) -> str:
    select = ROLLUP.build(with_metric(plan, expr, model), dialect)
    sql: str = select.expressions[0].this.sql(dialect=dialect)
    return sql


# --------------------------------------------------------------------------- #
# NULLIF guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        # dzielenie przez wyrażenie -> NULLIF
        (BinaryOp("/", A, B), "SUM(total_tip_amount) / NULLIF(SUM(total_fare_amount), 0)"),
        # przez złożone wyrażenie -> NULLIF((...), 0)
        (
            BinaryOp("/", A, BinaryOp("-", B, A)),
            "SUM(total_tip_amount) / NULLIF((SUM(total_fare_amount) - SUM(total_tip_amount)), 0)",
        ),
        # przez niezerowy literał -> bez NULLIF
        (BinaryOp("/", A, Literal(3600.0)), "SUM(total_tip_amount) / 3600.0"),
        (BinaryOp("/", A, Literal(2)), "SUM(total_tip_amount) / 2"),
        # przez ujemny niezerowy literał (UnaryOp nad Literal) -> bez NULLIF
        (BinaryOp("/", A, UnaryOp("-", Literal(3600.0))), "SUM(total_tip_amount) / -3600.0"),
        # przez literał 0 -> NULLIF(0, 0) (renderer jest totalny; config z `/ 0` odrzuca A2)
        (BinaryOp("/", A, Literal(0)), "SUM(total_tip_amount) / NULLIF(0, 0)"),
        (BinaryOp("/", A, Literal(0.0)), "SUM(total_tip_amount) / NULLIF(0.0, 0)"),
        # przez funkcję ze stałą -> NULLIF (renderer nie wylicza wartości funkcji)
        (
            BinaryOp("/", A, FunctionCall("ABS", (Literal(5),))),
            "SUM(total_tip_amount) / NULLIF(ABS(5), 0)",
        ),
        # zagnieżdżone dzielenia – każde chronione osobno
        (
            BinaryOp("/", BinaryOp("/", A, B), B),
            "(SUM(total_tip_amount) / NULLIF(SUM(total_fare_amount), 0))"
            " / NULLIF(SUM(total_fare_amount), 0)",
        ),
        # mnożenie nie jest chronione
        (BinaryOp("*", A, Literal(0)), "SUM(total_tip_amount) * 0"),
    ],
)
def test_nullif_guard(
    nyc_model: SemanticModel, base_plan: LogicalQuery, expr: Expr, expected: str
) -> None:
    assert metric_sql(expr, nyc_model, base_plan) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (Literal(3600.0), True),
        (Literal(0), False),
        (Literal(0.0), False),
        (UnaryOp("-", Literal(1)), True),
        (UnaryOp("-", Literal(0)), False),
        (A, False),
        (FunctionCall("ABS", (Literal(1),)), False),
    ],
)
def test_is_nonzero_constant(expr: Expr, expected: bool) -> None:
    assert is_nonzero_constant(expr) is expected


def test_parentheses_follow_ast_not_precedence(
    nyc_model: SemanticModel, base_plan: LogicalQuery
) -> None:
    # a - (b - a): bez nawiasów zmieniłaby się wartość
    expr = BinaryOp("-", A, BinaryOp("-", B, A))
    assert (
        metric_sql(expr, nyc_model, base_plan)
        == "SUM(total_tip_amount) - (SUM(total_fare_amount) - SUM(total_tip_amount))"
    )
    # -(a + b) i -(-a) – w drugim przypadku nie może powstać komentarz SQL "--"
    assert (
        metric_sql(UnaryOp("-", BinaryOp("+", A, B)), nyc_model, base_plan)
        == "-(SUM(total_tip_amount) + SUM(total_fare_amount))"
    )
    double_neg = metric_sql(UnaryOp("-", UnaryOp("-", A)), nyc_model, base_plan)
    assert "--" not in double_neg
    assert double_neg == "- -SUM(total_tip_amount)"
    reparsed = sqlglot.parse_one(double_neg, read="duckdb")
    assert isinstance(reparsed, exp.Neg)
    assert isinstance(reparsed.this, exp.Neg)


# --------------------------------------------------------------------------- #
# Funkcje skalarne
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "args", "duckdb", "bigquery"),
    [
        ("ROUND", (A, Literal(2)), "ROUND(SUM(total_tip_amount), 2)", None),
        ("ROUND", (A,), "ROUND(SUM(total_tip_amount))", None),
        ("ABS", (A,), "ABS(SUM(total_tip_amount))", None),
        (
            "COALESCE",
            (A, B, Literal(0)),
            "COALESCE(SUM(total_tip_amount), SUM(total_fare_amount), 0)",
            None,
        ),
        # GREATEST/LEAST: semantyka BigQuery (NULL, gdy którykolwiek argument NULL)
        # utrwalona w drzewie; generator DuckDB dopisuje jawny CASE.
        (
            "GREATEST",
            (A, B),
            "CASE WHEN SUM(total_tip_amount) IS NULL OR SUM(total_fare_amount) IS NULL "
            "THEN NULL ELSE GREATEST(SUM(total_tip_amount), SUM(total_fare_amount)) END",
            "GREATEST(SUM(total_tip_amount), SUM(total_fare_amount))",
        ),
        (
            "LEAST",
            (A, B),
            "CASE WHEN SUM(total_tip_amount) IS NULL OR SUM(total_fare_amount) IS NULL "
            "THEN NULL ELSE LEAST(SUM(total_tip_amount), SUM(total_fare_amount)) END",
            "LEAST(SUM(total_tip_amount), SUM(total_fare_amount))",
        ),
    ],
)
def test_scalar_functions(
    nyc_model: SemanticModel,
    base_plan: LogicalQuery,
    name: str,
    args: tuple[Expr, ...],
    duckdb: str,
    bigquery: str | None,
) -> None:
    expr = FunctionCall(name, args)
    assert metric_sql(expr, nyc_model, base_plan, "duckdb") == duckdb
    assert metric_sql(expr, nyc_model, base_plan, "bigquery") == (bigquery or duckdb)


def test_dialect_renderer_override(
    monkeypatch: pytest.MonkeyPatch, nyc_model: SemanticModel, base_plan: LogicalQuery
) -> None:
    """Punkt rozszerzenia ``FunctionSpec.dialect_renderers`` – nadpisanie dla jednego
    dialektu, bez wpływu na pozostałe."""

    def round_half_even(args: Sequence[exp.Expr]) -> exp.Expr:
        return exp.Anonymous(this="ROUND_EVEN", expressions=list(args))

    spec = registry.SCALAR_FUNCTIONS["ROUND"]
    patched = dataclasses.replace(
        spec, dialect_renderers=MappingProxyType({"duckdb": round_half_even})
    )
    functions = dict(registry.SCALAR_FUNCTIONS)
    functions["ROUND"] = patched
    monkeypatch.setattr(registry, "SCALAR_FUNCTIONS", MappingProxyType(functions))

    expr = FunctionCall("ROUND", (A, Literal(2)))
    assert (
        metric_sql(expr, nyc_model, base_plan, "duckdb") == "ROUND_EVEN(SUM(total_tip_amount), 2)"
    )
    assert metric_sql(expr, nyc_model, base_plan, "bigquery") == "ROUND(SUM(total_tip_amount), 2)"
    # build() bez dialektu – drzewo ogólne
    select = ROLLUP.build(with_metric(base_plan, expr, nyc_model))
    assert select.expressions[0].this.sql() == "ROUND(SUM(total_tip_amount), 2)"


def test_dialect_renderer_must_return_sqlglot_node(
    monkeypatch: pytest.MonkeyPatch, nyc_model: SemanticModel, base_plan: LogicalQuery
) -> None:
    spec = dataclasses.replace(
        registry.SCALAR_FUNCTIONS["ABS"],
        dialect_renderers=MappingProxyType({"duckdb": lambda args: "ABS(x)"}),
    )
    monkeypatch.setattr(
        registry, "SCALAR_FUNCTIONS", MappingProxyType({**registry.SCALAR_FUNCTIONS, "ABS": spec})
    )
    with pytest.raises(UnsupportedFunctionError, match="expected sqlglot"):
        metric_sql(FunctionCall("ABS", (A,)), nyc_model, base_plan, "duckdb")


def test_unregistered_function_and_aggregation() -> None:
    builder = MetricExprBuilder(lambda name: exp.column(name), None)
    with pytest.raises(UnsupportedFunctionError, match="not registered"):
        builder.visit(FunctionCall("SQRT", (A,)))
    with pytest.raises(UnsupportedFunctionError, match="MEDIAN"):
        aggregate("MEDIAN", exp.column("x"))


def test_new_registry_function_renders_generically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nowa funkcja w rejestrze bez własnego buildera -> ``exp.func`` (typowany węzeł)."""
    spec = FunctionSpec("SIGN", 1, 1, "Znak liczby.")
    monkeypatch.setattr(
        registry, "SCALAR_FUNCTIONS", MappingProxyType({**registry.SCALAR_FUNCTIONS, "SIGN": spec})
    )
    builder = MetricExprBuilder(lambda name: exp.column(name), "duckdb")
    assert builder.visit(FunctionCall("SIGN", (A,))).sql("duckdb") == "SIGN(total_tip_amount)"


# --------------------------------------------------------------------------- #
# Literały filtrów i SQL injection
# --------------------------------------------------------------------------- #

MALICIOUS = [
    "1' OR '1'='1",
    "x'; DROP TABLE raw_trips; --",
    "back\\slash' OR 1=1 --",
    "\\'; SELECT 1; --",
    "line\nbreak' /* comment */",
    "O'Brien",
    "' UNION SELECT * FROM secrets --",
]


@pytest.mark.parametrize("value", MALICIOUS)
@pytest.mark.parametrize(
    ("renderer", "dialect"),
    [(ROLLUP, "duckdb"), (RAW, "bigquery"), (RAW, "duckdb")],
    ids=["rollup-duckdb", "raw-bigquery", "raw-duckdb"],
)
def test_filter_values_are_safe_literals(
    models: dict[str, SemanticModel], value: str, renderer: Any, dialect: str
) -> None:
    plan = build_logical_query(
        make_request(
            filters=[
                {"field": "payment_type", "op": "==", "value": value},
                {"field": "vendor_id", "op": "in", "value": [value, "2"]},
            ]
        ),
        models,
    )
    sql = renderer.render(plan, dialect)

    # Ponowne parsowanie: dokładnie jedno zapytanie, a w WHERE dokładnie dwa warunki,
    # których literały mają oryginalną wartość (apostrof nie zamknął napisu).
    statements = sqlglot.parse(sql, read=dialect)
    assert len(statements) == 1
    select = statements[0]
    assert isinstance(select, exp.Select)
    where = select.args["where"].this
    assert isinstance(where, exp.And)
    eq, in_ = where.this, where.expression
    assert isinstance(eq, exp.EQ)
    assert isinstance(in_, exp.In)
    assert eq.expression.is_string and eq.expression.this == value
    assert [e.this for e in in_.expressions] == [value, "2"]
    assert not list(select.find_all(exp.Drop, exp.Union, exp.Or))
    table = select.find(exp.Table)
    assert table is not None
    assert table.name != "secrets"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "'abc'"),
        ("O'Brien", "'O''Brien'"),
        (5, "5"),
        (-5, "-5"),
        (2.5, "2.5"),
        (dt.date(2022, 12, 1), "CAST('2022-12-01' AS DATE)"),
    ],
)
def test_literal(value: Any, expected: str) -> None:
    assert literal(value).sql(dialect="duckdb") == expected


def test_number_filter_literal(nyc_model: SemanticModel, base_plan: LogicalQuery) -> None:
    dim = PlannedDimension(
        "n", DimensionConfig(sql="passenger_count", type="number", in_rollup=True)
    )
    plan = dataclasses.replace(base_plan, filters=(PlannedFilter(dim, "in", (1, -2, 3.5)),))
    sql = RAW.render(plan, "bigquery")
    assert "WHERE passenger_count IN (1, -2, 3.5)" in sql


# --------------------------------------------------------------------------- #
# RollupRenderer
# --------------------------------------------------------------------------- #


def test_rollup_glob(nyc_model: SemanticModel) -> None:
    assert rollup_glob("./data/nyc_taxi", "pickup_date") == (
        "./data/nyc_taxi/pickup_date=*/rollup.parquet"
    )
    assert rollup_glob("/srv/data/", "d") == "/srv/data/d=*/rollup.parquet"
    assert rollup_glob_for(nyc_model.config.storage.local_cache) == (
        "./data/nyc_taxi/pickup_date=*/rollup.parquet"
    )


def test_rollup_source(base_plan: LogicalQuery) -> None:
    sql = RollupRenderer("/tmp/x/d=*/rollup.parquet").render(base_plan, "duckdb")
    assert "FROM READ_PARQUET('/tmp/x/d=*/rollup.parquet', hive_partitioning = TRUE)" in sql


def test_rollup_glob_with_apostrophe_is_escaped(base_plan: LogicalQuery) -> None:
    sql = RollupRenderer("/data/o'neil/d=*/rollup.parquet").render(base_plan, "duckdb")
    table = sqlglot.parse_one(sql, read="duckdb").find(exp.ReadParquet)
    assert table is not None
    assert table.expressions[0].this == "/data/o'neil/d=*/rollup.parquet"


def test_rollup_rejects_non_additive_measure(
    nyc_model: SemanticModel, base_plan: LogicalQuery
) -> None:
    plan = with_metric(base_plan, MeasureRef("unique_pickup_zones"), nyc_model)
    with pytest.raises(NonAdditiveMeasureError) as info:
        ROLLUP.render(plan, "duckdb")
    assert info.value.measure == "unique_pickup_zones"
    assert info.value.aggregation == "COUNT_DISTINCT"
    # RawTableRenderer obsługuje miarę holistyczną
    assert "COUNT(DISTINCT pickup_location_id) AS m" in RAW.render(plan, "duckdb")


def test_rollup_rejects_non_rollup_dimension(models: dict[str, SemanticModel]) -> None:
    for overrides in (
        {"dimensions": ["dropoff_location_id"], "order_by": []},
        {"filters": [{"field": "dropoff_location_id", "op": "==", "value": "1"}]},
    ):
        plan = build_logical_query(make_request(**overrides), models)
        with pytest.raises(DimensionNotInRollupError, match="dropoff_location_id"):
            ROLLUP.render(plan, "duckdb")
        RAW.render(plan, "bigquery")  # surowa tabela – bez ograniczeń


def test_rollup_count_reaggregated_as_sum(
    nyc_model: SemanticModel, base_plan: LogicalQuery
) -> None:
    plan = with_metric(base_plan, MeasureRef("trips_count"), nyc_model)
    assert ROLLUP.render(plan, "duckdb").startswith("SELECT SUM(trips_count) AS m")
    assert RAW.render(plan, "duckdb").startswith("SELECT COUNT(1) AS m")


def test_rollup_min_max(base_plan: LogicalQuery) -> None:
    measures = {
        "min_fare": BaseMeasureConfig(aggregation="MIN", sql="fare_amount"),
        "max_fare": BaseMeasureConfig(aggregation="MAX", sql="fare_amount"),
    }
    plan = dataclasses.replace(
        base_plan,
        metrics=(
            PlannedMetric("lo", MeasureRef("min_fare"), frozenset({"min_fare"}), True),
            PlannedMetric("hi", MeasureRef("max_fare"), frozenset({"max_fare"}), True),
        ),
        base_measures=MappingProxyType(measures),
    )
    assert ROLLUP.render(plan, "duckdb") == (
        "SELECT MIN(min_fare) AS lo, MAX(max_fare) AS hi "
        "FROM READ_PARQUET('./data/nyc_taxi/pickup_date=*/rollup.parquet', "
        "hive_partitioning = TRUE)"
    )
    assert RAW.render(plan, "duckdb") == (
        "SELECT MIN(fare_amount) AS lo, MAX(fare_amount) AS hi FROM raw_trips"
    )


# --------------------------------------------------------------------------- #
# RawTableRenderer i fragmenty SQL z YAML
# --------------------------------------------------------------------------- #


def test_raw_table_name_in_bigquery_notation() -> None:
    renderer = RawTableRenderer("bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022")
    table = renderer.source()
    assert isinstance(table, exp.Table)
    assert (table.catalog, table.db, table.name) == (
        "bigquery-public-data",
        "new_york_taxi_trips",
        "tlc_yellow_trips_2022",
    )


def test_invalid_sql_fragment(nyc_model: SemanticModel, base_plan: LogicalQuery) -> None:
    measures = {"bad": BaseMeasureConfig(aggregation="SUM", sql="fare_amount +* 2")}
    plan = dataclasses.replace(
        base_plan,
        metrics=(PlannedMetric("m", MeasureRef("bad"), frozenset({"bad"}), True),),
        base_measures=MappingProxyType(measures),
    )
    with pytest.raises(SqlFragmentError, match="'bad'"):
        RAW.render(plan, "duckdb")


def test_parse_fragment_returns_independent_copies() -> None:
    first = parse_fragment("CAST(vendor_id AS STRING)", "vendor_id")
    second = parse_fragment("CAST(vendor_id AS STRING)", "vendor_id")
    assert first == second
    assert first is not second


def test_render_is_repeatable(models: dict[str, SemanticModel]) -> None:
    plan = build_logical_query(make_request(), models)
    for renderer, dialect in ((ROLLUP, "duckdb"), (RAW, "bigquery"), (RAW, "duckdb")):
        assert renderer.render(plan, dialect) == renderer.render(plan, dialect)


def test_build_returns_select_tree(models: dict[str, SemanticModel]) -> None:
    plan = build_logical_query(make_request(), models)
    select = ROLLUP.build(plan)
    assert isinstance(select, exp.Select)
    assert [e.alias_or_name for e in select.expressions] == [
        "pickup_date",
        "vendor_id",
        "trips_count",
        "total_revenue",
        "tip_to_fare_ratio_pct",
        "avg_trip_speed_mph",
    ]
    assert select.args["limit"].expression.this == "1000"


def test_raw_group_by_uses_full_expressions(models: dict[str, SemanticModel]) -> None:
    plan = build_logical_query(make_request(order_by=[]), models)
    group = RAW.build(plan).args["group"].expressions
    assert [g.sql(dialect="bigquery") for g in group] == [
        "DATE(pickup_datetime)",
        "CAST(vendor_id AS STRING)",
    ]


def test_order_by_nulls_last_is_portable(models: dict[str, SemanticModel]) -> None:
    plan = build_logical_query(
        make_request(
            order_by=[{"field": "vendor_id"}, {"field": "trips_count", "direction": "DESC"}]
        ),
        models,
    )
    # BigQuery domyślnie ASC NULLS FIRST -> jawne NULLS LAST; DuckDB – domyślne, pomijane.
    assert RAW.render(plan, "bigquery").endswith(
        "ORDER BY vendor_id ASC NULLS LAST, trips_count DESC LIMIT 1000"
    )
    assert ROLLUP.render(plan, "duckdb").endswith(
        "ORDER BY vendor_id ASC, trips_count DESC LIMIT 1000"
    )


# --------------------------------------------------------------------------- #
# Transpilacja fragmentów YAML (BigQuery -> DuckDB) – obserwacje dla autora
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fragment", "duckdb"),
    [
        ("CAST(vendor_id AS STRING)", "CAST(vendor_id AS TEXT)"),
        ("DATE(pickup_datetime)", "CAST(pickup_datetime AS DATE)"),
        # Kolejność argumentów odwrócona poprawnie (start, koniec). UWAGA semantyczna:
        # DuckDB DATE_DIFF liczy przekroczone granice jednostki, BigQuery TIMESTAMP_DIFF
        # obcina różnicę – wyniki różnią się dla znaczników z ułamkami sekund
        # (np. 00:00:00.9 -> 00:00:01.1: DuckDB 1, BigQuery 0). Zgłoszone autorowi (A3).
        (
            "TIMESTAMP_DIFF(dropoff_datetime, pickup_datetime, SECOND)",
            "DATE_DIFF('SECOND', pickup_datetime, dropoff_datetime)",
        ),
    ],
)
def test_bigquery_fragments_transpile_to_duckdb(fragment: str, duckdb: str) -> None:
    node = parse_fragment(fragment, "x")
    assert node.sql(dialect="duckdb") == duckdb
    assert node.sql(dialect="bigquery") == fragment


def test_inconsistent_plan_is_programming_error(
    nyc_model: SemanticModel, base_plan: LogicalQuery
) -> None:
    # liść spoza plan.base_measures
    plan = dataclasses.replace(base_plan, base_measures=MappingProxyType({}))
    with pytest.raises(KeyError, match="not part of the plan"):
        ROLLUP.render(plan, "duckdb")
    # agregacja spoza rejestru (model ominął walidację semantyczną)
    with pytest.raises(UnsupportedFunctionError, match="unknown aggregation"):
        ROLLUP.measure_expr("x", BaseMeasureConfig(aggregation="MEDIAN", sql="x"))
