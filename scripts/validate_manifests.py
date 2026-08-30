#!/usr/bin/env python3
"""Проверить реальные works/artifacts/rights и связанные файлы data/."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.manifests import ManifestStore  # noqa: E402


def main() -> int:
    """Проверить реестры корпуса и вернуть код завершения команды."""

    parser = argparse.ArgumentParser(
        description="Семантическая проверка реестров корпуса"
    )

    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=PROJECT_ROOT / "manifests",
        help="Каталог рабочих JSONL-реестров",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="Считать предупреждения ошибками",
    )
    
    arguments = parser.parse_args()

    report = ManifestStore(
        project_root=PROJECT_ROOT,
        manifest_dir=arguments.manifest_dir,
    ).audit()

    print(
        "Записей: "
        + ", ".join(f"{kind}={count}" for kind, count in report.counts.items())
    )

    for warning in report.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")

    for error in report.errors:
        print(f"ОШИБКА: {error}", file=sys.stderr)

    if report.errors or (arguments.strict and report.warnings):
        print("Проверка реестров не пройдена.", file=sys.stderr)
        return 1

    print("Проверка реестров пройдена.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
