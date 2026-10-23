#!/usr/bin/env python3
"""
Сбор публичных русских карточек и PDF УФН, ЖЭТФ, Писем в ЖЭТФ и Квантовой электроники.

По умолчанию команда работает без сети. Пример: download_journals.py probe jetp.
"""

from __future__ import annotations

import argparse
import re
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts должен находить локальные модули проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.base import HttpResponseSnapshot  # noqa: E402
from src.collect.ioffe_archive import IoffeArchive, write_json  # noqa: E402
from src.collect.ioffe_http import IoffeClient, IoffeError, IoffeTransientNetworkError  # noqa: E402
from src.collect.jetp import START_URL as JETP_START_URL  # noqa: E402
from src.collect.journal_collection import JournalCollector  # noqa: E402
from src.collect.journal_sources import SOURCE_MODULES, JournalSource, get_source  # noqa: E402

STATE_ROOT = PROJECT_ROOT / "manifests" / "imports" / "journals"
OUTPUT_ROOT = PROJECT_ROOT / "data" / "raw" / "journals"


class RequestBudgetReached(Exception):
    """Остановка очередной порции после заданного числа сетевых обращений."""


class JournalClient(IoffeClient):
    """Использовать проверенный транспорт со своими путями и общим состоянием источника."""

    def __init__(self, source: JournalSource, *, max_requests: int, timeout: float) -> None:
        """Отделить паузы разных сайтов от каталогов результатов конкретного запуска."""

        if type(max_requests) is not int or max_requests < 0:
            raise ValueError("Лимит запросов должен быть целым неотрицательным числом")

        super().__init__(STATE_ROOT / "rate_limit" / source.source_id,
                         validate_url=source.validate_url, state_prefix="journal", timeout=timeout)
        self.max_requests = max_requests
        self.request_count = 0
        self.source_id = source.source_id

    def get_response(self, url: str) -> HttpResponseSnapshot:
        """Учитывать только сетевые запросы, не чтение уже сохранённых файлов."""

        self.validate_url(url)
        self._count_request(url)

        return super().get_response(url)

    def post_form_response(self, url: str, body: bytes) -> HttpResponseSnapshot:
        """Включить выбор тома ЖЭТФ в общий бюджет сетевых обращений."""

        self.validate_url(url)

        if self.source_id != "jetp" or url != JETP_START_URL:
            raise ValueError("Отправка формы поддержана только для выбора тома ЖЭТФ")

        if not isinstance(body, bytes) or len(body) > 4096 or re.fullmatch(
            rb"a=select&issues_lang=r&volume=[1-9][0-9]*", body
        ) is None:
            raise ValueError("Недопустимое тело формы выбора тома ЖЭТФ")

        self._count_request(url, method="POST")

        return super().post_form_response(url, body)

    def _count_request(self, url: str, *, method: str = "GET") -> None:
        """Проверить общий лимит до сетевого вызова любого поддержанного метода."""

        if self.max_requests and self.request_count >= self.max_requests:
            raise RequestBudgetReached

        self.request_count += 1
        print(f"Запрос {self.request_count}: {method} {url}; пауза не меньше 60 секунд", flush=True)


def _nonnegative(value: str) -> int:
    """Прочитать неотрицательный лимит из аргумента команды."""

    try:
        number = int(value)

    except ValueError as exception:
        raise argparse.ArgumentTypeError("Ожидается целое число") from exception

    if number < 0:
        raise argparse.ArgumentTypeError("Лимит не может быть отрицательным")

    return number


def _positive(value: str) -> int:
    """Прочитать положительный номер тома, ещё не подтверждая его наличие на сайте."""

    number = _nonnegative(value)

    if number == 0:
        raise argparse.ArgumentTypeError("Номер тома должен быть больше нуля")

    return number


def build_parser() -> argparse.ArgumentParser:
    """Описать явное включение сети и ограниченные порции скачивания."""

    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("source", choices=sorted(SOURCE_MODULES))
    common.add_argument("--allow-network", action="store_true")
    common.add_argument("--max-requests", type=_nonnegative, default=20,
                        help="Сетевые запросы, включая robots.txt; 0 — без лимита")
    common.add_argument("--timeout", type=float, default=30.0)
    common.add_argument("--output-dir", type=Path)
    common.add_argument("--state-dir", type=Path)
    common.add_argument("--run-id", help="Идентификатор запуска для проверки итогового файла")
    common.add_argument("--summary-path", type=Path, help="Дополнительный отдельный итог этого запуска")
    common.add_argument("--refresh-url", action="append", default=[],
                        help="Явно обновить этот ответ при обходе; нужен --allow-network")
    common.add_argument("--metadata-only", action="store_true",
                        help="Получать только HTML и метаданные, не запрашивая PDF")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", parents=[common], help="Получить и проверить только robots.txt")
    crawl = commands.add_parser("crawl", parents=[common], help="Обойти опубликованные ссылки русского архива")
    start = crawl.add_mutually_exclusive_group()
    start.add_argument("--start-url", help="Явная ссылка на проверенный выпуск или карточку")
    start.add_argument("--volume", type=_positive,
                       help="Один том ЖЭТФ из опубликованной формы; обход только этого тома")
    crawl.add_argument("--max-articles", type=_nonnegative, default=0,
                       help="Карточек, включая кеш; 0 — без ограничения")
    article = commands.add_parser("article", parents=[common], help="Одна русская карточка и PDF")
    article.add_argument("url")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполнить одну порцию, оставив исходники и краткий итог после остановки."""

    arguments = build_parser().parse_args(argv)
    collector: JournalCollector | None = None
    client: JournalClient | None = None
    status = "complete"
    error: str | None = None
    retryable_error = False
    exit_code = 0

    try:
        if arguments.refresh_url and not arguments.allow_network:
            raise ValueError("Для --refresh-url нужно явно включить --allow-network")

        if getattr(arguments, "volume", None) is not None and arguments.source != "jetp":
            raise ValueError("Выбор --volume поддержан только для ЖЭТФ (jetp)")

        source = get_source(arguments.source)
        client = JournalClient(source, max_requests=arguments.max_requests, timeout=arguments.timeout)
        archive = IoffeArchive(arguments.output_dir or OUTPUT_ROOT / source.source_id,
                               arguments.state_dir or STATE_ROOT / source.source_id, client,
                               allow_network=arguments.allow_network, validate_url=source.validate_url,
                               accepted_statuses=source.accepted_statuses)
        collector = JournalCollector(source, archive, refresh_urls=tuple(arguments.refresh_url),
                                      download_pdfs=not arguments.metadata_only)

        if arguments.command == "probe":
            collector.prepare()
            print("robots.txt проверен; карточки и PDF не скачивались.")

        elif arguments.command == "article":
            collector.collect_article(arguments.url)

        else:
            if arguments.volume is None:
                collector.crawl(start_url=arguments.start_url, max_articles=arguments.max_articles)

            else:
                collector.crawl(volume=arguments.volume, max_articles=arguments.max_articles)

    except RequestBudgetReached:
        status = "request_limit"
        print("Достигнут лимит запросов. Для продолжения повторите ту же команду.")

    except (IoffeError, OSError, ValueError, TypeError) as exception:
        status = "error"
        error = str(exception)
        retryable_error = isinstance(exception, IoffeTransientNetworkError)
        exit_code = 1
        print(f"ОШИБКА: {exception}. Исходные файлы сохранены; повторов нет.", file=sys.stderr)

    except KeyboardInterrupt:
        status = "interrupted"
        exit_code = 130
        print("Сбор остановлен; сохранённые файлы будут использованы при продолжении.")

    if collector is not None and client is not None and status == "complete":
        if collector.article_limit_reached:
            status = "article_limit"

        elif collector.deferred_issues_count:
            status = "complete_with_deferred"

    summary = {
        "run_id": arguments.run_id,
        "source_id": arguments.source,
        "command": arguments.command,
        "status": status,
        "error": error,
        "retryable_error": retryable_error,
        "network_requests": client.request_count if client is not None else 0,
        "cards_processed": collector.completed if collector is not None else 0,
        "new_records": collector.new_records if collector is not None else 0,
        "cards_with_pdf": collector.pdf_count if collector is not None else 0,
        "deferred_issues_count": collector.deferred_issues_count if collector is not None else 0,
        "deferred_entries_count": collector.deferred_entries_count if collector is not None else 0,
        "deferred_issues": list(collector.deferred_issues.values()) if collector is not None else [],
        "metadata_only": arguments.metadata_only,
        "selected_volume": getattr(arguments, "volume", None),
        "selected_journal": None,
        "max_requests": arguments.max_requests,
        "max_articles": getattr(arguments, "max_articles", None),
        "max_issues": None,
        "start_url": getattr(arguments, "start_url", None),
        "scope": (
            f"Только том {arguments.volume} ЖЭТФ; это не весь архив"
            if getattr(arguments, "volume", None) is not None
            else "Одна команда; достижение лимита не означает получение всего архива"
        ),
    }
    state_dir = (
        collector.archive.state_dir if collector is not None
        else arguments.state_dir or STATE_ROOT / arguments.source
    )
    summary_paths = [state_dir / "last_run.json"]

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

    if collector is not None and client is not None:
        print(f"Карточек обработано: {collector.completed}; новых записей: {collector.new_records}; "
              f"с PDF: {collector.pdf_count}; отложено оглавлений: {collector.deferred_issues_count} "
              f"({collector.deferred_entries_count} публикаций); сетевых запросов: {client.request_count}.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
