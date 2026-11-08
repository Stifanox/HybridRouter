"""Manifest lokalnego cache – jedyne źródło prawdy o aktualnych plikach rollupu (ADR-005).

``{base_path}/manifest.json`` mapuje dzień na **niezmienny** plik rollupu. Publikacja nowych
danych (``app.sync.publisher``) najpierw zapisuje nowe pliki, a na końcu jednym
``os.replace`` podmienia manifest – to jest moment, w którym nowe dane stają się widoczne.
Wszystkie dni jednej publikacji przełączają się jednocześnie.

Manifest przechowuje także:

* ``schema`` – kolumny i typy plików rollupu; zmiana schematu (np. nowa miara w YAML)
  unieważnia dni spoza zakresu publikacji (zapytanie nie może łączyć plików o różnych
  schematach),
* ``retired`` – pliki wycofane z manifestu wraz z czasem wycofania; plik jest usuwany
  dopiero po okresie karencji (zapytania mogą go jeszcze czytać ze starszej migawki).

Format (``format_version = 1``)::

    {"format_version": 1, "version": 42, "partition_key": "pickup_date",
     "published_at": "2026-11-08T12:00:00+00:00",
     "schema": [["vendor_id", "string"], ...],
     "partitions": {"2022-12-01": {"file": "pickup_date=2022-12-01/rollup-00000042.parquet",
                                   "rows": 32, "size_bytes": 4412}},
     "retired": [{"file": "pickup_date=2022-12-01/rollup-00000041.parquet",
                  "retired_at": "2026-11-08T12:00:00+00:00"}]}

Ścieżki plików są względne wobec ``base_path`` (katalog cache można przenieść).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import pyarrow as pa

from app.sync.atomic_writer import atomic_write, fsync_dir

MANIFEST_FILE_NAME = "manifest.json"
FORMAT_VERSION = 1

SchemaFingerprint = tuple[tuple[str, str], ...]


class ManifestError(Exception):
    """Manifest nie daje się odczytać lub nie pasuje do modelu."""


@dataclass(frozen=True, slots=True)
class PartitionEntry:
    """Aktualny plik dnia (ścieżka względna wobec ``base_path``)."""

    file: str
    rows: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RetiredFile:
    """Plik wycofany z manifestu – do usunięcia po okresie karencji."""

    file: str
    retired_at: dt.datetime


@dataclass(frozen=True, slots=True)
class Manifest:
    """Niemutowalny stan opublikowanego cache."""

    version: int
    partition_key: str
    published_at: dt.datetime | None = None
    schema: SchemaFingerprint = ()
    partitions: Mapping[dt.date, PartitionEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )
    retired: tuple[RetiredFile, ...] = ()

    @classmethod
    def empty(cls, partition_key: str) -> Manifest:
        """Stan przed pierwszą publikacją (wersja 0)."""
        return cls(version=0, partition_key=partition_key)

    @property
    def referenced_files(self) -> frozenset[str]:
        """Pliki, których nie wolno usuwać: aktualne i wycofane w okresie karencji."""
        return frozenset(e.file for e in self.partitions.values()) | frozenset(
            r.file for r in self.retired
        )

    # -- serializacja --------------------------------------------------------- #

    def to_json(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "version": self.version,
            "partition_key": self.partition_key,
            "published_at": None if self.published_at is None else self.published_at.isoformat(),
            "schema": [list(column) for column in self.schema],
            "partitions": {
                day.isoformat(): {"file": e.file, "rows": e.rows, "size_bytes": e.size_bytes}
                for day, e in sorted(self.partitions.items())
            },
            "retired": [
                {"file": r.file, "retired_at": r.retired_at.isoformat()} for r in self.retired
            ],
        }

    @classmethod
    def from_json(cls, data: Any) -> Manifest:
        """:raises ManifestError: niepoprawna struktura lub wartości."""
        try:
            if data["format_version"] != FORMAT_VERSION:
                raise ManifestError(f"unsupported format_version {data['format_version']!r}")
            version = data["version"]
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                raise ManifestError(f"invalid version {version!r}")
            published = data["published_at"]
            partitions = {
                dt.date.fromisoformat(day): PartitionEntry(
                    _relative(entry["file"]), int(entry["rows"]), int(entry["size_bytes"])
                )
                for day, entry in data["partitions"].items()
            }
            return cls(
                version=version,
                partition_key=str(data["partition_key"]),
                published_at=None if published is None else _aware(published),
                schema=tuple((str(name), str(type_)) for name, type_ in data["schema"]),
                partitions=MappingProxyType(partitions),
                retired=tuple(
                    RetiredFile(_relative(r["file"]), _aware(r["retired_at"]))
                    for r in data["retired"]
                ),
            )
        except ManifestError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ManifestError(f"invalid manifest structure: {exc!r}") from exc


def manifest_path(base_path: str | Path) -> Path:
    return Path(base_path) / MANIFEST_FILE_NAME


def read_manifest(base_path: str | Path, partition_key: str) -> Manifest | None:
    """Manifest z katalogu cache albo ``None``, gdy cache nie był jeszcze publikowany.

    :raises ManifestError: plik uszkodzony lub dotyczy innego klucza partycji.
    """
    path = manifest_path(base_path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        manifest = Manifest.from_json(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: invalid JSON ({exc})") from exc
    except ManifestError as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    if manifest.partition_key != partition_key:
        raise ManifestError(
            f"{path}: partition key '{manifest.partition_key}' does not match '{partition_key}'"
        )
    return manifest


def write_manifest(base_path: str | Path, manifest: Manifest) -> Path:
    """Atomowo podmienia manifest (``.tmp`` + ``fsync`` + ``os.replace`` + ``fsync`` katalogu)."""
    path = manifest_path(base_path)
    payload = json.dumps(manifest.to_json(), indent=2, ensure_ascii=False).encode("utf-8")
    atomic_write(path, lambda handle: handle.write(payload))
    fsync_dir(path.parent)
    return path


def schema_fingerprint(schema: pa.Schema) -> SchemaFingerprint:
    """Kolumny i typy Arrow – porównywane przy publikacji (zmiana schematu rollupu)."""
    return tuple((f.name, str(f.type)) for f in schema)


def _relative(file: Any) -> str:
    path = PurePosixPath(str(file))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ManifestError(f"manifest file path must be relative to base_path: {file!r}")
    return path.as_posix()


def _aware(text: Any) -> dt.datetime:
    value = dt.datetime.fromisoformat(str(text))
    if value.tzinfo is None:
        raise ManifestError(f"timestamp without time zone: {text!r}")
    return value
