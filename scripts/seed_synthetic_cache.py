"""Zasiewa lokalny cache syntetycznymi rollupami (do ręcznych prób API bez GCP).

Generuje deterministyczne surowe przejazdy (``tests/fixtures/synthetic.py``, ADR-012),
buduje z nich dzienne rollupy tym samym zapytaniem co SyncWorker i publikuje je jako nową
wersję ``manifest.json`` (ADR-005). Domyślny zakres 2022-12-01…2022-12-31 pokrywa
przykład z dok. 03, sekcja 4 (2022-12-01…2022-12-23), i nie zawiera dni bez danych.

Użycie::

    poetry run python scripts/seed_synthetic_cache.py
    poetry run python scripts/seed_synthetic_cache.py --rows-per-day 5000 --empty-day 2022-12-10
    poetry run python scripts/seed_synthetic_cache.py --data-root /tmp/hr-data  # = DATA_ROOT
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

# Generator danych syntetycznych leży w pakiecie testów (tests/fixtures) – skrypt uruchamiany
# jako plik potrzebuje katalogu projektu na ścieżce importu.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config.loader import load_model_file  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.sync.local_builder import build_local_rollups  # noqa: E402
from tests.fixtures.synthetic import DEFAULT_SEED, generate_trips  # noqa: E402

DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "nyc_taxi.yaml"
DEFAULT_START = dt.date(2022, 12, 1)
DEFAULT_END = dt.date(2022, 12, 31)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="plik YAML modelu")
    parser.add_argument("--start-date", type=dt.date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-date", type=dt.date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--rows-per-day", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--empty-day",
        type=dt.date.fromisoformat,
        action="append",
        default=[],
        help="dzień bez danych (dziura w cache); można powtórzyć",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="katalog nadrzędny cache (jak HYBRID_ROUTER_DATA_ROOT); "
        "domyślnie storage.local_cache.base_path z YAML",
    )
    args = parser.parse_args(argv)
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    if args.rows_per_day < 3:
        parser.error("--rows-per-day must be at least 3")
    for day in args.empty_day:
        if not args.start_date <= day <= args.end_date:
            parser.error(f"--empty-day {day} outside of the seeded range")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_model_file(args.config)
    base_path = Settings(data_root=args.data_root).cache_path(config)
    days = (args.end_date - args.start_date).days + 1

    started = time.perf_counter()
    raw = generate_trips(
        days=days,
        rows_per_day=args.rows_per_day,
        start=args.start_date,
        seed=args.seed,
        empty_day_offsets=[(day - args.start_date).days for day in args.empty_day],
    )
    result = build_local_rollups(raw, config, (args.start_date, args.end_date), base_path)
    elapsed = time.perf_counter() - started

    print(
        f"model {config.model_name}: {raw.num_rows} synthetic trips "
        f"({args.start_date}..{args.end_date}) -> {result.result_rows} rollup rows in "
        f"{len(result.partitions)} partition(s), {result.total_bytes / 1024:.1f} KiB"
    )
    print(f"published manifest v{result.version}: {result.publication.manifest_path}")
    written = set(result.days_written)
    empty = [
        day.isoformat()
        for offset in range(days)
        if (day := args.start_date + dt.timedelta(days=offset)).isoformat() not in written
    ]
    if empty:
        print(f"days without data (not cached): {', '.join(empty)}")
    print(f"done in {elapsed:.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
