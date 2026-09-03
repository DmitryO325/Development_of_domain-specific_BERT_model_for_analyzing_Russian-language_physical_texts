#!/usr/bin/env python3
"""Проверка связей, файлов и арифметики одного запуска OCR QA."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.ocr_qa_validation import OcrQaValidator  # noqa: E402


def main() -> int:
    """Проверить запуск OCR QA и вернуть код завершения команды."""

    parser = argparse.ArgumentParser(
        description="Проверка машинных форм одного запуска OCR QA"
    )
    parser.add_argument(
        "--run",
        type=Path,
        required=True,
        help="JSON-паспорт запуска",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        required=True,
        help="JSON-сводка запуска",
    )
    parser.add_argument(
        "--frame",
        type=Path,
        help="JSONL-выборка; по умолчанию берётся из паспорта",
    )
    parser.add_argument(
        "--pages",
        type=Path,
        help="JSONL с результатами страниц; по умолчанию берётся из сводки",
    )
    parser.add_argument(
        "--formulas",
        type=Path,
        help="JSONL с результатами формул; по умолчанию берётся из сводки",
    )
    parser.add_argument(
        "--without-file-checks",
        action="store_true",
        help=(
            "Не проверять наличие и SHA-256 ссылочных файлов; "
            "только для синтетических примеров и модульных тестов"
        ),
    )
    arguments = parser.parse_args()
    report = OcrQaValidator(
        PROJECT_ROOT,
        check_files=not arguments.without_file_checks,
    ).validate(
        run_path=arguments.run,
        summary_path=arguments.summary,
        frame_path=arguments.frame,
        page_results_path=arguments.pages,
        formula_results_path=arguments.formulas,
    )

    if report.counts:
        print(
            "Записей: "
            + ", ".join(
                f"{name}={count}"
                for name, count in report.counts.items()
            )
        )

    for warning in report.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")

    for error in report.errors:
        print(f"ОШИБКА: {error}", file=sys.stderr)

    if not report.ok:
        print("Проверка OCR QA не пройдена.", file=sys.stderr)
        return 1

    print("Проверка OCR QA пройдена.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
