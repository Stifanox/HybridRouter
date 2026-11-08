"""Ustawienia (``HYBRID_ROUTER_*``), budowa stanu aplikacji i skrypt seedujący cache."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config.errors import ConfigValidationError
from app.config.loader import load_model_file
from app.config.settings import Settings
from app.main import build_service, create_app
from scripts.seed_synthetic_cache import main as seed_main
from tests.api.conftest import CONFIG_DIR
from tests.conftest import EXAMPLE_REQUEST, MODEL_NAME, NYC_TAXI_YAML


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYBRID_ROUTER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HYBRID_ROUTER_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("HYBRID_ROUTER_INDEX_CHECK_INTERVAL", "2.5")
    monkeypatch.setenv("HYBRID_ROUTER_DUCKDB_THREADS", "2")
    monkeypatch.setenv("HYBRID_ROUTER_BIGQUERY_BACKEND", "fake")
    monkeypatch.setenv("HYBRID_ROUTER_THREADPOOL_SIZE", "8")
    monkeypatch.setenv("HYBRID_ROUTER_FAKE_RAW_PATH", str(tmp_path / "raw.parquet"))
    settings = Settings()
    assert settings.config_dir == tmp_path / "cfg"
    assert settings.data_root == tmp_path / "data"
    assert settings.index_check_interval == 2.5
    assert settings.duckdb_threads == 2
    assert settings.bigquery_backend == "fake"
    assert settings.threadpool_size == 8
    assert settings.fake_raw_path == tmp_path / "raw.parquet"


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CONFIG_DIR",
        "DATA_ROOT",
        "INDEX_CHECK_INTERVAL",
        "DUCKDB_THREADS",
        "BIGQUERY_BACKEND",
        "THREADPOOL_SIZE",
        "FAKE_RAW_PATH",
    ):
        monkeypatch.delenv(f"HYBRID_ROUTER_{name}", raising=False)
    settings = Settings()
    assert settings.config_dir == Path("configs")
    assert settings.data_root is None
    assert settings.index_check_interval == 0.0
    assert settings.duckdb_threads is None
    assert settings.bigquery_backend == "disabled"
    assert settings.threadpool_size == 40
    assert settings.fake_raw_path is None


@pytest.mark.parametrize(
    ("name", "value"),
    [("BIGQUERY_BACKEND", "emulator"), ("THREADPOOL_SIZE", "0"), ("THREADPOOL_SIZE", "-3")],
)
def test_settings_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(f"HYBRID_ROUTER_{name}", value)
    with pytest.raises(ValidationError):
        Settings()


def test_cache_path_respects_data_root(tmp_path: Path) -> None:
    config = load_model_file(NYC_TAXI_YAML)
    assert Settings().cache_path(config) == Path("./data/nyc_taxi")
    assert Settings(data_root=tmp_path).cache_path(config) == tmp_path / "nyc_taxi"


def test_build_service(tmp_path: Path) -> None:
    service = build_service(Settings(config_dir=CONFIG_DIR, data_root=tmp_path, duckdb_threads=1))
    try:
        assert list(service.models) == [MODEL_NAME]
        index = service.indexes[MODEL_NAME]
        assert index.base_path == tmp_path / "nyc_taxi"
        assert index.partition_key == "pickup_date"
    finally:
        service.local_executor.close()


def test_empty_config_dir_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match=r"no \*\.yaml models"):
        build_service(Settings(config_dir=tmp_path))


def test_shared_cache_directory_fails(tmp_path: Path) -> None:
    shutil.copy(NYC_TAXI_YAML, tmp_path / "a.yaml")
    text = NYC_TAXI_YAML.read_text(encoding="utf-8").replace(MODEL_NAME, "second_model")
    (tmp_path / "b.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="already used by model"):
        build_service(Settings(config_dir=tmp_path))


def test_invalid_config_prevents_startup(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("model_name: x\n", encoding="utf-8")
    app = create_app(Settings(config_dir=tmp_path))
    with pytest.raises(ConfigValidationError), TestClient(app):
        pass


def test_seed_script_then_example_query(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Skrypt seedujący + przykład z dok. 03, sekcja 4 (zakres 2022-12-01…23, auto)."""
    assert seed_main(["--data-root", str(tmp_path), "--rows-per-day", "20"]) == 0
    output = capsys.readouterr().out
    assert "31 partition(s)" in output
    manifest = json.loads((tmp_path / "nyc_taxi" / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["partitions"]) == 31

    settings = Settings(config_dir=CONFIG_DIR, data_root=tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/query", json=EXAMPLE_REQUEST)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["telemetry"]["engine_used"] == "duckdb"
    assert payload["telemetry"]["routing_mode"] == "auto"
    assert {row["pickup_date"] for row in payload["data"]} == {
        f"2022-12-{d:02d}" for d in range(1, 24)
    }


def test_seed_script_empty_day_and_reseed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--data-root", str(tmp_path), "--start-date", "2022-12-01", "--end-date", "2022-12-03"]
    assert seed_main([*args, "--rows-per-day", "5", "--empty-day", "2022-12-02"]) == 0
    assert "days without data (not cached): 2022-12-02" in capsys.readouterr().out
    assert seed_main([*args, "--rows-per-day", "5"]) == 0  # nowa wersja manifestu
    manifest = json.loads((tmp_path / "nyc_taxi" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert len(manifest["partitions"]) == 3


@pytest.mark.parametrize(
    "args",
    [
        ["--start-date", "2022-12-05", "--end-date", "2022-12-01"],
        ["--rows-per-day", "2"],
        ["--empty-day", "2023-01-01"],
    ],
)
def test_seed_script_rejects_bad_arguments(args: list[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        seed_main(["--data-root", str(tmp_path), *args])
    assert excinfo.value.code == 2
