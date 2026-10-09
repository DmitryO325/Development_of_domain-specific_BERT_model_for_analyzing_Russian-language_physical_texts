#!/usr/bin/env python3
"""
Проверка кеша и одиночное получение ответов API Math-Net.Ru.

Без --allow-network команда читает только локальный кеш. Реальные адреса
и форматы пока не подтверждены; команда не обходит список публикаций.

Примеры без сети:
  python scripts/download_mathnet.py probe
  python scripts/download_mathnet.py articles JOURNAL_ID
  python scripts/download_mathnet.py article ARTICLE_ID
  python scripts/download_mathnet.py text ARTICLE_ID
"""

from __future__ import annotations

import argparse
import re
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts/ должен находить модули проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.mathnet_api import (  # noqa: E402
    DEFAULT_API_URL,
    MathnetApiClient,
    MathnetApiError,
    validate_api_url,
)
from src.collect.mathnet_archive import MathnetArchive  # noqa: E402

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "mathnet_api"
DEFAULT_STATE_DIR = PROJECT_ROOT / "manifests" / "imports" / "mathnet_api"


def _identifier(value: str) -> str:
    """Принять точный идентификатор без разделителей пути и автодополнения."""

    if re.fullmatch(r"[A-Za-z0-9]{1,80}", value) is None:
        raise argparse.ArgumentTypeError(
            "Идентификатор должен содержать от 1 до 80 латинских букв или цифр"
        )

    return value


def _add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool = False,
) -> None:
    """Разрешить общие параметры до и после подкоманды без потери значений."""

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_OUTPUT_DIR,
        help="Каталог неизменяемых ответов API",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_STATE_DIR,
        help="Каталог индекса кеша и состояния паузы",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Явно разрешить один сетевой запрос, если ответа нет в кеше",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Получить новую версию ответа; требует --allow-network",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS if suppress_defaults else 30.0,
        help="Тайм-аут сетевого запроса в секундах",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=argparse.SUPPRESS if suppress_defaults else 60.0,
        help="Пауза после предыдущего запроса, не менее 60 секунд",
    )


def _create_parser() -> argparse.ArgumentParser:
    """Описать одиночные команды без массового обхода и произвольных URL."""

    parser = argparse.ArgumentParser(
        description="Math-Net.Ru: по умолчанию только локальный кеш, без сети",
        allow_abbrev=False,
    )
    _add_common_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "probe": "Список журналов по точному адресу /api/journals/list из письма",
        "journals": "Список журналов по явно выбранной схеме адресов",
        "articles": "Список публикаций одного журнала, без обхода карточек",
        "article": "Метаданные одной публикации по точному идентификатору",
        "text": "Распознанный текст одной публикации по точному идентификатору",
    }

    for command, description in descriptions.items():
        command_parser = commands.add_parser(command, help=description, allow_abbrev=False)
        _add_common_arguments(command_parser, suppress_defaults=True)

        if command in {"journals", "articles", "article"}:
            command_parser.add_argument(
                "--route-style",
                choices=("examples", "nested"),
                default="examples",
                help="Схема из примеров письма или вложенная; без перебора адресов",
            )

        if command == "articles":
            command_parser.add_argument("journal_id", type=_identifier)

        if command in {"article", "text"}:
            command_parser.add_argument("article_id", type=_identifier)

        if command == "article":
            command_parser.add_argument(
                "--journal-id",
                type=_identifier,
                help="Обязателен только для вложенного маршрута nested",
            )

        if command == "text":
            command_parser.add_argument(
                "--encoding",
                help="Явная кодировка исходного текста, если она подтверждена",
            )

    return parser


def _request_url(arguments: argparse.Namespace) -> str:
    """Построить один маршрут, не угадывая префикс идентификатора статьи."""

    if arguments.command == "probe":
        path = "/journals/list"

    elif arguments.command == "text":
        path = f"/texts/{arguments.article_id}"

    elif arguments.command == "journals":
        path = "/journals/list" if arguments.route_style == "examples" else "/journals"

    elif arguments.command == "articles":
        path = f"/journals/{arguments.journal_id}"

        if arguments.route_style == "nested":
            path += "/articles"

    elif arguments.route_style == "nested":
        if arguments.journal_id is None:
            raise ValueError("Для article --route-style nested нужен --journal-id")

        path = f"/journals/{arguments.journal_id}/articles/{arguments.article_id}"

    else:
        if arguments.journal_id is not None:
            raise ValueError("--journal-id используется только с --route-style nested")

        path = f"/journals/{arguments.article_id}"

    url = f"{DEFAULT_API_URL}{path}"
    validate_api_url(url)

    return url


def main(argv: list[str] | None = None) -> int:
    """Прочитать один ответ из кеша либо явно разрешить его получение."""

    arguments = _create_parser().parse_args(argv)

    try:
        if arguments.refresh and not arguments.allow_network:
            raise ValueError("--refresh требует явного разрешения --allow-network")

        url = _request_url(arguments)
        client = None

        if arguments.allow_network:
            client = MathnetApiClient(
                arguments.state_dir,
                timeout=arguments.timeout,
                delay_seconds=arguments.delay,
            )

            print(f"Сеть разрешена: {url}. Действует пауза между запросами.", flush=True)

        archive = MathnetArchive(arguments.output_dir, arguments.state_dir, client)
        result = archive.fetch(
            url,
            kind="text" if arguments.command == "text" else "json",
            allow_network=arguments.allow_network,
            refresh=arguments.refresh,
            encoding=getattr(arguments, "encoding", None),
        )

        if result.from_cache:
            print("Ответ проверен в локальном кеше; сетевого запроса не было.")

        else:
            print("Один ответ получен из сети и сохранён; обход публикаций не запускался.")

        print(f"Тело ответа: {result.body_path}")
        print(f"Сведения об ответе: {result.metadata_path}")

    except (MathnetApiError, OSError, ValueError) as exception:
        print(f"ОШИБКА: {exception}", file=sys.stderr)
        return 1

    except KeyboardInterrupt:
        print("Получение остановлено; уже сохранённые ответы остаются на диске.")
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
