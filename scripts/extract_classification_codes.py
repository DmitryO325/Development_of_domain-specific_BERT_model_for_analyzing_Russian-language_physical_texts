#!/usr/bin/env python3
"""Подготовить исходные классификационные коды для проверки без изменения реестров."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts/ требует добавить корень до импортов проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.classification_candidates import (  # noqa: E402
    build_classification_candidates,
    export_classification_candidates,
)


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Собрать локальный пакет для просмотра, не загружая и не регистрируя статьи."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path,
        help="Необязательный проверяемый снимок метаданных ОИЯИ для дополнения УДК",
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="Новый каталог версии внутри manifests/results",
    )
    options = parser.parse_args(arguments)

    try:
        rows, summary = build_classification_candidates(
            project_root, snapshot_path=options.snapshot,
        )
        output = export_classification_candidates(project_root, options.output_dir, rows, summary)

    except (ValueError, OSError) as exception:
        parser.error(f"Не удалось подготовить классификационные коды: {exception}")

    print(f"Проверено текстовых артефактов: {summary['text_artifacts_scanned']}.")
    print(f"Связано карточек метаданных: {summary['metadata_items_matched']}.")
    print(f"Найдено групп кодов: {len(rows)} у {summary['works_with_candidates']} работ.")
    print(f"Кандидаты для проверки: {output / 'candidates.jsonl'}")
    print("Все находки ожидают проверки; реестры, метки H2 и допуск статей не изменялись.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
