#!/usr/bin/env python3
"""Подготовить офлайн-пакет проверки повторов и связей версий публикаций H2."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = Path("manifests/results/jinr_metadata_snapshot_20261010_v1/snapshot.json")

# Прямой запуск из scripts/ требует добавить корень до импортов локальных модулей.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.h2_candidate_input import read_candidate_snapshot  # noqa: E402
from src.corpus.h2_duplicate_export import export_duplicate_review  # noqa: E402
from src.corpus.h2_duplicate_review import build_duplicate_review  # noqa: E402


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Описать локальный снимок, отдельную версию результата и пробный запуск."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, default=project_root / DEFAULT_SNAPSHOT,
        help="Опись полного локального снимка метаданных с контрольными суммами",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Новый независимый каталог версии внутри manifests/results",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Проверить данные и путь результата без создания файлов и каталогов",
    )

    return parser


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Подготовить проверку без скачивания, объединения статей и изменения реестров."""

    root = Path(project_root).resolve()
    parser = _build_parser(root)
    parsed = parser.parse_args(arguments)
    snapshot_path = parsed.snapshot

    if not snapshot_path.is_absolute():
        snapshot_path = root / snapshot_path

    try:
        snapshot = read_candidate_snapshot(root, snapshot_path)
        review = build_duplicate_review(snapshot)
        result = export_duplicate_review(
            root, parsed.output_dir, review,
            snapshot_path=snapshot_path, dry_run=parsed.dry_run,
        )

    except (ValueError, OSError) as exception:
        parser.error(f"Не удалось подготовить проверку повторов H2: {exception}")

    print(f"Групп совпадений: {len(review['groups'])}; пар для проверки: {len(review['pairs'])}.")
    print(f"Связей, заявленных источником: {len(review['relations'])}.")

    if parsed.dry_run:
        print("Пробный запуск: файлы и каталоги не создавались и не изменялись.")

    else:
        print(f"Создано файлов: {result.created_files}; без изменений: {result.unchanged_files}.")
        print(f"Памятка: {result.output_dir / 'START_HERE.txt'}")

    print("Это сигналы для проверки, а не подтверждённые дубли или готовое разбиение выборки.")
    print("Скачивание, объединение статей, изменение меток H2 и рабочих реестров не выполнялись.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
