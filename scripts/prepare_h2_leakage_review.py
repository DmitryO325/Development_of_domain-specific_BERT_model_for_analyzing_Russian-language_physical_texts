#!/usr/bin/env python3
"""Подготовить офлайн-предложения для предварительной проверки утечек H2."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = Path("manifests/results/jinr_metadata_snapshot_20261010_v1/snapshot.json")
DEFAULT_PRACTICE_DIR = Path("manifests/results/h2_practice_20261011_draft_v1")

# Прямой запуск из scripts/ требует добавить корень до импортов локальных модулей.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.h2_candidate_input import read_candidate_snapshot  # noqa: E402
from src.corpus.h2_leakage import build_h2_leakage_review  # noqa: E402
from src.corpus.h2_leakage_io import export_leakage_review, read_practice_holds  # noqa: E402


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Описать локальные источники, обязательную версию результата и пробный запуск."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, default=project_root / DEFAULT_SNAPSHOT,
        help="Опись полного локального снимка метаданных ОИЯИ",
    )
    parser.add_argument(
        "--practice-dir", type=Path, default=project_root / DEFAULT_PRACTICE_DIR,
        help="Исходный пакет пробы с packet.json и private/evaluation_hold.jsonl",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Новый независимый каталог версии внутри manifests/results",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Проверить источники и назначение без записи файлов и каталогов",
    )

    return parser


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить происхождение и экспортировать предложения без назначения разбиения."""

    root = Path(project_root).resolve()
    parser = _build_parser(root)
    parsed = parser.parse_args(arguments)
    snapshot_path = root / parsed.snapshot
    practice_dir = root / parsed.practice_dir

    try:
        snapshot = read_candidate_snapshot(root, snapshot_path)
        practice_holds, provenance = read_practice_holds(root, practice_dir, snapshot)
        review = build_h2_leakage_review(snapshot, practice_holds)
        review["summary"]["practice_provenance"] = provenance
        result = export_leakage_review(
            root, parsed.output_dir, review,
            snapshot_path=snapshot_path, practice_dir=practice_dir, dry_run=parsed.dry_run,
        )

    except (TypeError, ValueError, OSError) as exception:
        parser.error(f"Не удалось подготовить предварительную проверку утечек H2: {exception}")

    print(f"Предложений ограничений: {len(review['constraints'])}; резервов оценки: {len(review['evaluation_holds'])}.")
    print(f"Вопросов для проверки: {len(review['issues'])}.")

    if parsed.dry_run:
        print("Пробный запуск: файлы и каталоги не создавались и не изменялись.")

    else:
        print(f"Создано файлов: {result.created_files}; без изменений: {result.unchanged_files}.")
        print(f"Памятка: {result.output_dir / 'START_HERE.txt'}")

    print("Это предварительные предложения; защита от утечки не завершена, готовое разбиение отсутствует.")
    print("Автоматическое применение, обучение, изменение меток и рабочих реестров не выполнялись.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
