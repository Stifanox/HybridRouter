"""Strażnik kosztów hurtowni – czysta funkcja decyzji (ADR-006; dok. 03, Moduł 3; F2-9).

:func:`evaluate_cost` nie wykonuje żadnych operacji wejścia/wyjścia: na podstawie bajtów z
dry-runu, sekcji ``cost_guard`` konfiguracji i potwierdzenia klienta zwraca
:class:`CostDecision`. Tę samą funkcję wywołują wykonawca zapytań API, synchronizator
rollupów i skrypt ładowania kopii (w CLI potwierdzenie = flaga ``--yes``).
:func:`enforce` zamienia decyzję blokującą na wyjątek (403 / 409 – mapowanie HTTP w API).

Tabela decyzji (``exceeded`` = ``bytes > max_bytes_per_query`` **lub**
``koszt > max_cost_usd_per_query``; równość nie jest przekroczeniem):

=========  ==========  ======================  ===========  ====================  ===========
enabled    exceeded    action_on_exceed        confirmed    action                max. bajty
=========  ==========  ======================  ===========  ====================  ===========
False      dowolne     dowolny                 dowolne      allow                 ``None``
True       False       dowolny                 dowolne      allow                 limit
True       True        raise_error             dowolne      reject                ``None``
True       True        require_confirmation    False        require_confirmation  ``None``
True       True        require_confirmation    True         allow_over_limit      bajty+10%
True       True        silent_allow            dowolne      allow_over_limit      bajty+10%
=========  ==========  ======================  ===========  ====================  ===========

„max. bajty” to ``maximum_bytes_billed`` zadania (twardy limit egzekwowany przez hurtownię):

* ``limit`` – efektywny limit: mniejszy z ``max_bytes_per_query`` i liczby bajtów
  odpowiadającej ``max_cost_usd_per_query`` (oba progi chronione także po stronie hurtowni),
* ``bajty+10%`` – ``ceil(bytes_processed * 1.1)``; błąd oszacowania dry-runu nie może
  przekroczyć budżetu zapytania o więcej niż margines,
* obie wartości nie są mniejsze niż minimum rozliczeniowe :data:`MIN_BYTES_BILLED`
  (BigQuery odrzuca zadanie, którego ``maximum_bytes_billed`` jest mniejsze od minimum
  naliczanego za tabelę – limit poniżej 10 MB blokowałby każde zapytanie),
* ``None`` przy wyłączonym strażniku – brak limitu (``enabled: false`` to świadome
  wyłączenie ochrony, np. dla pomiaru bez narzutu dry-runu); ``None`` przy akcjach
  blokujących – zadanie nie zostanie uruchomione.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from app.config.schemas import CostGuardConfig

BYTES_PER_TIB = 1024**4
"""Cennik BigQuery on-demand jest podawany per TiB (2^40 bajtów; dok. 03, Moduł 3)."""

MIN_BYTES_BILLED = 10 * 1024**2
"""Minimum rozliczeniowe BigQuery on-demand: 10 MB na każdą tabelę w zapytaniu (MB = 2^20)."""

OVER_LIMIT_MARGIN = 0.10
"""Margines ``maximum_bytes_billed`` ponad ``bytes_processed`` przy dopuszczonym przekroczeniu."""

CostAction = Literal["allow", "allow_over_limit", "require_confirmation", "reject"]
LimitName = Literal["max_bytes_per_query", "max_cost_usd_per_query"]


def cost_usd(bytes_count: int, price_per_tb_usd: float) -> float:
    """Koszt w USD: ``bytes / 1024**4 * price_per_tb_usd``."""
    return bytes_count / BYTES_PER_TIB * price_per_tb_usd


def estimated_cost_usd(bytes_processed: int, config: CostGuardConfig) -> float:
    """Koszt **szacowany** – z ``total_bytes_processed`` dry-runu."""
    return cost_usd(bytes_processed, config.price_per_tb_usd)


def actual_cost_usd(bytes_billed: int, config: CostGuardConfig) -> float:
    """Koszt **rzeczywisty** – z ``total_bytes_billed`` wykonanego zadania (z minimum
    rozliczeniowym; 0 przy trafieniu w cache wyników)."""
    return cost_usd(bytes_billed, config.price_per_tb_usd)


def effective_limit_bytes(config: CostGuardConfig) -> int:
    """Twardy limit bajtów zgodny z obydwoma progami: ``min(max_bytes_per_query,
    floor(max_cost_usd_per_query / price * TiB))``, nie mniejszy niż :data:`MIN_BYTES_BILLED`."""
    bytes_for_cost = math.floor(
        config.max_cost_usd_per_query / config.price_per_tb_usd * BYTES_PER_TIB
    )
    return max(min(config.max_bytes_per_query, bytes_for_cost), MIN_BYTES_BILLED)


def over_limit_bytes(bytes_processed: int) -> int:
    """``maximum_bytes_billed`` przy dopuszczonym przekroczeniu: bajty z dry-runu + 10%."""
    return max(math.ceil(bytes_processed * (1 + OVER_LIMIT_MARGIN)), MIN_BYTES_BILLED)


@dataclass(frozen=True, slots=True)
class CostDecision:
    """Decyzja strażnika kosztów.

    ``bytes_processed`` / ``estimated_cost_usd`` są ``None``, gdy dry-run pominięto
    (strażnik wyłączony). ``exceeded_limits`` wymienia przekroczone progi (pusty, gdy
    ``exceeded`` jest fałszywe). ``maximum_bytes_billed`` – limit do ustawienia w zadaniu
    (``None``: bez limitu albo zadanie nie zostanie uruchomione – ``runnable``).
    """

    bytes_processed: int | None
    estimated_cost_usd: float | None
    exceeded: bool
    exceeded_limits: tuple[LimitName, ...]
    action: CostAction
    maximum_bytes_billed: int | None
    guard_enabled: bool
    max_bytes_per_query: int
    max_cost_usd_per_query: float

    @property
    def runnable(self) -> bool:
        """Czy zapytanie może zostać wykonane (``allow`` / ``allow_over_limit``)."""
        return self.action in ("allow", "allow_over_limit")

    @property
    def limits(self) -> dict[str, int | float]:
        """Progi konfiguracji (do treści odpowiedzi 403/409)."""
        return {
            "max_bytes_per_query": self.max_bytes_per_query,
            "max_cost_usd_per_query": self.max_cost_usd_per_query,
        }


def evaluate_cost(
    bytes_processed: int | None, config: CostGuardConfig, *, confirmed: bool
) -> CostDecision:
    """Decyzja strażnika kosztów (tabela w docstringu modułu).

    :param bytes_processed: bajty z dry-runu; ``None`` dozwolone tylko przy wyłączonym
        strażniku (dry-run pominięty).
    :param confirmed: klient potwierdził wysoki koszt (``control.confirm_high_cost`` /
        ``--yes``); znaczenie ma wyłącznie w trybie ``require_confirmation``.
    :raises ValueError: ujemna liczba bajtów albo ``None`` przy włączonym strażniku.
    """
    if bytes_processed is not None and bytes_processed < 0:
        raise ValueError(f"bytes_processed must be >= 0, got {bytes_processed}")
    if bytes_processed is None and config.enabled:
        raise ValueError("bytes_processed (dry-run) is required when the cost guard is enabled")

    cost: float | None = None
    exceeded_limits: tuple[LimitName, ...] = ()
    if bytes_processed is not None:
        cost = estimated_cost_usd(bytes_processed, config)
        exceeded_limits = _exceeded_limits(bytes_processed, cost, config)
    exceeded = bool(exceeded_limits)

    action: CostAction
    maximum_bytes_billed: int | None
    if not config.enabled:
        action, maximum_bytes_billed = "allow", None
    elif not exceeded:
        action, maximum_bytes_billed = "allow", effective_limit_bytes(config)
    elif config.action_on_exceed == "raise_error":
        action, maximum_bytes_billed = "reject", None
    elif config.action_on_exceed == "require_confirmation" and not confirmed:
        action, maximum_bytes_billed = "require_confirmation", None
    else:  # require_confirmation + potwierdzenie albo silent_allow
        assert bytes_processed is not None  # exceeded => bajty znane
        action, maximum_bytes_billed = "allow_over_limit", over_limit_bytes(bytes_processed)

    return CostDecision(
        bytes_processed=bytes_processed,
        estimated_cost_usd=cost,
        exceeded=exceeded,
        exceeded_limits=exceeded_limits,
        action=action,
        maximum_bytes_billed=maximum_bytes_billed,
        guard_enabled=config.enabled,
        max_bytes_per_query=config.max_bytes_per_query,
        max_cost_usd_per_query=config.max_cost_usd_per_query,
    )


def _exceeded_limits(
    bytes_processed: int, cost: float, config: CostGuardConfig
) -> tuple[LimitName, ...]:
    exceeded: list[LimitName] = []
    if bytes_processed > config.max_bytes_per_query:
        exceeded.append("max_bytes_per_query")
    if cost > config.max_cost_usd_per_query:
        exceeded.append("max_cost_usd_per_query")
    return tuple(exceeded)


# --------------------------------------------------------------------------- #
# Wyjątki (mapowanie na HTTP: app/api/errors.py – krok B2)
# --------------------------------------------------------------------------- #


class CostGuardException(Exception):
    """Bazowy wyjątek strażnika kosztów: zapytanie zablokowane przed wykonaniem."""

    def __init__(self, decision: CostDecision, sql: str) -> None:
        self.decision = decision
        self.sql = sql
        super().__init__(
            f"{self._summary} (action={decision.action}): bytes_processed="
            f"{decision.bytes_processed}, estimated_cost_usd={decision.estimated_cost_usd:.6f}, "
            f"exceeded={list(decision.exceeded_limits)}, limits={decision.limits}"
        )

    _summary = "Query blocked by cost guard"

    @property
    def bytes_processed(self) -> int:
        assert self.decision.bytes_processed is not None
        return self.decision.bytes_processed

    @property
    def estimated_cost_usd(self) -> float:
        assert self.decision.estimated_cost_usd is not None
        return self.decision.estimated_cost_usd

    @property
    def limits(self) -> dict[str, int | float]:
        return self.decision.limits

    @property
    def exceeded_limits(self) -> tuple[LimitName, ...]:
        return self.decision.exceeded_limits


class CostTooHighForQueryException(CostGuardException):
    """Limit przekroczony w trybie ``raise_error`` (HTTP 403); potwierdzenie nie pomaga."""

    _summary = "Query cost exceeds the configured limit"


class CostConfirmationRequiredException(CostGuardException):
    """Limit przekroczony w trybie ``require_confirmation`` bez potwierdzenia (HTTP 409);
    klient może ponowić żądanie z ``confirm_high_cost: true``."""

    _summary = "Query cost exceeds the configured limit; confirmation required"


def enforce(decision: CostDecision, sql: str) -> None:
    """Zgłasza wyjątek dla decyzji blokującej; dla ``allow``/``allow_over_limit`` nic nie robi.

    :raises CostTooHighForQueryException: ``action == "reject"``.
    :raises CostConfirmationRequiredException: ``action == "require_confirmation"``.
    """
    if decision.action == "reject":
        raise CostTooHighForQueryException(decision, sql)
    if decision.action == "require_confirmation":
        raise CostConfirmationRequiredException(decision, sql)
