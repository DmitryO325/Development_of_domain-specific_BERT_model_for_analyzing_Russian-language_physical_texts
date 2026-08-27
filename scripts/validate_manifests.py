#!/usr/bin/env python3
"""Проверить реальные works/artifacts/rights и связанные файлы data/."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.corpus.manifests import ManifestStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Семантический аудит реестров корпуса")
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=ROOT / "manifests",
        help="Каталог рабочих JSONL-реестров",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Считать предупреждения ошибками",
    )
    args = parser.parse_args()

    report = ManifestStore(project_root=ROOT, manifest_dir=args.manifest_dir).audit()
    print(
        "Записей: "
        + ", ".join(f"{kind}={count}" for kind, count in report.counts.items())
    )
    for warning in report.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")
    for error in report.errors:
        print(f"ОШИБКА: {error}", file=sys.stderr)

    if report.errors or (args.strict and report.warnings):
        print("Аудит не пройден.", file=sys.stderr)
        return 1
    print("Аудит пройден.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
