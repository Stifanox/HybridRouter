"""Strażnik kosztów – czysta funkcja ``evaluate_cost`` (F2-11, warstwa 1).

Konfiguracje testowe mają cenę 1 USD/TiB, więc koszt = bajty / 2^40 i równość z limitem
kosztu jest osiągalna dokładnie (bez błędu zaokrąglenia).
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.config.schemas import ActionOnExceed, CostGuardConfig
from app.warehouse.cost_guard import (
    BYTES_PER_TIB,
    MIN_BYTES_BILLED,
    CostConfirmationRequiredException,
    CostTooHighForQueryException,
    actual_cost_usd,
    cost_usd,
    effective_limit_bytes,
    enforce,
    estimated_cost_usd,
    evaluate_cost,
    over_limit_bytes,
)

GIB = 1024**3
TIB = BYTES_PER_TIB


def guard(
    action: ActionOnExceed = "require_confirmation",
    *,
    enabled: bool = True,
    price: float = 1.0,
    max_bytes: int = 100 * GIB,
    max_cost: float = 1.0,
) -> CostGuardConfig:
    return CostGuardConfig(
        enabled=enabled,
        price_per_tb_usd=price,
        max_bytes_per_query=max_bytes,
        max_cost_usd_per_query=max_cost,
        action_on_exceed=action,
    )


# Dwa warianty limitów: w BYTES wiąże limit bajtów (100 GiB < 1 TiB odpowiadający 1 USD),
# w COST – limit kosztu (1 USD = 1 TiB < 2 TiB).
LIMITS: dict[str, dict[str, Any]] = {
    "bytes": {"max_bytes": 100 * GIB, "max_cost": 1.0},
    "cost": {"max_bytes": 2 * TIB, "max_cost": 1.0},
}
BOUNDARY = {"bytes": 100 * GIB, "cost": TIB}
EXCEEDED_NAME = {"bytes": "max_bytes_per_query", "cost": "max_cost_usd_per_query"}
POSITIONS = {"below": -1, "equal": 0, "above": 1}

# Tabela decyzji (docstring app.warehouse.cost_guard): (enabled, action, confirmed, exceeded).
EXPECTED_ACTION = {
    (True, "raise_error", False, False): "allow",
    (True, "raise_error", True, False): "allow",
    (True, "raise_error", False, True): "reject",
    (True, "raise_error", True, True): "reject",
    (True, "require_confirmation", False, False): "allow",
    (True, "require_confirmation", True, False): "allow",
    (True, "require_confirmation", False, True): "require_confirmation",
    (True, "require_confirmation", True, True): "allow_over_limit",
    (True, "silent_allow", False, False): "allow",
    (True, "silent_allow", True, False): "allow",
    (True, "silent_allow", False, True): "allow_over_limit",
    (True, "silent_allow", True, True): "allow_over_limit",
}
for _action in ("raise_error", "require_confirmation", "silent_allow"):
    for _confirmed in (False, True):
        for _exceeded in (False, True):
            EXPECTED_ACTION[(False, _action, _confirmed, _exceeded)] = "allow"

CASES = [
    pytest.param(
        enabled,
        action,
        confirmed,
        limit,
        position,
        id=f"{'on' if enabled else 'off'}-"
        f"{action}-{'confirmed' if confirmed else 'unconfirmed'}-{limit}-{position}",
    )
    for enabled in (True, False)
    for action in ("raise_error", "require_confirmation", "silent_allow")
    for confirmed in (False, True)
    for limit in LIMITS
    for position in POSITIONS
]


@pytest.mark.parametrize(("enabled", "action", "confirmed", "limit", "position"), CASES)
def test_decision_table(
    enabled: bool, action: ActionOnExceed, confirmed: bool, limit: str, position: str
) -> None:
    config = guard(action, enabled=enabled, **LIMITS[limit])
    bytes_processed = BOUNDARY[limit] + POSITIONS[position]
    exceeded = position == "above"

    decision = evaluate_cost(bytes_processed, config, confirmed=confirmed)

    assert decision.action == EXPECTED_ACTION[(enabled, action, confirmed, exceeded)]
    assert decision.exceeded is exceeded
    assert decision.exceeded_limits == ((EXCEEDED_NAME[limit],) if exceeded else ())
    assert decision.bytes_processed == bytes_processed
    assert decision.estimated_cost_usd == bytes_processed / TIB
    assert decision.guard_enabled is enabled
    assert decision.limits == {
        "max_bytes_per_query": LIMITS[limit]["max_bytes"],
        "max_cost_usd_per_query": 1.0,
    }
    expected_max_billed = {
        "allow": None if not enabled else min(LIMITS[limit]["max_bytes"], TIB),
        "allow_over_limit": math.ceil(bytes_processed * 1.1),
        "reject": None,
        "require_confirmation": None,
    }[decision.action]
    assert decision.maximum_bytes_billed == expected_max_billed
    assert decision.runnable is (decision.action in ("allow", "allow_over_limit"))


def test_case_table_covers_every_combination() -> None:
    assert len(CASES) == 2 * 3 * 2 * len(LIMITS) * len(POSITIONS) == 72
    assert len(EXPECTED_ACTION) == 2 * 3 * 2 * 2


def test_both_limits_exceeded_are_reported() -> None:
    decision = evaluate_cost(3 * TIB, guard(**LIMITS["cost"]), confirmed=False)
    assert decision.exceeded_limits == ("max_bytes_per_query", "max_cost_usd_per_query")
    assert decision.action == "require_confirmation"


def test_nyc_taxi_limits(nyc_model: Any) -> None:
    config = nyc_model.config.cost_guard
    # 10 GiB przy 6,25 USD/TiB = 0,061 USD < 0,0625 USD -> wiąże limit bajtów.
    assert effective_limit_bytes(config) == 10 * GIB
    assert evaluate_cost(10 * GIB, config, confirmed=False).action == "allow"
    over = evaluate_cost(10 * GIB + 1, config, confirmed=False)
    assert (over.action, over.exceeded_limits) == ("require_confirmation", ("max_bytes_per_query",))


def test_disabled_guard_without_dry_run() -> None:
    decision = evaluate_cost(None, guard(enabled=False), confirmed=False)
    assert decision.action == "allow"
    assert decision.bytes_processed is None
    assert decision.estimated_cost_usd is None
    assert decision.exceeded is False
    assert decision.maximum_bytes_billed is None


def test_bytes_required_when_enabled() -> None:
    with pytest.raises(ValueError, match="required"):
        evaluate_cost(None, guard(), confirmed=True)


def test_negative_bytes_rejected() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        evaluate_cost(-1, guard(), confirmed=False)


def test_zero_bytes_is_allowed() -> None:
    decision = evaluate_cost(0, guard(), confirmed=False)
    assert (decision.action, decision.estimated_cost_usd) == ("allow", 0.0)


def test_max_billed_never_below_billing_minimum() -> None:
    # Limit 1 MiB < minimum 10 MB – BigQuery odrzuciłoby każde zadanie z takim limitem.
    config = guard("silent_allow", max_bytes=1024**2, max_cost=100.0)
    assert evaluate_cost(100, config, confirmed=False).maximum_bytes_billed == MIN_BYTES_BILLED
    over = evaluate_cost(2 * 1024**2, config, confirmed=False)
    assert over.action == "allow_over_limit"
    assert over.maximum_bytes_billed == MIN_BYTES_BILLED  # 2,2 MiB < 10 MiB
    assert over_limit_bytes(0) == MIN_BYTES_BILLED
    assert over_limit_bytes(100 * GIB) == math.ceil(100 * GIB * 1.1)


def test_effective_limit_uses_cost_threshold() -> None:
    assert effective_limit_bytes(guard(price=6.25, max_bytes=TIB, max_cost=0.0625)) == math.floor(
        0.0625 / 6.25 * TIB
    )
    assert effective_limit_bytes(guard(max_bytes=5 * GIB, max_cost=1.0)) == 5 * GIB


def test_cost_functions() -> None:
    config = guard(price=6.25)
    assert cost_usd(TIB, 6.25) == 6.25
    assert estimated_cost_usd(TIB // 2, config) == 3.125
    # Koszt rzeczywisty z bytes_billed (minimum 10 MB).
    assert actual_cost_usd(MIN_BYTES_BILLED, config) == pytest.approx(10 / 1024**2 * 6.25)
    assert actual_cost_usd(0, config) == 0.0


def test_enforce_raises_for_blocking_actions() -> None:
    sql = "SELECT 1"
    reject = evaluate_cost(200 * GIB, guard("raise_error"), confirmed=True)
    with pytest.raises(CostTooHighForQueryException) as too_high:
        enforce(reject, sql)
    exc = too_high.value
    assert (exc.sql, exc.bytes_processed) == (sql, 200 * GIB)
    assert exc.estimated_cost_usd == 200 * GIB / TIB
    assert exc.limits == {"max_bytes_per_query": 100 * GIB, "max_cost_usd_per_query": 1.0}
    assert exc.exceeded_limits == ("max_bytes_per_query",)
    assert "reject" in str(exc)

    pending = evaluate_cost(200 * GIB, guard("require_confirmation"), confirmed=False)
    with pytest.raises(CostConfirmationRequiredException) as confirm:
        enforce(pending, sql)
    assert confirm.value.decision is pending
    assert "confirmation required" in str(confirm.value)


@pytest.mark.parametrize("action", ["silent_allow", "require_confirmation"])
def test_enforce_passes_runnable_decisions(action: ActionOnExceed) -> None:
    enforce(evaluate_cost(1, guard(action), confirmed=True), "SELECT 1")
    enforce(evaluate_cost(200 * GIB, guard(action), confirmed=True), "SELECT 1")
