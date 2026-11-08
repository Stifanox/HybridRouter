"""Ustawienia procesu aplikacji (zmienne środowiskowe ``HYBRID_ROUTER_*``, pydantic-settings).

Ustawienia dotyczą *uruchomienia* serwisu (skąd wczytać modele, gdzie leży cache), a nie
modelu semantycznego – ten opisują pliki YAML w ``config_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.schemas import ModelConfig


class Settings(BaseSettings):
    """Ustawienia serwisu.

    * ``config_dir`` (``HYBRID_ROUTER_CONFIG_DIR``) – katalog z plikami ``*.yaml`` modeli,
    * ``data_root`` (``HYBRID_ROUTER_DATA_ROOT``) – gdy ustawiony, zastępuje katalog
      nadrzędny ``storage.local_cache.base_path`` każdego modelu: cache modelu leży w
      ``{data_root}/{nazwa katalogu z base_path}`` (dla ``./data/nyc_taxi`` –
      ``{data_root}/nyc_taxi``). Pozwala testom i wdrożeniom wskazać inny wolumen bez
      zmiany YAML,
    * ``index_check_interval`` (``HYBRID_ROUTER_INDEX_CHECK_INTERVAL``) – minimalny odstęp
      (s) między sprawdzeniami ``manifest.json`` przez indeks partycji (``0`` – przy każdym
      zapytaniu; ``app.sync.partition_index``),
    * ``duckdb_threads`` (``HYBRID_ROUTER_DUCKDB_THREADS``) – limit wątków DuckDB
      (``None`` – domyślny DuckDB, liczba rdzeni),
    * ``bigquery_backend`` (``HYBRID_ROUTER_BIGQUERY_BACKEND``) – implementacja bramki
      hurtowni (ADR-014, F2-6): ``disabled`` (domyślnie; brak wykonawcy hurtowni, HTTP 501
      jak w fazie 1, aplikacja działa bez GCP), ``fake`` (``FakeBigQueryGateway`` – DuckDB
      na surowych danych syntetycznych z ``fake_raw_path``) albo ``google``
      (``GoogleBigQueryGateway``; poświadczenia wyłącznie z Application Default
      Credentials),
    * ``threadpool_size`` (``HYBRID_ROUTER_THREADPOOL_SIZE``) – rozmiar puli wątków
      obsługujących synchroniczne endpointy (``def``); zapytania do hurtowni blokują wątek
      na czas całego zadania (F2-10),
    * ``fake_raw_path`` (``HYBRID_ROUTER_FAKE_RAW_PATH``) – plik Parquet lub katalog plików
      Parquet z surowymi danymi syntetycznymi (schemat tabeli BigQuery) dla backendu
      ``fake``.
    """

    model_config = SettingsConfigDict(env_prefix="HYBRID_ROUTER_", frozen=True, extra="ignore")

    config_dir: Path = Path("configs")
    data_root: Path | None = None
    index_check_interval: float = Field(default=0.0, ge=0)
    duckdb_threads: int | None = Field(default=None, gt=0)
    bigquery_backend: Literal["disabled", "fake", "google"] = "disabled"
    threadpool_size: int = Field(default=40, gt=0)
    fake_raw_path: Path | None = None

    def cache_path(self, model: ModelConfig) -> Path:
        """Katalog lokalnego cache modelu po uwzględnieniu ``data_root``."""
        base_path = Path(model.storage.local_cache.base_path)
        if self.data_root is None:
            return base_path
        return self.data_root / base_path.name
