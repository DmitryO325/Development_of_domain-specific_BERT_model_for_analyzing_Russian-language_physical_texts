#!/usr/bin/env python3
"""
Сбор публичных карточек и PDF русских журналов Иоффе с возобновлением.

Пример: python scripts/download_ioffe.py issue 2088 --max-articles 2 --allow-network
Без --allow-network команда работает только с уже сохранёнными ответами.
"""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts/ должен находить модули проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.base import HttpResponseSnapshot  # noqa: E402
from src.collect.ioffe import IOFFE_JOURNALS  # noqa: E402
from src.collect.ioffe_archive import IoffeArchive, write_json  # noqa: E402
from src.collect.ioffe_collection import IoffeCollector  # noqa: E402
from src.collect.ioffe_http import IoffeClient, IoffeError, IoffeTransientNetworkError  # noqa: E402

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "ioffe"
DEFAULT_STATE_DIR = PROJECT_ROOT / "manifests" / "imports" / "ioffe"
# Общая пауза не зависит от выбранных каталогов партий и схемы HTTP/HTTPS.
RATE_LIMIT_DIR = DEFAULT_STATE_DIR / "rate_limit"


class RequestBudgetReached(Exception):
    """Штатная остановка после заданного числа обращений к сайту."""


class BudgetedIoffeClient(IoffeClient):
    """Ограничить объём одного запуска, не считая чтения локального кеша."""

    def __init__(self, *, max_requests: int, timeout: float) -> None:
        """Подготовить счётчик запросов и общую для партий паузу."""

        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0:
            raise ValueError("max_requests должен быть целым неотрицательным числом")

        super().__init__(RATE_LIMIT_DIR, timeout=timeout)
        self.max_requests = max_requests
        self.request_count = 0

    def get_response(self, url: str) -> HttpResponseSnapshot:
        """Выполнить разрешённое число запросов; ноль означает отсутствие лимита."""

        if self.max_requests and self.request_count >= self.max_requests:
            raise RequestBudgetReached

        self.request_count += 1
        print(f"Сетевой запрос {self.request_count}: {url} (пауза не меньше минуты)", flush=True)

        return super().get_response(url)


def _nonnegative(value: str) -> int:
    """Прочитать неотрицательный лимит из командной строки."""

    try:
        number = int(value)

    except ValueError as exception:
        raise argparse.ArgumentTypeError("Ожидается целое число") from exception

    if number < 0:
        raise argparse.ArgumentTypeError("Число не может быть отрицательным")

    return number


def _positive(value: str) -> int:
    """Прочитать положительный идентификатор публикации или выпуска."""

    number = _nonnegative(value)

    if not number:
        raise argparse.ArgumentTypeError("Идентификатор должен быть больше нуля")

    return number


def build_parser() -> argparse.ArgumentParser:
    """Описать команды для одного журнала, выпуска или статьи."""

    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--allow-network", action="store_true", help="Разрешить обращения к сайту")
    common.add_argument("--origin", choices=["https://journals.ioffe.ru", "http://journals.ioffe.ru"],
                        default="https://journals.ioffe.ru", help="HTTP выбирается только явно")
    common.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    common.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    common.add_argument("--run-id", help="Идентификатор запуска для проверки итогового файла")
    common.add_argument("--summary-path", type=Path, help="Дополнительный отдельный итог этого запуска")
    common.add_argument("--timeout", type=float, default=30.0)
    common.add_argument("--max-requests", type=_nonnegative, default=20,
                        help="Сетевых запросов за запуск, включая robots.txt; 0 — без лимита")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", parents=[common], help="Проверить robots.txt")
    article = commands.add_parser("article", parents=[common], help="Одна карточка и PDF")
    article.add_argument("article_id", type=_positive)
    issue = commands.add_parser("issue", parents=[common], help="Статьи одного выпуска")
    issue.add_argument("issue_id", type=_positive)
    issue.add_argument("--max-articles", type=_nonnegative, default=3, help="0 — все статьи")
    journal = commands.add_parser("journal", parents=[common], help="Архив русского журнала")
    journal.add_argument("journal_id", type=int, choices=sorted(IOFFE_JOURNALS))
    journal.add_argument("--max-issues", type=_nonnegative, default=1, help="0 — все выпуски")
    journal.add_argument("--max-articles", type=_nonnegative, default=3,
                         help="Общий лимит статей, включая кеш; 0 — все статьи")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Запустить сбор, сохраняя прогресс после каждой завершённой статьи."""

    arguments = build_parser().parse_args(argv)
    collector: IoffeCollector | None = None
    client: BudgetedIoffeClient | None = None
    status = "complete"
    error: str | None = None
    retryable_error = False
    exit_code = 0

    try:
        client = BudgetedIoffeClient(max_requests=arguments.max_requests, timeout=arguments.timeout)
        archive = IoffeArchive(arguments.output_dir, arguments.state_dir, client,
                               allow_network=arguments.allow_network)
        collector = IoffeCollector(archive, arguments.state_dir, arguments.origin)
        collector.prepare()

        if arguments.command == "probe":
            print("robots.txt проверен. Статьи и PDF не скачивались.")

        else:
            for url in collector.article_urls(
                journal_id=getattr(arguments, "journal_id", None),
                issue_id=getattr(arguments, "issue_id", None),
                article_id=getattr(arguments, "article_id", None),
                max_issues=getattr(arguments, "max_issues", 1),
                max_articles=getattr(arguments, "max_articles", 1),
            ):
                collector.collect_article(url)

            max_articles = getattr(arguments, "max_articles", 0)

            # Сборщик не загружает лишнюю страницу ради доказательства конца архива.
            # Поэтому достижение явной границы не выдаётся за полный обход.
            if max_articles and collector.completed >= max_articles:
                status = "article_limit"

            elif arguments.command == "journal" and arguments.max_issues:
                status = "issue_limit"

        print(f"Готово: карточек {collector.completed}, с PDF {collector.pdf_count}, "
              f"без ссылки PDF {collector.metadata_only_count}; сетевых запросов {client.request_count}.")

    except RequestBudgetReached:
        status = "request_limit"
        print("Достигнут лимит запросов. Повторите ту же команду: сохранённые ответы будут прочитаны из кеша.")

    except (IoffeError, OSError, ValueError, TypeError) as exception:
        status = "error"
        error = str(exception)
        retryable_error = isinstance(exception, IoffeTransientNetworkError)
        exit_code = 1
        print(f"ОШИБКА: {exception}. Сохранённые исходники не удалены.", file=sys.stderr)

    except KeyboardInterrupt:
        status = "interrupted"
        exit_code = 130
        print("Сбор остановлен пользователем; сохранённые ответы можно использовать при продолжении.")

    summary = {
        "run_id": arguments.run_id,
        "source_id": "ioffe",
        "command": arguments.command,
        "selected_volume": None,
        "selected_journal": getattr(arguments, "journal_id", None),
        "selected_issue": getattr(arguments, "issue_id", None),
        "selected_article": getattr(arguments, "article_id", None),
        "origin": arguments.origin,
        "metadata_only": False,
        "max_requests": arguments.max_requests,
        "max_articles": getattr(arguments, "max_articles", None),
        "max_issues": getattr(arguments, "max_issues", None),
        "status": status,
        "error": error,
        "retryable_error": retryable_error,
        "network_requests": client.request_count if client is not None else 0,
        "cards_processed": collector.completed if collector is not None else 0,
        "cards_with_pdf": collector.pdf_count if collector is not None else 0,
        "cards_without_pdf": collector.metadata_only_count if collector is not None else 0,
    }
    summary_paths = [arguments.state_dir / "last_run.json"]

    if arguments.summary_path is not None and arguments.summary_path not in summary_paths:
        summary_paths.append(arguments.summary_path)

    for summary_path in summary_paths:
        try:
            write_json(summary_path, summary)

        except (OSError, IoffeError, ValueError, TypeError) as exception:
            print(f"Не удалось сохранить итог запуска: {exception}", file=sys.stderr)
            exit_code = 1
            summary["status"] = "error"
            summary["error"] = f"Не удалось сохранить итог запуска: {exception}"
            summary["retryable_error"] = False

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
