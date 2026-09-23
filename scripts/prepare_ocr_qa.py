#!/usr/bin/env python3
"""Подготовить изображения, варианты и паспорт запуска OCR QA."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.ocr_qa_preparation import prepare_ocr_qa_run  # noqa: E402

DEFAULT_PAGE_PLAN = (
    PROJECT_ROOT
    / "manifests"
    / "plans"
    / "ufn_ocr_calibration_v1.json"
)
DEFAULT_RUN_ID = "ufn_ocr_calibration_20260904_v2"


def _build_parser() -> argparse.ArgumentParser:
    """Построить интерфейс команды подготовки OCR QA."""

    parser = argparse.ArgumentParser(
        description=(
            "Подготовка целенаправленного инженерного запуска OCR QA "
            "без создания ложных итоговых метрик"
        )
    )
    parser.add_argument(
        "--run-id",
        default=DEFAULT_RUN_ID,
        help=f"Идентификатор нового запуска (по умолчанию {DEFAULT_RUN_ID})",
    )
    parser.add_argument(
        "--page-plan",
        type=Path,
        default=DEFAULT_PAGE_PLAN,
        help="JSON-план выбранных страниц",
    )
    parser.add_argument(
        "--reviewer-id",
        default="dmitry_oberemok",
        help="Устойчивый идентификатор проверяющего",
    )
    parser.add_argument(
        "--created-at",
        help="Время ISO 8601 с часовым поясом; по умолчанию текущее",
    )

    return parser


def main() -> int:
    """Подготовить запуск и вернуть код завершения."""

    arguments = _build_parser().parse_args()

    try:
        result = prepare_ocr_qa_run(
            project_root=PROJECT_ROOT,
            qa_run_id=arguments.run_id,
            page_plan_path=arguments.page_plan,
            reviewer_id=arguments.reviewer_id,
            created_at=arguments.created_at,
        )

    except (OSError, RuntimeError, TypeError, ValueError) as exception:
        print(f"ОШИБКА: {exception}", file=sys.stderr)
        return 1

    print(f"Запуск: {result.qa_run_id}")
    print(f"Страниц: {result.page_count}")
    print(f"Вариантов: {result.variant_count}")
    print(
        "Кандидатов: "
        f"успешно {result.succeeded_candidates}, "
        f"с ошибкой {result.failed_candidates}"
    )
    print(f"Паспорт: {result.run_path}")
    print(f"Кадр: {result.frame_path}")
    print(f"Кандидаты: {result.candidate_manifest_path}")
    print(f"Печать: {result.seal_path}")
    print(f"SHA-256 паспорта: {result.run_sha256}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
