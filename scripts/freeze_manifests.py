#!/usr/bin/env python3
"""Создать или проверить неизменяемую версию реестров корпуса."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus.manifests import ManifestStore  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    """Построить интерфейс команд создания и проверки фиксации."""

    parser = argparse.ArgumentParser(
        description="Фиксация неизменяемой версии реестров корпуса"
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=PROJECT_ROOT / "manifests",
        help="Каталог рабочих реестров",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create",
        help="Проверить и зафиксировать текущие реестры",
    )
    create_parser.add_argument("version", help="Неизменяемая версия корпуса")

    verify_parser = subparsers.add_parser(
        "verify",
        help="Сверить ранее зафиксированную версию",
    )
    verify_parser.add_argument("version", help="Проверяемая версия корпуса")

    return parser


def main(arguments: list[str] | None = None) -> int:
    """Выполнить выбранную команду и вернуть код завершения процесса."""

    options = _build_parser().parse_args(arguments)
    store = ManifestStore(
        project_root=PROJECT_ROOT,
        manifest_dir=options.manifest_dir,
    )

    if options.command == "create":
        expected_hashes = store.snapshot_hashes()
        result = store.freeze(
            options.version,
            expected_snapshot_hashes=expected_hashes,
        )
        print(f"Версия {result.version!r} зафиксирована: {result.path}")
        print(f"SHA-256 паспорта: {result.manifest_sha256}")

        return 0

    report = store.verify_frozen(options.version)

    for error in report.errors:
        print(f"ОШИБКА: {error}", file=sys.stderr)

    if not report.ok:
        print("Проверка зафиксированной версии не пройдена.", file=sys.stderr)
        return 1

    print(f"Зафиксированная версия {options.version!r} не изменена.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
