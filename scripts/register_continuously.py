#!/usr/bin/env python3
"""Периодическая регистрация новых загрузок журналов без сети и изменения прав."""

from __future__ import annotations

import argparse
import math
import sys

from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts должен находить локальные модули проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.continuous_inventory import load_inventory_status, run_inventory  # noqa: E402
from src.corpus.manifests import ManifestError  # noqa: E402


def _positive_seconds(value: str) -> float:
    """Прочитать конечный положительный интервал между проверками загрузок."""

    try:
        seconds = float(value)

    except ValueError as exception:
        raise argparse.ArgumentTypeError("Ожидается положительное число секунд") from exception

    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Интервал должен быть конечным и больше нуля")

    return seconds


def build_parser() -> argparse.ArgumentParser:
    """Разделить запуск регистрации и просмотр сохранённого состояния."""

    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, help="Каталог состояния внутри manifests/imports")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", parents=[common], help="Проверять и регистрировать новые поступления")
    run.add_argument("--interval", type=_positive_seconds, default=900.0,
                     help="Пауза после каждого прохода, с; по умолчанию 900")
    run.add_argument("--once", action="store_true", help="Выполнить один проход и завершиться")
    run.add_argument("--seed-batch", type=Path, help="Ранее зарегистрированная партия для первого запуска")
    run.add_argument("--seed-receipt", type=Path, help="Квитанция успешной регистрации начальной партии")
    commands.add_parser("status", parents=[common], help="Показать состояние без записи и сетевых запросов")

    return parser


def _print_status(status: dict[str, Any]) -> None:
    """Показать прогресс, не выдавая сохранённое состояние за работающий процесс."""

    print(f"Состояние: {status['state_path']}")

    if not status["state_exists"]:
        print("Автоматическая регистрация ещё не создавала состояние.")

        return

    print(f"Учтено партий: {status['completed_batches']}; версий карточек по текущим путям: {status['seen_cards']}.")
    print("Есть незавершённая партия." if status["pending"] else "Незавершённой партии нет.")

    if status["last_receipt"] is not None:
        print(f"Последняя квитанция: {status['last_receipt']}")

    print("Это сохранённое состояние, не подтверждение работающего процесса. Права и допуск не меняются.")


def main(arguments: list[str] | None = None) -> int:
    """Запустить офлайн-регистрацию либо прочитать её состояние."""

    options = build_parser().parse_args(arguments)

    try:
        if options.command == "status":
            _print_status(load_inventory_status(PROJECT_ROOT, state_dir=options.state_dir))

            return 0

        if (options.seed_batch is None) != (options.seed_receipt is None):
            raise ValueError("--seed-batch и --seed-receipt нужно указывать вместе")

        status = run_inventory(
            PROJECT_ROOT, state_dir=options.state_dir, interval_seconds=options.interval,
            once=options.once, seed_batch=options.seed_batch, seed_receipt=options.seed_receipt,
        )
        _print_status(status)

        return 0

    except (ManifestError, OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: {exception}. Проверьте состояние и квитанции перед продолжением.", file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print("Команда прервана. Исходники сохранены; незавершённая партия будет проверена при продолжении.",
              file=sys.stderr)

        return 130


if __name__ == "__main__":
    raise SystemExit(main())
