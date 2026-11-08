"""Granice zależności fazy 2: izolacja biblioteki Google (ADR-014) i marker ``bigquery``."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.conftest import BQ_INTEGRATION_ENV, bigquery_tests_enabled, pytest_collection_modifyitems

APP = Path(__file__).resolve().parents[2] / "app"
GOOGLE_MODULE = APP / "warehouse" / "google_gateway.py"


def _imports_google(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name == "google" or name.startswith("google.") for name in names):
            return True
    return False


def test_only_google_gateway_imports_google_libraries() -> None:
    offenders = [
        str(path.relative_to(APP))
        for path in sorted(APP.rglob("*.py"))
        if path != GOOGLE_MODULE and _imports_google(path)
    ]
    assert offenders == []
    assert _imports_google(GOOGLE_MODULE)


@pytest.mark.parametrize(
    ("environ", "enabled"),
    [({}, False), ({BQ_INTEGRATION_ENV: "0"}, False), ({BQ_INTEGRATION_ENV: "1"}, True)],
)
def test_bigquery_tests_enabled(environ: dict[str, str], enabled: bool) -> None:
    assert bigquery_tests_enabled(environ) is enabled


def _item(marked: bool) -> MagicMock:
    item = MagicMock(spec=pytest.Item)
    item.get_closest_marker.return_value = object() if marked else None
    return item


def test_bigquery_marked_tests_are_skipped_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BQ_INTEGRATION_ENV, raising=False)
    marked, plain = _item(True), _item(False)
    pytest_collection_modifyitems(MagicMock(), [marked, plain])
    (skip,), _ = marked.add_marker.call_args
    assert skip.mark.name == "skip"
    assert BQ_INTEGRATION_ENV in skip.mark.kwargs["reason"]
    plain.add_marker.assert_not_called()


def test_bigquery_marked_tests_run_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BQ_INTEGRATION_ENV, "1")
    marked = _item(True)
    pytest_collection_modifyitems(MagicMock(), [marked])
    marked.add_marker.assert_not_called()


@pytest.mark.bigquery
def test_marker_is_registered_and_skipped_by_default() -> None:
    """Pusty test z markerem – przy domyślnym uruchomieniu musi zostać pominięty."""
    assert bigquery_tests_enabled()
