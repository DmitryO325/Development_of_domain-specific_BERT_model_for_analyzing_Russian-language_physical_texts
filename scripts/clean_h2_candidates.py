#!/usr/bin/env python3
"""Подготовить отдельную версию названий и аннотаций H2 без изменения формул."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = Path("manifests/results/h2_candidates_jinr_20261009_v1")

# Прямой запуск из scripts/ требует добавить корень до импортов локальных модулей.
sys.path.insert(0, str(PROJECT_ROOT))

from src.preprocess.h2_cleaning_io import (  # noqa: E402
    prepare_h2_cleaning,
    publish_h2_cleaning,
)


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Описать исходную очередь, новый каталог версии и проверку без записи."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue-dir", type=Path, default=project_root / DEFAULT_QUEUE,
        help="Каталог исходной очереди с candidates.jsonl, summary.json и review.csv",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Новый отдельный каталог внутри manifests/results; перезапись запрещена",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Проверить источники и рассчитать изменения без записи файлов",
    )

    return parser


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Очистить локальные метаданные без сети, назначения меток и записи реестров."""

    root = Path(project_root).resolve()
    parser = _build_parser(root)
    parsed = parser.parse_args(arguments)

    try:
        prepared = prepare_h2_cleaning(
            project_root=root,
            queue_dir=parsed.queue_dir,
            output_dir=parsed.output_dir,
        )
        created = publish_h2_cleaning(prepared, dry_run=parsed.dry_run)

    except (ValueError, OSError) as exception:
        print(f"ОШИБКА: не удалось подготовить метаданные H2: {exception}", file=sys.stderr)
        return 1

    counts = prepared.report["counts"]

    if parsed.dry_run:
        status = "Предварительная проверка без записи"

    elif created:
        status = "Создан новый пакет"

    else:
        status = "Точный повтор, запись не нужна"

    print(f"{status}. Карточек: {counts['candidates']}.")
    print(
        f"Изменено карточек: {counts['changed_candidates']}; "
        f"замен: {counts['changes']}."
    )
    print(
        f"Карточек с замечаниями: {counts['candidates_with_issues']}; "
        f"замечаний: {counts['issues']}."
    )
    print(f"Без названия: {counts['missing_title']}; без аннотации: {counts['missing_abstract']}.")
    print(f"{'Будущий каталог' if parsed.dry_run else 'Результат'}: {prepared.output_dir}")
    print("Это кандидаты для проверки, не готовая обучающая выборка.")
    print("Исходники, ручные заметки, метки, разбиение и реестры не изменены.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
