"""Typy błędów warstwy konfiguracji.

Wydzielone z ``app.config.loader``, aby walidatory semantyczne (``app.semantic.validator``)
mogły zwracać :class:`ConfigIssue` bez importowania loadera – loader importuje walidator
semantyczny, więc odwrotny import tworzyłby cykl.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """Pojedynczy problem w konfiguracji: ścieżka pola (np. ``storage.local_cache``) i opis."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class ConfigValidationError(Exception):
    """Jedyny typ błędu zgłaszany przez warstwę konfiguracji.

    ``source`` wskazuje plik (lub katalog), ``issues`` – wszystkie wykryte problemy.
    """

    def __init__(self, source: str | Path, issues: Iterable[ConfigIssue]) -> None:
        self.source = str(source)
        self.issues: tuple[ConfigIssue, ...] = tuple(issues)
        lines = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"Invalid configuration in '{self.source}':\n{lines}")
