"""Punkt wejścia FastAPI (ADR-001): ``poetry run uvicorn app.main:app``.

``lifespan`` przy starcie:

1. wczytuje i waliduje modele z ``Settings.config_dir`` (błąd konfiguracji = serwis nie
   startuje – ``ConfigValidationError`` z listą problemów w logu),
2. buduje model semantyczny każdego modelu (rozwinięte AST metryk, raz na proces),
3. tworzy indeks partycji (migawka ``manifest.json``) dla katalogu cache każdego modelu
   (``Settings.cache_path`` – z uwzględnieniem ``HYBRID_ROUTER_DATA_ROOT``),
4. otwiera współdzielone połączenie DuckDB (``DuckDBExecutor``).

Przy zamykaniu – zamyka połączenie DuckDB.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.routes import router
from app.api.service import QueryService
from app.config.errors import ConfigIssue, ConfigValidationError
from app.config.loader import load_models_dir
from app.config.settings import Settings
from app.executors.duckdb_executor import ConfigValue, DuckDBExecutor
from app.semantic.resolver import build_semantic_model
from app.sync.partition_index import PartitionIndex

logger = logging.getLogger(__name__)


def build_service(settings: Settings) -> QueryService:
    """Stan aplikacji z ustawień (bez serwera HTTP – używane też w testach i skryptach)."""
    configs = load_models_dir(settings.config_dir)
    if not configs:
        raise ConfigValidationError(settings.config_dir, [ConfigIssue("", "no *.yaml models")])

    models = {name: build_semantic_model(config) for name, config in configs.items()}
    indexes: dict[str, PartitionIndex] = {}
    owners: dict[Path, str] = {}
    for name, config in configs.items():
        path = settings.cache_path(config)
        if path in owners:
            raise ConfigValidationError(
                settings.config_dir,
                [
                    ConfigIssue(
                        f"{name}.storage.local_cache.base_path",
                        f"cache directory '{path}' already used by model '{owners[path]}'",
                    )
                ],
            )
        owners[path] = name
        indexes[name] = PartitionIndex(
            path,
            config.storage.local_cache.partition_key,
            check_interval=settings.index_check_interval,
        )
        logger.info(
            "model '%s': cache %s, %d partition(s), manifest v%d",
            name,
            path,
            len(indexes[name].current),
            indexes[name].current.version,
        )

    duckdb_config: dict[str, ConfigValue] = {}
    if settings.duckdb_threads is not None:
        duckdb_config["threads"] = settings.duckdb_threads
    return QueryService(models, indexes, DuckDBExecutor(duckdb_config))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Fabryka aplikacji; ``settings=None`` – ustawienia ze zmiennych środowiskowych."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = build_service(settings or Settings())
        app.state.service = service
        try:
            yield
        finally:
            service.local_executor.close()

    app = FastAPI(
        title="Hybrid OLAP Router",
        version="0.1.0",
        description=(
            "Hybrydowy router zapytań analitycznych z warstwą semantyczną "
            "(faza 1: lokalny cache DuckDB; BigQuery – faza 2)."
        ),
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()
