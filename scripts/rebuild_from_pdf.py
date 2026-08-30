#!/usr/bin/env python3
"""
Пересобрать текст в JSONL из уже скачанных PDF (data/raw/pdf/).

Полный текст статей УФН — в PDF (2 колонки), не на HTML-странице.
Извлечённый текст сохраняется в data/raw/pdf_text/*.txt и в поле text JSONL.

Требуется Tesseract для кириллицы:
  brew install tesseract tesseract-lang

Пример:
  python scripts/rebuild_from_pdf.py -i data/raw/ufn_100.jsonl \\
      -o data/raw/ufn_100_pdf.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.base import Document, append_jsonl  # noqa: E402
from src.collect.pdf_text import extract_best_text, is_readable_russian  # noqa: E402


def _parse_arguments() -> argparse.Namespace:
    """Разобрать параметры пересборки текстов из локальных PDF."""

    parser = argparse.ArgumentParser(
        description="Пересобрать тексты JSONL-записей из локальных PDF"
    )

    parser.add_argument(
        "-i",
        "--input",
        default=str(PROJECT_ROOT / "data" / "raw" / "ufn_100.jsonl"),
        help="Исходный JSONL-файл",
    )

    parser.add_argument(
        "-o",
        "--output",
        default=str(PROJECT_ROOT / "data" / "raw" / "ufn_100_pdf.jsonl"),
        help="Выходной JSONL-файл",
    )

    parser.add_argument(
        "--pdf-dir",
        default=str(PROJECT_ROOT / "data" / "raw" / "pdf"),
        help="Каталог с исходными PDF",
    )

    parser.add_argument(
        "--text-dir",
        default=str(PROJECT_ROOT / "data" / "raw" / "pdf_text"),
        help="Каталог для извлечённых TXT-файлов",
    )

    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Не использовать OCR",
    )

    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Удалить выходной файл перед записью",
    )

    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Сколько статей обработать (например 10)",
    )

    return parser.parse_args()


def _parse_json_record(json_line: str, *, line_number: int) -> dict[str, Any]:
    """Разобрать строку JSONL и проверить базовую структуру записи."""

    try:
        parsed_record: object = json.loads(json_line)

    except json.JSONDecodeError as exception:
        raise ValueError(
            f"строка {line_number}: некорректный JSON: {exception}"
        ) from exception

    if not isinstance(parsed_record, dict):
        raise ValueError(f"строка {line_number}: ожидается JSON-объект")

    record = cast(dict[str, Any], parsed_record)
    extra = record.get("extra", {})

    if not isinstance(extra, dict):
        raise ValueError(f"строка {line_number}: поле extra должно быть объектом")

    record["extra"] = extra

    return record


def main() -> None:
    """Пересобрать тексты JSONL-записей из уже загруженных PDF."""

    arguments = _parse_arguments()
    input_path = Path(arguments.input)
    output_path = Path(arguments.output)
    pdf_directory = Path(arguments.pdf_dir)
    text_directory = Path(arguments.text_dir)

    if arguments.fresh and output_path.exists():
        output_path.unlink()

    processed_count = 0
    skipped_count = 0

    input_lines = input_path.read_text(encoding="utf-8").strip().split("\n")

    for line_number, json_line in enumerate(input_lines, start=1):
        if not json_line.strip():
            continue

        document_record = _parse_json_record(
            json_line,
            line_number=line_number,
        )

        extra = document_record["extra"]

        pdf_name = extra.get("pdf_file") or (
            Path(document_record["pdf_url"]).name
            if document_record.get("pdf_url")
            else None
        )

        if not pdf_name:
            skipped_count += 1
            continue

        pdf_path = pdf_directory / pdf_name

        if not pdf_path.is_file():
            print(f"  нет PDF: {pdf_name}")
            skipped_count += 1
            continue

        text, extraction_method, is_readable = extract_best_text(
            pdf_path,
            text_dir=text_directory,
            try_ocr=not arguments.no_ocr,
        )

        if not is_readable and not is_readable_russian(text):
            print(f"  пропуск (нечитаемо): {pdf_name} [{extraction_method}]")
            skipped_count += 1
            continue

        document_record["text"] = text

        extra["text_source"] = extraction_method
        extra["pdf_extract_method"] = extraction_method

        extra["pdf_text_file"] = str(
            text_directory / f"{pdf_path.stem}_{extraction_method}.txt"
        )

        append_jsonl(
            Document(
                source=document_record["source"],
                url=document_record["url"],
                title=document_record["title"],
                text=text,
                authors=document_record.get("authors") or [],
                published=document_record.get("published"),
                section=document_record.get("section"),
                pdf_url=document_record.get("pdf_url"),
                language=document_record.get("language", "ru"),
                extra=extra,
            ),
            output_path,
        )

        processed_count += 1

        print(
            f"  [{processed_count}] {pdf_name} → "
            f"{extraction_method}, {len(text)} симв."
        )

        if arguments.limit is not None and processed_count >= arguments.limit:
            break

    print(
        f"\nГотово: {processed_count} в {output_path}, "
        f"пропущено {skipped_count}"
    )
    
    print(f"Тексты из PDF: {text_directory}/")


if __name__ == "__main__":
    main()
