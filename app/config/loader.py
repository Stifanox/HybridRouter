"""Ładowanie i walidacja strukturalna plików konfiguracyjnych modeli semantycznych.

Potok walidacji jednego pliku:

1. odczyt YAML (z wykrywaniem zduplikowanych kluczy, których PyYAML domyślnie nie zgłasza),
2. walidacja pól modelami Pydantic (``app.config.schemas``),
3. walidacja strukturalna – reguły międzypolowe (``STRUCTURAL_VALIDATORS``),
4. walidacja semantyczna (``SEMANTIC_VALIDATORS``, ``app.semantic.validator`` – rejestr
   agregacji, parser formuł, rozwijanie zależności).

Każdy błąd na dowolnym etapie zgłaszany jest jednym typem wyjątku –
:class:`ConfigValidationError` – ze wskazaniem pliku i ścieżki pola.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError

from app.config.errors import ConfigIssue, ConfigValidationError
from app.config.schemas import ModelConfig
from app.semantic.validator import validate_semantics

__all__ = [
    "SEMANTIC_VALIDATORS",
    "STRUCTURAL_VALIDATORS",
    "ConfigIssue",
    "ConfigValidationError",
    "ModelRegistry",
    "ModelValidator",
    "load_model_dict",
    "load_model_file",
    "load_models_dir",
]

ModelValidator = Callable[[ModelConfig], Iterable[ConfigIssue]]
"""Walidator modelu: zwraca (lub generuje) listę problemów; pusta lista = model poprawny."""

ModelRegistry = Mapping[str, ModelConfig]
"""Niemutowalny rejestr modeli kluczowany ``model_name``."""


# --------------------------------------------------------------------------- #
# Odczyt YAML z wykrywaniem duplikatów kluczy
# --------------------------------------------------------------------------- #


class _DuplicateKeyError(Exception):
    def __init__(self, key: object, line: int) -> None:
        super().__init__(f"duplicate key '{key}' (line {line})")
        self.key = key
        self.line = line


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """``SafeLoader``, który odrzuca zduplikowane klucze mapowań.

    Standardowy PyYAML po cichu nadpisuje wcześniejszą wartość, co dla sekcji
    ``dimensions`` / ``base_measures`` / ``derived_metrics`` oznaczałoby utratę definicji.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise _DuplicateKeyError(key, key_node.start_mark.line + 1)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigValidationError(path, [ConfigIssue("", f"cannot read file: {exc}")]) from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)  # SafeLoader subclass – bezpieczny
    except _DuplicateKeyError as exc:
        raise ConfigValidationError(path, [ConfigIssue("", str(exc))]) from exc
    except yaml.YAMLError as exc:
        raise ConfigValidationError(path, [ConfigIssue("", f"invalid YAML: {exc}")]) from exc
    if not isinstance(data, dict):
        raise ConfigValidationError(
            path, [ConfigIssue("", "top-level YAML element must be a mapping")]
        )
    return data


# --------------------------------------------------------------------------- #
# Walidacja strukturalna (reguły międzypolowe)
# --------------------------------------------------------------------------- #

_SECTIONS: tuple[str, ...] = ("dimensions", "base_measures", "derived_metrics")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _named_items(model: ModelConfig) -> Iterator[tuple[str, str]]:
    """(sekcja, nazwa) dla wszystkich wymiarów, miar bazowych i metryk pochodnych."""
    for section in _SECTIONS:
        for name in getattr(model, section):
            yield section, name


def check_identifiers(model: ModelConfig) -> Iterator[ConfigIssue]:
    """Nazwy są identyfikatorami: trafiają do SQL jako aliasy i do formuł jako nazwy."""
    for section, name in _named_items(model):
        if not _IDENTIFIER_RE.fullmatch(name):
            yield ConfigIssue(
                f"{section}.{name}",
                "name must be an identifier matching [A-Za-z_][A-Za-z0-9_]*",
            )


def check_unique_names(model: ModelConfig) -> Iterator[ConfigIssue]:
    """Nazwy unikalne w obrębie i pomiędzy sekcjami (bez rozróżniania wielkości liter).

    Identyfikatory w BigQuery i DuckDB nie rozróżniają wielkości liter, więc
    ``trips_count`` i ``Trips_Count`` dałyby w SQL kolidujące aliasy.
    """
    first_seen: dict[str, str] = {}
    for section, name in _named_items(model):
        key = name.casefold()
        here = f"{section}.{name}"
        if key in first_seen:
            yield ConfigIssue(here, f"name conflicts with '{first_seen[key]}'")
        else:
            first_seen[key] = here


def check_partition_key(model: ModelConfig) -> Iterator[ConfigIssue]:
    """``local_cache.partition_key`` wskazuje wymiar typu ``date`` z ``in_rollup: true``."""
    path = "storage.local_cache.partition_key"
    key = model.storage.local_cache.partition_key
    dimension = model.dimensions.get(key)
    if dimension is None:
        yield ConfigIssue(path, f"'{key}' is not a defined dimension")
        return
    if dimension.type != "date":
        yield ConfigIssue(path, f"dimension '{key}' must have type 'date', got '{dimension.type}'")
    if not dimension.in_rollup:
        yield ConfigIssue(path, f"dimension '{key}' must have in_rollup: true")


def check_date_window(model: ModelConfig) -> Iterator[ConfigIssue]:
    """``date_window_start <= date_window_end``."""
    cache = model.storage.local_cache
    if cache.date_window_start > cache.date_window_end:
        yield ConfigIssue(
            "storage.local_cache.date_window_start",
            f"date_window_start ({cache.date_window_start}) must not be later than "
            f"date_window_end ({cache.date_window_end})",
        )


STRUCTURAL_VALIDATORS: tuple[ModelValidator, ...] = (
    check_identifiers,
    check_unique_names,
    check_partition_key,
    check_date_window,
)
"""Reguły strukturalne. Pola pojedyncze (Literal, dodatniość ``cost_guard``,
niepuste ``aggregation``) sprawdzają modele Pydantic."""

SEMANTIC_VALIDATORS: tuple[ModelValidator, ...] = (validate_semantics,)
"""Walidatory semantyczne (etap A2), uruchamiane wyłącznie dla modeli poprawnych
strukturalnie: zgodność ``aggregation`` z rejestrem ``AGGREGATIONS``, parsowanie formuł
(biała lista), rozwijanie zależności, wykrywanie cykli i nieznanych nazw."""


# --------------------------------------------------------------------------- #
# API publiczne
# --------------------------------------------------------------------------- #


def _pydantic_issues(error: ValidationError) -> list[ConfigIssue]:
    return [
        ConfigIssue(".".join(str(part) for part in err["loc"]), err["msg"])
        for err in error.errors()
    ]


def _run_validators(model: ModelConfig, validators: Sequence[ModelValidator]) -> list[ConfigIssue]:
    return [issue for validator in validators for issue in validator(model)]


def load_model_dict(
    data: Mapping[str, Any],
    source: str | Path = "<dict>",
    *,
    semantic_validators: Sequence[ModelValidator] | None = None,
) -> ModelConfig:
    """Waliduje słownik (np. wczytany z YAML) i zwraca ``ModelConfig``.

    ``semantic_validators=None`` oznacza użycie ``SEMANTIC_VALIDATORS``.
    """
    try:
        model = ModelConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigValidationError(source, _pydantic_issues(exc)) from exc

    issues = _run_validators(model, STRUCTURAL_VALIDATORS)
    if issues:
        raise ConfigValidationError(source, issues)

    validators = SEMANTIC_VALIDATORS if semantic_validators is None else semantic_validators
    issues = _run_validators(model, validators)
    if issues:
        raise ConfigValidationError(source, issues)
    return model


def load_model_file(
    path: str | Path,
    *,
    semantic_validators: Sequence[ModelValidator] | None = None,
) -> ModelConfig:
    """Wczytuje i waliduje pojedynczy plik YAML modelu."""
    path = Path(path)
    return load_model_dict(_read_yaml(path), source=path, semantic_validators=semantic_validators)


def load_models_dir(
    directory: str | Path,
    *,
    semantic_validators: Sequence[ModelValidator] | None = None,
) -> ModelRegistry:
    """Wczytuje wszystkie ``*.yaml`` z katalogu do rejestru kluczowanego ``model_name``.

    Duplikat ``model_name`` w dwóch plikach jest błędem.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ConfigValidationError(directory, [ConfigIssue("", "not a directory")])

    registry: dict[str, ModelConfig] = {}
    origin: dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        model = load_model_file(path, semantic_validators=semantic_validators)
        name = model.model_name
        if name in origin:
            raise ConfigValidationError(
                path,
                [
                    ConfigIssue(
                        "model_name",
                        f"duplicate model_name '{name}' (already defined in '{origin[name]}')",
                    )
                ],
            )
        registry[name] = model
        origin[name] = path
    return MappingProxyType(registry)
