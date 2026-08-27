#!/usr/bin/env python3
"""Сбор пилотного корпуса с сайтов физических журналов.

Примеры:
  python scripts/scrape.py ufn --max-docs 10 --text-source pdf
  python scripts/scrape.py ufn --text-source html --max-articles 3
  python scripts/scrape.py rss --feed https://quantum-electronics.ru/feed/ --limit 10
  python scripts/scrape.py pilot
"""

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collect.base import Document, append_jsonl  # noqa: E402
from src.collect.rss_feed import RssScraper  # noqa: E402
from src.collect.ufn import UfnScraper  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "raw" / "corpus.jsonl"
DEFAULT_PILOT_OUT = ROOT / "data" / "raw" / "pilot.jsonl"


def _save_docs(docs: Iterable[Document], out: Path) -> int:
    """Сохранить содержательные документы и вернуть их число."""
    saved = 0

    for doc in docs:
        if doc.extra.get("skipped") or len(doc.text.strip()) < 30:
            continue

        append_jsonl(doc, out)
        saved += 1

    return saved


def cmd_ufn(args: argparse.Namespace) -> None:
    """Выполнить сбор статей и RSS-карточек УФН."""
    scraper = UfnScraper(
        delay_seconds=args.delay,
        text_mode=args.text_source,
        pdf_dir=Path(args.pdf_dir),
        pdf_text_dir=Path(args.pdf_text_dir),
        min_pdf_chars=args.min_pdf_chars,
        try_ocr=not args.no_ocr,
    )
    out = Path(args.output)

    if args.fresh and out.exists():
        out.unlink()

    saved = 0

    if args.rss:
        docs = scraper.parse_rss(limit=args.rss_limit)
        saved += _save_docs(docs, out)
        print(f"RSS: сохранено {saved} записей → {out}")

    max_issues = args.max_issues
    if args.max_docs is not None and max_issues is None:
        print(
            f"Цель: {args.max_docs} статей — обход выпусков без лимита по числу номеров"
        )
    elif max_issues:
        print(f"Лимит: не более {max_issues} последних выпусков")

    for doc in scraper.iter_articles(
        max_issues=max_issues,
        max_articles_per_issue=args.max_articles,
        max_docs=args.max_docs,
    ):
        append_jsonl(doc, out)
        saved += 1
        src = doc.extra.get("text_source", "?")
        print(f"  [{saved}] {doc.title[:60]}… [{src}, {len(doc.text)} симв.]")

    print(f"Итого UFN: {saved} документов → {out}")


def cmd_rss(args: argparse.Namespace) -> None:
    """Выполнить сбор из одной или всех штатных RSS-лент."""
    scraper = RssScraper(delay_seconds=args.delay)
    out = Path(args.output)

    if args.fresh and out.exists():
        out.unlink()

    if args.feed:
        docs = scraper.parse_feed(args.feed, limit=args.limit)
        saved = _save_docs(docs, out)
        name = urlsplit(args.feed).hostname or args.feed
        print(f"  {name}: {len(docs)} элементов ленты")
    else:
        docs = list(scraper.iter_default_feeds(limit_per_feed=args.limit))
        for doc in docs:
            if doc.extra.get("skipped"):
                print(f"  {doc.source}: {doc.extra.get('error', 'неизвестная ошибка')}")
        saved = _save_docs(docs, out)

    print(f"Итого RSS: {saved} документов → {out}")


def cmd_pilot(args: argparse.Namespace) -> None:
    """Небольшой прогон для проверки: 1 выпуск УФН + RSS."""
    args.max_issues = 1
    args.max_articles = 4
    args.max_docs = 4
    args.rss = False
    args.rss_limit = 0
    args.text_source = "pdf+html"
    args.pdf_dir = str(ROOT / "data" / "raw" / "pdf")
    args.pdf_text_dir = str(ROOT / "data" / "raw" / "pdf_text")
    args.min_pdf_chars = 500
    args.no_ocr = False
    cmd_ufn(args)

    args.feed = None
    args.limit = 8
    args.fresh = False
    cmd_rss(args)

    # Статистика
    output = Path(args.output)
    content = output.read_text(encoding="utf-8").strip() if output.exists() else ""
    line_count = len(content.splitlines()) if content else 0
    print(f"\nПилот готов: {line_count} записей в {output}")


def main() -> None:
    """Разобрать аргументы командной строки и запустить выбранный сбор."""
    parser = argparse.ArgumentParser(description="Сбор текстов с сайтов НИР")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_OUT),
        help=("JSONL-файл (data/raw/corpus.jsonl; для pilot — data/raw/pilot.jsonl)"),
    )
    common.add_argument(
        "--delay", type=float, default=1.0, help="Пауза между запросами (с)"
    )
    common.add_argument(
        "--fresh", action="store_true", help="Удалить output перед записью"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_ufn = sub.add_parser(
        "ufn", parents=[common], help="ufn.ru — выпуски и HTML-статьи"
    )
    p_ufn.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help=(
            "Сколько последних выпусков (по умолчанию без лимита; "
            "с --max-docs идём по архиву до N статей)"
        ),
    )
    p_ufn.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Статей на выпуск (по умолчанию все)",
    )
    p_ufn.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Остановиться после N статей (например 100)",
    )
    p_ufn.add_argument("--rss", action="store_true", help="Дополнительно RSS ufn.ru")
    p_ufn.add_argument("--rss-limit", type=int, default=20)
    p_ufn.add_argument(
        "--text-source",
        choices=["pdf", "html", "pdf+html"],
        default="pdf+html",
        help=(
            "pdf+html (по умолч.): PDF если кириллица ок, иначе HTML; "
            "ufn.ru PDF часто без Unicode"
        ),
    )
    p_ufn.add_argument(
        "--pdf-dir",
        default=str(ROOT / "data" / "raw" / "pdf"),
        help="Куда сохранять PDF",
    )
    p_ufn.add_argument(
        "--min-pdf-chars",
        type=int,
        default=500,
        help="Мин. длина текста из PDF",
    )
    p_ufn.add_argument(
        "--pdf-text-dir",
        default=str(ROOT / "data" / "raw" / "pdf_text"),
        help="Куда писать .txt из PDF (видно, что распарсилось)",
    )
    p_ufn.add_argument(
        "--no-ocr",
        action="store_true",
        help="Без Tesseract (на ufn.ru PDF без OCR обычно нечитаемы)",
    )
    p_ufn.set_defaults(func=cmd_ufn)

    p_rss = sub.add_parser("rss", parents=[common], help="RSS-ленты журналов")
    p_rss.add_argument("--feed", help="URL одной ленты")
    p_rss.add_argument("--limit", type=int, default=20)
    p_rss.set_defaults(func=cmd_rss)

    p_pilot = sub.add_parser(
        "pilot",
        parents=[common],
        help="Пилотный прогон (1 выпуск + RSS)",
    )
    p_pilot.set_defaults(func=cmd_pilot, output=str(DEFAULT_PILOT_OUT))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
