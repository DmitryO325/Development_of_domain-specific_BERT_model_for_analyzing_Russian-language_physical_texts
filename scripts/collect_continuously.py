#!/usr/bin/env python3
"""
Создание, продолжение и просмотр сохранённой очереди сбора журналов.

Создание очереди и просмотр состояния не обращаются к сети. Для продолжения
сбора требуется явный параметр --allow-network; исходные ответы остаются в кеше.
"""

from __future__ import annotations

import argparse
import math
import sys

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts должен находить локальные модули проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.collection_queue import build_queue, load_queue, save_queue  # noqa: E402
# Создание и выполнение очереди используют одну блокировку, чтобы init не затирал состояние run.
from src.collect.continuous_collection import _runner_lock, run_queue  # noqa: E402
from src.collect.ioffe_http import IoffeError  # noqa: E402

DEFAULT_QUEUE = PROJECT_ROOT / "manifests" / "imports" / "collection_queue" / "queue.json"
STATUS_LABELS = {
    "pending": "ожидают запуска",
    "running": "выполняются",
    "retry_wait": "ожидают повтора после сетевого сбоя",
    "complete": "обход завершён",
    "complete_with_deferred": "обход завершён с пропусками",
    "blocked": "требуют проверки",
    "interrupted": "прерваны",
}
DETAIL_STATUSES = {"running", "retry_wait", "blocked", "interrupted", "complete_with_deferred"}
MAX_STATUS_DETAILS = 20
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def _positive_number(value: str) -> float:
    """Прочитать конечное положительное число секунд."""

    try:
        number = float(value)

    except ValueError as exception:
        raise argparse.ArgumentTypeError("Ожидается положительное число секунд") from exception

    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Число секунд должно быть конечным и больше нуля")

    return number


def build_parser() -> argparse.ArgumentParser:
    """Описать отдельные безопасные команды управления очередью."""

    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="Путь к сохранённой очереди")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init", parents=[common], help="Создать очередь без обращения к сети")
    create.add_argument("--ioffe-origin", default="https://journals.ioffe.ru",
                        choices=("http://journals.ioffe.ru", "https://journals.ioffe.ru"),
                        help="Явно выбранный адрес журналов Иоффе")
    run = commands.add_parser("run", parents=[common], help="Продолжить сохранённую очередь")
    run.add_argument("--allow-network", action="store_true", help="Разрешить сетевые запросы")
    run.add_argument("--timeout", type=_positive_number, default=60.0, help="Тайм-аут одного запроса, с")
    run.add_argument("--retry-base", type=_positive_number, default=60.0,
                     help="Начальная пауза после временного сетевого сбоя, с; не меньше 60")
    run.add_argument("--retry-max", type=_positive_number, default=1800.0,
                     help="Максимальная пауза после временного сетевого сбоя, с")
    commands.add_parser("status", parents=[common], help="Показать состояние без сети и изменений")

    return parser


def _one_line(value: object, *, limit: int = 240) -> str:
    """Ограничить длину подробности и убрать переносы строк из краткого отчёта."""

    text = " ".join(str(value).split())

    return text if len(text) <= limit else text[:limit - 1] + "…"


def _print_task(task: dict[str, Any]) -> None:
    """Показать причину ожидания или остановки одной задачи без исходных данных."""

    status = task["status"]
    scope = f"{task['source_id']}; {task['task_id']}"

    if task.get("volume") is not None:
        scope += f"; том {task['volume']}"

    if task.get("journal_id") is not None:
        scope += f"; журнал {task['journal_id']}"

    if task.get("metadata_only"):
        scope += "; только метаданные"

    print(f"  {_one_line(scope)}: {STATUS_LABELS.get(status, status)}")
    retry_at = task.get("next_retry_at")

    if retry_at is not None:
        try:
            retry_time = datetime.fromtimestamp(retry_at, tz=MOSCOW_TIMEZONE)

        except (OverflowError, OSError, ValueError) as exception:
            raise ValueError("Время следующего повтора выходит за допустимый диапазон дат") from exception

        print(f"    Следующая попытка: {retry_time:%Y-%m-%d %H:%M:%S} МСК")

    if task.get("last_error"):
        print(f"    Причина: {_one_line(task['last_error'])}")

    if task.get("last_log"):
        print(f"    Журнал запуска: {_one_line(task['last_log'])}")


def _print_status(queue_path: Path, queue: dict[str, Any]) -> None:
    """Показать число задач по состояниям, не выдавая его за число статей."""

    tasks = queue["tasks"]
    counts = Counter(task["status"] for task in tasks)
    print(f"Очередь: {queue_path}")
    print(f"Задач: {len(tasks)}. Это задания обхода, не количество статей.")

    for status, label in STATUS_LABELS.items():
        if counts[status]:
            print(f"  {label}: {counts[status]}")

    detailed_tasks = [task for task in tasks if task["status"] in DETAIL_STATUSES]

    for task in detailed_tasks[:MAX_STATUS_DETAILS]:
        _print_task(task)

    if len(detailed_tasks) > MAX_STATUS_DETAILS:
        print(f"  Ещё задач с подробностями: {len(detailed_tasks) - MAX_STATUS_DETAILS}; см. файл очереди.")

    if counts["running"]:
        print("Статус «выполняются» сохранён в очереди; сам по себе он не подтверждает, что процесс ещё работает.")


def main(argv: list[str] | None = None) -> int:
    """Выполнить управление очередью, не разрешая сеть неявно."""

    arguments = build_parser().parse_args(argv)

    try:
        if arguments.command == "init":
            with _runner_lock(PROJECT_ROOT):
                if arguments.queue.exists() or arguments.queue.is_symlink():
                    raise ValueError("Очередь уже существует или является ссылкой; существующие данные не изменены")

                queue = build_queue(PROJECT_ROOT, ioffe_origin=arguments.ioffe_origin)

                if arguments.queue.exists() or arguments.queue.is_symlink():
                    raise ValueError("Очередь появилась во время подготовки; существующие данные не изменены")

                save_queue(arguments.queue, queue)
                print(f"Очередь создана: {arguments.queue}. Задач: {len(queue['tasks'])}. Сеть не использовалась.")

            return 0

        if arguments.command == "status":
            _print_status(arguments.queue, load_queue(arguments.queue))

            return 0

        if not arguments.allow_network:
            raise ValueError("Для продолжения сбора нужно явно указать --allow-network")

        if arguments.retry_base < 60:
            raise ValueError("Начальная пауза --retry-base должна быть не меньше 60 секунд")

        if arguments.retry_max < arguments.retry_base:
            raise ValueError("Максимальная пауза --retry-max не может быть меньше --retry-base")

        return run_queue(arguments.queue, PROJECT_ROOT, allow_network=True,
                         timeout_seconds=arguments.timeout, retry_base_seconds=arguments.retry_base,
                         retry_max_seconds=arguments.retry_max)

    except (IoffeError, OSError, ValueError, TypeError) as exception:
        print(f"ОШИБКА: {exception}", file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print("Команда прервана. Сохранённые файлы не удалены.", file=sys.stderr)

        return 130


if __name__ == "__main__":
    raise SystemExit(main())
