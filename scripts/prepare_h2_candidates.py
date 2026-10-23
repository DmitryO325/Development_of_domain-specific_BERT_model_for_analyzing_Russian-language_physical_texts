#!/usr/bin/env python3
"""Подготовить офлайн-очередь просмотра кандидатов H2 из закреплённых метаданных."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = Path("manifests/results/jinr_metadata_analysis_20260924/analysis.json")

# Прямой запуск из scripts/ требует добавить корень до импортов локальных модулей.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.h2_candidate_export import export_candidate_queue  # noqa: E402
from src.corpus.h2_candidate_input import read_candidate_snapshot  # noqa: E402
from src.corpus.h2_candidates import build_candidate_queue  # noqa: E402


def _build_parser(project_root: Path) -> argparse.ArgumentParser:
    """Описать локальный снимок и обязательный отдельный каталог версии очереди."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, default=project_root / DEFAULT_SNAPSHOT,
        help="JSON-отчёт с закреплёнными исходными метаданными",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Новый каталог версии внутри manifests/results; старые файлы не заменяются",
    )

    return parser


def main(arguments: list[str] | None = None, *, project_root: Path = PROJECT_ROOT) -> int:
    """Проверить снимок и записать кандидатов без сети, разметки и изменения реестров."""

    root = Path(project_root).resolve()
    parser = _build_parser(root)
    parsed = parser.parse_args(arguments)
    snapshot_path = parsed.snapshot

    if not snapshot_path.is_absolute():
        snapshot_path = root / snapshot_path

    try:
        snapshot = read_candidate_snapshot(root, snapshot_path)
        rows, summary = build_candidate_queue(snapshot)
        result = export_candidate_queue(
            root, parsed.output_dir, rows, summary, snapshot_path=snapshot_path,
        )

    except (ValueError, OSError) as exception:
        parser.error(f"Не удалось подготовить очередь кандидатов H2: {exception}")

    print(f"Кандидатов для просмотра: {len(rows)}.")
    print(f"Создано файлов: {result.created_files}; без изменений: {result.unchanged_files}.")
    print(f"Таблица для просмотра: {result.output_dir / 'review.csv'}")
    print("Это очередь отбора, а не слепая разметка или готовая обучающая выборка.")
    print("Метки H2, разбиение выборки, реестры и допуск по правам не изменялись.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
