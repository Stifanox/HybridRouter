"""Indeks dostępnych partycji rollupu (dok. 03, Moduł 2 – Warunek 2).

Źródłem prawdy jest ``{base_path}/manifest.json`` (``app.sync.manifest``). Indeks
przechowuje **niemutowalną migawkę** (:class:`PartitionSnapshot`): wersję manifestu i mapę
dzień → plik. Zapytanie pobiera migawkę **raz** (:meth:`PartitionIndex.snapshot`) i używa
jej zarówno do oceny Warunku 2, jak i do listy plików w ``read_parquet([...])`` – późniejsza
publikacja nie zmienia ani decyzji, ani plików trwającego zapytania (pliki wycofane są
usuwane dopiero po okresie karencji, ``app.sync.publisher``).

Odświeżanie: :meth:`PartitionIndex.snapshot` sprawdza manifest co najwyżej raz na
``check_interval`` sekund (``0`` – przy każdym zapytaniu). Sprawdzenie to jedno wywołanie
``stat``: manifest jest podmieniany przez ``os.replace``, więc każda publikacja zmienia jego
i-węzeł; plik jest wczytywany ponownie tylko wtedy, gdy ``(st_ino, st_mtime_ns, st_size)``
się zmieniło. Nowa migawka zastępuje starą jednym przypisaniem referencji – migawki już
pobrane przez zapytania pozostają nietknięte.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from app.config.schemas import LocalCacheConfig
from app.planner.logical_plan import DateRange
from app.sync.manifest import manifest_path, read_manifest

logger = logging.getLogger(__name__)

_FileSignature = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class PartitionSnapshot:
    """Stan cache z jednej wersji manifestu (niemutowalny)."""

    base_path: Path
    partition_key: str
    version: int = 0
    files: Mapping[dt.date, Path] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def days(self) -> frozenset[dt.date]:
        return frozenset(self.files)

    def __contains__(self, day: object) -> bool:
        return day in self.files

    def __len__(self) -> int:
        return len(self.files)

    def files_for(self, date_range: DateRange | None) -> list[str]:
        """Ścieżki plików dni z ``date_range`` (wszystkie przy ``None``), rosnąco po dniu –
        lista dla ``RollupRenderer``."""
        days = sorted(self.files)
        if date_range is not None:
            start, end = date_range
            days = [day for day in days if start <= day <= end]
        return [self.files[day].as_posix() for day in days]

    def missing_days(
        self, start: dt.date, end: dt.date, *, limit: int | None = None
    ) -> list[dt.date]:
        """Dni z domkniętego przedziału ``[start, end]`` bez partycji, rosnąco.

        :param limit: maksymalna liczba zwracanych dni (``None`` – wszystkie). Pętla kończy
            się po znalezieniu ``limit`` dni, więc koszt nie zależy od długości zakresu,
            gdy brakuje wielu dni (np. zakres wieloletni na pustym cache).
        :raises ValueError: ``start > end`` lub ``limit < 0``.
        """
        _check_range(start, end)
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")
        missing: list[dt.date] = []
        day = start
        while day <= end and (limit is None or len(missing) < limit):
            if day not in self.files:
                missing.append(day)
            if day == dt.date.max:
                break
            day += dt.timedelta(days=1)
        return missing

    def count_missing(self, start: dt.date, end: dt.date) -> int:
        """Liczba dni bez partycji w ``[start, end]`` (bez iteracji po całym zakresie)."""
        _check_range(start, end)
        present = sum(1 for day in self.files if start <= day <= end)
        return (end - start).days + 1 - present


class PartitionIndex:
    """Źródło migawek cache jednego modelu."""

    def __init__(
        self,
        base_path: str | Path,
        partition_key: str,
        *,
        check_interval: float = 0.0,
        scan: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """:param check_interval: minimalny odstęp (s) między sprawdzeniami manifestu w
            :meth:`snapshot`; ``0`` – sprawdzenie (``stat``) przy każdym wywołaniu.
        :param scan: ``True`` – wczytaj manifest od razu (stan przy starcie aplikacji)."""
        if check_interval < 0:
            raise ValueError("check_interval must not be negative")
        self.base_path = Path(base_path)
        self.partition_key = partition_key
        self.check_interval = check_interval
        self._monotonic = monotonic
        self._snapshot = PartitionSnapshot(self.base_path, partition_key)
        self._signature: _FileSignature | None = None
        self._loaded = False
        self._checked_at: float | None = None
        self._lock = threading.Lock()
        if scan:
            self.refresh()

    @classmethod
    def for_cache(
        cls, cache: LocalCacheConfig, *, check_interval: float = 0.0, scan: bool = True
    ) -> PartitionIndex:
        """Indeks dla sekcji ``storage.local_cache`` modelu."""
        return cls(cache.base_path, cache.partition_key, check_interval=check_interval, scan=scan)

    @property
    def current(self) -> PartitionSnapshot:
        """Ostatnio wczytana migawka (bez sprawdzania manifestu)."""
        return self._snapshot

    def snapshot(self) -> PartitionSnapshot:
        """Migawka dla jednego zapytania; sprawdza manifest, jeśli od ostatniego sprawdzenia
        minęło co najmniej ``check_interval`` sekund."""
        checked = self._checked_at
        if checked is None or self._monotonic() - checked >= self.check_interval:
            return self.refresh()
        return self._snapshot

    def refresh(self) -> PartitionSnapshot:
        """Sprawdza manifest i – jeśli się zmienił – wczytuje nową migawkę.

        :raises ManifestError: manifest uszkodzony (poprzednia migawka pozostaje aktywna).
        """
        with self._lock:
            self._checked_at = self._monotonic()
            signature = _signature(manifest_path(self.base_path))
            if self._loaded and signature == self._signature:
                return self._snapshot
            self._snapshot = self._load()
            self._signature = signature
            self._loaded = True
            return self._snapshot

    def _load(self) -> PartitionSnapshot:
        manifest = read_manifest(self.base_path, self.partition_key)
        if manifest is None:
            return PartitionSnapshot(self.base_path, self.partition_key)
        files: dict[dt.date, Path] = {}
        for day, entry in manifest.partitions.items():
            path = self.base_path / entry.file
            if path.is_file():
                files[day] = path
            else:  # plik usunięty poza publikacją – dzień traktowany jako brakujący
                logger.warning("manifest v%d references missing file %s", manifest.version, path)
        return PartitionSnapshot(
            self.base_path, self.partition_key, manifest.version, MappingProxyType(files)
        )


def _signature(path: Path) -> _FileSignature | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _check_range(start: dt.date, end: dt.date) -> None:
    if start > end:
        raise ValueError(f"empty date range: {start.isoformat()} > {end.isoformat()}")
