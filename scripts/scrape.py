#!/usr/bin/env python3
"""
Сбор пилотного корпуса с сайтов физических журналов.

Примеры:
  python scripts/scrape.py ufn --max-docs 10 --text-source pdf
  python scripts/scrape.py ufn --text-source html --max-articles 3
  python scripts/scrape.py rss --feed https://quantum-electronics.ru/feed/ --limit 10
  python scripts/scrape.py pilot
"""

from __future__ import annotations

import argparse
import sys

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.base import Document, append_jsonl  # noqa: E402
from src.collect.rss_feed import RssScraper  # noqa: E402
from src.collect.ufn import UfnScraper  # noqa: E402

DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "corpus.jsonl"
DEFAULT_PILOT_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "pilot.jsonl"


def _save_documents(documents: Iterable[Document], output_path: Path) -> int:
    """Сохранить содержательные документы и вернуть их число."""

    saved_count = 0

    for document in documents:
        if document.extra.get("skipped") or len(document.text.strip()) < 30:
            continue

        append_jsonl(document, output_path)
        saved_count += 1

    return saved_count


def cmd_ufn(arguments: argparse.Namespace) -> None:
    """Выполнить сбор статей и RSS-карточек УФН."""

    scraper = UfnScraper(
        delay_seconds=arguments.delay,
        text_mode=arguments.text_source,
        pdf_dir=Path(arguments.pdf_dir),
        pdf_text_dir=Path(arguments.pdf_text_dir),
        min_pdf_chars=arguments.min_pdf_chars,
        try_ocr=not arguments.no_ocr,
    )

    output_path = Path(arguments.output)

    if arguments.fresh and output_path.exists():
        output_path.unlink()

    saved_count = 0

    if arguments.rss:
        documents = scraper.parse_rss(limit=arguments.rss_limit)
        saved_count += _save_documents(documents, output_path)
        print(f"RSS: сохранено {saved_count} записей → {output_path}")

    max_issues = arguments.max_issues

    if arguments.max_docs is not None and max_issues is None:
        print(
            f"Цель: {arguments.max_docs} статей — обход выпусков "
            "без лимита по числу номеров"
        )

    elif max_issues:
        print(f"Лимит: не более {max_issues} последних выпусков")

    for document in scraper.iter_articles(
        max_issues=max_issues,
        max_articles_per_issue=arguments.max_articles,
        max_docs=arguments.max_docs,
    ):
        append_jsonl(document, output_path)
        saved_count += 1
        text_source = document.extra.get("text_source", "?")

        print(
            f"  [{saved_count}] {document.title[:60]}… "
            f"[{text_source}, {len(document.text)} симв.]"
        )

    print(f"Итого UFN: {saved_count} документов → {output_path}")


def cmd_rss(arguments: argparse.Namespace) -> None:
    """Выполнить сбор из одной или всех штатных RSS-лент."""

    scraper = RssScraper(delay_seconds=arguments.delay)
    output_path = Path(arguments.output)

    if arguments.fresh and output_path.exists():
        output_path.unlink()

    if arguments.feed:
        documents = scraper.parse_feed(arguments.feed, limit=arguments.limit)
        saved_count = _save_documents(documents, output_path)
        source_name = urlsplit(arguments.feed).hostname or arguments.feed
        print(f"  {source_name}: {len(documents)} элементов ленты")

    else:
        documents = list(
            scraper.iter_default_feeds(limit_per_feed=arguments.limit)
        )

        for document in documents:
            if document.extra.get("skipped"):
                error_message = document.extra.get("error", "неизвестная ошибка")
                print(f"  {document.source}: {error_message}")

        saved_count = _save_documents(documents, output_path)

    print(f"Итого RSS: {saved_count} документов → {output_path}")


def cmd_pilot(arguments: argparse.Namespace) -> None:
    """Небольшой прогон для проверки: 1 выпуск УФН + RSS."""

    arguments.max_issues = 1
    arguments.max_articles = 4
    arguments.max_docs = 4
    arguments.rss = False
    arguments.rss_limit = 0
    arguments.text_source = "pdf+html"
    arguments.pdf_dir = str(PROJECT_ROOT / "data" / "raw" / "pdf")
    arguments.pdf_text_dir = str(PROJECT_ROOT / "data" / "raw" / "pdf_text")
    arguments.min_pdf_chars = 500
    arguments.no_ocr = False
    cmd_ufn(arguments)

    arguments.feed = None
    arguments.limit = 8
    arguments.fresh = False
    cmd_rss(arguments)

    # Статистика
    output_path = Path(arguments.output)

    content = (
        output_path.read_text(encoding="utf-8").strip()
        if output_path.exists()
        else ""
    )

    line_count = len(content.splitlines()) if content else 0
    print(f"\nПилот готов: {line_count} записей в {output_path}")


def main() -> None:
    """Разобрать аргументы командной строки и запустить выбранный сбор."""

    parser = argparse.ArgumentParser(description="Сбор текстов с сайтов НИР")
    common_parser = argparse.ArgumentParser(add_help=False)

    common_parser.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_OUTPUT_PATH),
        help=(
            "JSONL-файл (data/raw/corpus.jsonl; "
            "для pilot — data/raw/pilot.jsonl)"
        ),
    )

    common_parser.add_argument(
        "--delay", type=float, default=1.0, help="Пауза между запросами (с)"
    )

    common_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Удалить выходной файл перед записью",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    ufn_parser = subparsers.add_parser(
        "ufn", parents=[common_parser], help="ufn.ru — выпуски и HTML-статьи"
    )

    ufn_parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help=(
            "Сколько последних выпусков (по умолчанию без лимита; "
            "с --max-docs идём по архиву до N статей)"
        ),
    )

    ufn_parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Статей на выпуск (по умолчанию все)",
    )

    ufn_parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Остановиться после N статей (например 100)",
    )

    ufn_parser.add_argument(
        "--rss", action="store_true", help="Дополнительно RSS ufn.ru"
    )

    ufn_parser.add_argument("--rss-limit", type=int, default=20)

    ufn_parser.add_argument(
        "--text-source",
        choices=["pdf", "html", "pdf+html"],
        default="pdf+html",
        help=(
            "pdf+html (по умолч.): PDF если кириллица ок, иначе HTML; "
            "ufn.ru PDF часто без Unicode"
        ),
    )

    ufn_parser.add_argument(
        "--pdf-dir",
        default=str(PROJECT_ROOT / "data" / "raw" / "pdf"),
        help="Куда сохранять PDF",
    )

    ufn_parser.add_argument(
        "--min-pdf-chars",
        type=int,
        default=500,
        help="Мин. длина текста из PDF",
    )

    ufn_parser.add_argument(
        "--pdf-text-dir",
        default=str(PROJECT_ROOT / "data" / "raw" / "pdf_text"),
        help="Куда писать .txt из PDF (видно, что распарсилось)",
    )

    ufn_parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Без Tesseract (на ufn.ru PDF без OCR обычно нечитаемы)",
    )

    ufn_parser.set_defaults(func=cmd_ufn)

    rss_parser = subparsers.add_parser(
        "rss", parents=[common_parser], help="RSS-ленты журналов"
    )

    rss_parser.add_argument("--feed", help="URL одной ленты")
    rss_parser.add_argument("--limit", type=int, default=20)
    rss_parser.set_defaults(func=cmd_rss)

    pilot_parser = subparsers.add_parser(
        "pilot",
        parents=[common_parser],
        help="Пилотный прогон (1 выпуск + RSS)",
    )
    
    pilot_parser.set_defaults(
        func=cmd_pilot,
        output=str(DEFAULT_PILOT_OUTPUT_PATH),
    )

    arguments = parser.parse_args()
    arguments.func(arguments)


if __name__ == "__main__":
    main()
